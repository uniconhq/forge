"""Registering for a contest, and what organisers decide about it. A request to
join has nothing at the forge until someone approves it, so all of it is a
row in `contestants`, the package's own table: `register` checks the rules
in `contest.yaml` and writes the row, and nothing is made at the forge until
the registration is approved.

`register` runs in this order and stops at the first refusal, each with a
code of its own (`forge.domain.registration` holds the rules):

1. The contest is one the person may see: published, and `public` or
   `signed-in`. Any other is no such contest.
2. The registration window is open (`registration_closed`).
3. The person holds no role at the contest, at one of its tasks or at its org
   (`is_staff`), read under the org's lock on role changes, so a grant and a
   registration of one person never pass each other.
4. They have no registration for the contest already, whatever became of it
   (`already_registered`).
5. The invite, the code and the email address the contest asks for
   (`invite_required`, `wrong_invite_code`, `domain_not_allowed`), where the
   address is one the forge has confirmed is theirs.
6. A place is free (`contest_full`). The places are counted under an
   advisory lock on the contest, a lock Postgres holds against a name, since
   there is no contest row to lock, so the last place goes once.

The row is written pending with what let it through. With `approval: auto`
a registration that passed is approved in the same call.

An organiser managing the contest decides a registration: `approve` turns a
pending one approved and asks for the contestant's workspace, `reject`
records the decision with a reason the person reads, `reopen` takes a
rejection back and leaves the registration pending again, `remove` ends an
approved one, taking the contestant's access to their workspace away and
keeping what is in it, and `extend` gives one person more time, which every
deadline check adds. Anyone observing the contest lists the registrations,
each with where the contestant's workspace stands.
"""

import builtins
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from forge.db.tables import Contestant, Invite
from forge.domain import registration as rules
from forge.domain import release
from forge.domain.definitions import Approval, ContestDefinition, RegistrationMode
from forge.domain.errors import AlreadyRegistered, ContestFull, IsStaff, NotFound
from forge.domain.identity import User
from forge.domain.ids import ContestId
from forge.domain.registration import Status, WorkspaceState
from forge.domain.roles import Role, contest_scope
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import published, roles, workspaces
from forge.services.access import Organiser, require

log = get_logger(__name__)

PLACES_LOCK = 0x504C4143
"""The first key of every lock on a contest's places, the second being the
contest's id hashed by Postgres."""
INVITE_ACCEPTED = "accepted"


@dataclass(frozen=True, slots=True)
class Registration:
    """One person's registration for a contest. `user` is who they are at the
    forge, or none once their account is gone. `decided_at` and `reason` are
    the last decision and, for a rejection, why. `workspace` is where the
    workspace of an approved contestant stands, and none for anyone else;
    `workspace_error` is why the last try at making part of it failed, in the
    platform's words, while it is still being made.
    """

    contest: ContestId
    user_id: int
    user: User | None
    status: Status
    registered_at: datetime
    decided_at: datetime | None
    reason: str | None
    time_extension: timedelta
    workspace: WorkspaceState | None
    workspace_error: str | None


@action
async def register(
    ctx: Context, session: Session, contest: ContestId, *, invite_code: str | None = None
) -> Registration:
    """Register the signed-in person for the contest, with the contest's code
    when it asks for one. `NotFound` for a contest they may not see, and each
    rule's own refusal otherwise.
    """
    scope = contest_scope(contest)
    settings = await published.contest(ctx, contest)
    if not release.contest_visible_to(
        settings, has_session=True, is_contestant=False, is_organiser=False
    ):
        raise NotFound(published.NO_SUCH_CONTEST)
    rules.refuse_closed(settings.registration, ctx.now)
    user = await ctx.forge.identity.find_user(session.user_id)
    await roles.one_change_at_a_time(ctx, scope.org)
    if await roles.holds_role_in_contest(ctx, user.id, contest):
        raise IsStaff("Organisers of a contest cannot also enter it.")
    await _hold_places(ctx, contest)
    if await row_of(ctx, contest, user.id) is not None:
        raise AlreadyRegistered("You have registered for this contest already.")
    eligibility = rules.eligibility(
        settings.registration,
        emails=await _confirmed_emails(ctx, user.id, settings),
        invite_code=invite_code,
        invited=await _invited(ctx, contest, user.id, settings),
    )
    await _refuse_full(ctx, contest, settings)
    row = Contestant(
        contest_id=contest,
        user_id=user.id,
        status=Status.PENDING,
        registered_at=ctx.now,
        eligibility=eligibility,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    log.info("contestants.registered", contest=contest, user_id=user.id)
    if settings.registration.approval is Approval.AUTO:
        await _approve(ctx, row, decided_by=None)
    return await registration_of(ctx, row, user)


@action
async def mine(ctx: Context, session: Session, contest: ContestId) -> Registration | None:
    """The signed-in person's own registration for the contest, or none."""
    row = await row_of(ctx, contest, session.user_id)
    if row is None:
        return None
    return await registration_of(ctx, row, User(id=session.user_id, username=session.username))


@action
async def list(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Registration, ...]:
    """Every registration for the contest, oldest first, each with where the
    contestant's workspace stands. Needs the observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    rows = (
        await ctx.db.execute(
            select(Contestant)
            .where(Contestant.contest_id == contest)
            .order_by(Contestant.registered_at, Contestant.id)
        )
    ).scalars()
    found = builtins.list(rows)
    ready = await workspaces.readiness(ctx, found)
    return tuple(
        [_registration_of(row, await _user(ctx, row.user_id), ready.get(row.id)) for row in found]
    )


@action
async def approve(
    ctx: Context, organiser: Organiser, contest: ContestId, user_id: int
) -> Registration:
    """Approve a pending registration and ask for the contestant's workspace.
    Needs the manager role at the contest.
    """
    row = await _decided(ctx, organiser, contest, user_id, (Status.PENDING,), "approved")
    await _approve(ctx, row, decided_by=organiser.user.id)
    return await registration_of(ctx, row, await _user(ctx, user_id))


@action
async def reject(
    ctx: Context, organiser: Organiser, contest: ContestId, user_id: int, reason: str
) -> Registration:
    """Reject a pending registration with a reason the person reads. Needs
    the manager role at the contest.
    """
    row = await _decided(ctx, organiser, contest, user_id, (Status.PENDING,), "rejected")
    checked = rules.checked_reason(reason)
    _decide(ctx, row, Status.REJECTED, decided_by=organiser.user.id, reason=checked)
    await ctx.db.flush()
    log.info("contestants.rejected", contest=contest, user_id=user_id, by=organiser.user.id)
    return await registration_of(ctx, row, await _user(ctx, user_id))


@action
async def reopen(
    ctx: Context, organiser: Organiser, contest: ContestId, user_id: int
) -> Registration:
    """Take a rejection back: the registration is pending again, waiting for
    a decision, with the reason gone. It takes a place again, so it is
    refused like a new registration when the person holds a role at the
    contest by now (`is_staff`) or every place is taken (`contest_full`),
    checked under the same locks. Needs the manager role at the contest.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    settings = await published.contest(ctx, contest)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    if await roles.holds_role_in_contest(ctx, user_id, contest):
        raise IsStaff("That person holds a role at this contest now.")
    await _hold_places(ctx, contest)
    row = await _decided(ctx, organiser, contest, user_id, (Status.REJECTED,), "reopened")
    await _refuse_full(ctx, contest, settings)
    row.status = Status.PENDING
    row.decided_at = None
    row.decided_by_user_id = None
    row.reason = None
    await ctx.db.flush()
    log.info("contestants.reopened", contest=contest, user_id=user_id, by=organiser.user.id)
    return await registration_of(ctx, row, await _user(ctx, user_id))


@action
async def remove(
    ctx: Context, organiser: Organiser, contest: ContestId, user_id: int
) -> Registration:
    """End an approved registration: the contestant can no longer write their
    workspace, and what is in it stays. Needs the manager role at the
    contest.
    """
    row = await _decided(ctx, organiser, contest, user_id, (Status.APPROVED,), "removed")
    _decide(ctx, row, Status.REMOVED, decided_by=organiser.user.id, reason=None)
    await ctx.db.flush()
    await workspaces.close(ctx, row)
    log.info("contestants.removed", contest=contest, user_id=user_id, by=organiser.user.id)
    return await registration_of(ctx, row, await _user(ctx, user_id))


@action
async def extend(
    ctx: Context, organiser: Organiser, contest: ContestId, user_id: int, extension: timedelta
) -> Registration:
    """Give one person `extension` past the contest's end in place of any
    they had, so an extension of nothing takes theirs away. Needs the manager
    role at the contest, and the person pending or approved.
    """
    row = await _decided(ctx, organiser, contest, user_id, rules.REGISTERED, "given time")
    checked = rules.checked_extension(extension)
    row.time_extension_seconds = int(checked.total_seconds())
    await ctx.db.flush()
    log.info(
        "contestants.extended",
        contest=contest,
        user_id=user_id,
        seconds=row.time_extension_seconds,
        by=organiser.user.id,
    )
    return await registration_of(ctx, row, await _user(ctx, user_id))


async def row_of(ctx: Context, contest: ContestId, user_id: int) -> Contestant | None:
    """The person's row for the contest, or none."""
    return (
        await ctx.db.execute(
            select(Contestant).where(
                Contestant.contest_id == contest, Contestant.user_id == user_id
            )
        )
    ).scalar_one_or_none()


def time_extension(row: Contestant | None) -> timedelta:
    """The person's own extension, nothing without a row."""
    return timedelta(seconds=row.time_extension_seconds if row is not None else 0)


async def _approve(ctx: Context, row: Contestant, *, decided_by: int | None) -> None:
    _decide(ctx, row, Status.APPROVED, decided_by=decided_by, reason=None)
    await ctx.db.flush()
    await workspaces.request(ctx, row)
    log.info("contestants.approved", contest=row.contest_id, user_id=row.user_id, by=decided_by)


def _decide(
    ctx: Context, row: Contestant, status: Status, *, decided_by: int | None, reason: str | None
) -> None:
    row.status = status
    row.decided_at = ctx.now
    row.decided_by_user_id = decided_by
    row.reason = reason


async def _decided(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    user_id: int,
    allowed: tuple[Status, ...],
    doing: str,
) -> Contestant:
    """The person's row, held until the unit of work ends, once the organiser
    is checked to manage the contest and the row's status allows `doing`.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    row = (
        await ctx.db.execute(
            select(Contestant)
            .where(Contestant.contest_id == contest, Contestant.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That person has not registered for this contest.")
    rules.refuse_undecidable(Status(row.status), allowed, doing)
    return row


async def _hold_places(ctx: Context, contest: ContestId) -> None:
    """Wait for any other registration for the contest to finish, and hold the
    contest's places until this unit of work ends.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:contest))"),
        {"space": PLACES_LOCK, "contest": contest},
    )


async def _refuse_full(ctx: Context, contest: ContestId, settings: ContestDefinition) -> None:
    capacity = settings.registration.capacity
    if capacity is None:
        return
    taken = (
        await ctx.db.execute(
            select(func.count())
            .select_from(Contestant)
            .where(Contestant.contest_id == contest, Contestant.status.in_(rules.REGISTERED))
        )
    ).scalar_one()
    if taken >= capacity:
        raise ContestFull("Every place in this contest is taken.")


async def _confirmed_emails(
    ctx: Context, user_id: int, settings: ContestDefinition
) -> tuple[str, ...]:
    """The person's confirmed addresses, asked only of a contest with an
    email pattern.
    """
    if settings.registration.eligibility.email_pattern is None:
        return ()
    return await ctx.forge.identity.verified_emails(user_id)


async def _invited(
    ctx: Context, contest: ContestId, user_id: int, settings: ContestDefinition
) -> bool:
    """Whether the person accepted an invite to the contest, asked only of an
    invite-only contest.
    """
    if settings.registration.mode is not RegistrationMode.INVITE_ONLY:
        return False
    found = (
        await ctx.db.execute(
            select(Invite.id)
            .where(
                Invite.scope_kind == "contest",
                Invite.scope_id == contest,
                Invite.status == INVITE_ACCEPTED,
                Invite.accepted_by_user_id == user_id,
            )
            .limit(1)
        )
    ).first()
    return found is not None


async def _user(ctx: Context, user_id: int) -> User | None:
    try:
        return await ctx.forge.identity.find_user(user_id)
    except NotFound:
        return None


async def registration_of(ctx: Context, row: Contestant, user: User | None) -> Registration:
    """The registration a row records, with where its workspace stands."""
    ready = await workspaces.readiness(ctx, [row])
    return _registration_of(row, user, ready.get(row.id))


def _registration_of(
    row: Contestant, user: User | None, ready: workspaces.Readiness | None
) -> Registration:
    return Registration(
        contest=ContestId(row.contest_id),
        user_id=row.user_id,
        user=user,
        status=Status(row.status),
        registered_at=row.registered_at,
        decided_at=row.decided_at,
        reason=row.reason,
        time_extension=time_extension(row),
        workspace=ready.workspace if ready is not None else None,
        workspace_error=ready.error if ready is not None else None,
    )
