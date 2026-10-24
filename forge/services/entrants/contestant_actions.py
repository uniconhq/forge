import uuid

from sqlalchemy import select

from forge.db.tables import Invite as InviteRow
from forge.domain import entrants as rules
from forge.domain import registration
from forge.domain import release as release_rules
from forge.domain.errors import AlreadyRegistered, EntrantsAlone, InEntrant, IsStaff, NotFound
from forge.domain.ids import ContestId
from forge.domain.invites import Grant, InviteStatus
from forge.domain.registration import Status
from forge.domain.roles import contest_scope
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrant_view, invites, roles
from forge.services.entrants import guards
from forge.services.entrants.entrant_rows import _after_join, _make, _new_name, _rename, _take_out
from forge.services.entrants.guards import (
    _emails,
    _held_own,
    _hold_entrants,
    _hold_invites_into,
    _hold_places,
    _own,
    _refuse_fixed,
    _refuse_full,
    _refuse_staff,
    _refuse_too_many_sent,
    _refuse_window,
    _waiting,
)
from forge.services.entrants.queries import membership, submitted
from forge.services.entrants.records import NOT_IN_ONE, Entrant, Mine
from forge.services.entrants.views import _view, panel
from forge.services.member_lookup import member_ids

log = get_logger(__name__)


# The contestant's actions.


@action
async def register(
    ctx: Context,
    session: Session,
    contest: ContestId,
    *,
    name: str | None = None,
    code: str | None = None,
) -> Entrant:
    """Register a new entrant with the signed-in person as its first member,
    named `name` and with the contest's `code` when it asks for them.
    `NotFound` for a contest they may not see, and each rule's own refusal
    otherwise.
    """
    settings = await entrant_view.contest(ctx, contest)
    invited = await invites.accepted_place(ctx, contest, session.user_id)
    if not release_rules.contest_visible_to(
        settings, has_session=True, is_contestant=invited, is_organiser=False
    ):
        raise NotFound(entrant_view.NO_SUCH_CONTEST)
    _refuse_window(settings, ctx.now)
    _refuse_fixed(settings, ctx.now)
    user = await ctx.forge.identity.find_user(session.user_id)
    emails = await _emails(ctx, user.id, settings)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    await _refuse_staff(ctx, user.id, contest)
    await guards._hold_people(ctx, contest, [user.id])
    if await membership(ctx, contest, user.id) is not None:
        raise AlreadyRegistered("You have already entered this contest.")
    qualified = registration.qualify(
        settings.registration, emails=emails, code=code, invited=invited
    )
    await _hold_places(ctx, contest)
    await _refuse_full(ctx, contest, settings)
    named = _new_name(settings, name)
    row = await _make(ctx, contest, named, user.id, qualified, by=user.id)
    log.info("entrants.registered", entrant=str(row.id), contest=contest, user_id=user.id)
    await _after_join(ctx, row, settings, qualified.approval)
    return await _view(ctx, row, settings)


@action
async def mine(ctx: Context, session: Session, contest: ContestId) -> Mine:
    """The signed-in person's registration panel for the contest: their
    entrant, their invites into its entrants, and whether and how they may
    register. `NotFound` for a contest they neither see nor are in.
    """
    settings, person = await _own(ctx, session, contest)
    return await panel(ctx, session, contest, settings, person)


@action
async def invite(
    ctx: Context,
    session: Session,
    contest: ContestId,
    *,
    username: str | None = None,
    email: str | None = None,
) -> invites.Invite:
    """Ask the person a username or an address names into the signed-in
    person's entrant, and mail them once it commits.
    """
    settings, _ = await _own(ctx, session, contest)
    if settings.entrants.most == 1:
        raise EntrantsAlone(
            "Everyone enters this contest alone, so there is no team to invite anyone into."
        )
    target = await invites.target(ctx, username, email)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    row = await _held_own(ctx, contest, session.user_id)
    registration.refuse_undecidable(Status(row.status), registration.REGISTERED, "joined")
    rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, row))
    if target.user is not None:
        if target.user.id in await member_ids(ctx, row.id):
            raise InEntrant(f"{target.user.username} is in this team already.")
        if await roles.holds_role_in_contest(ctx, target.user.id, contest):
            raise IsStaff(
                f"{target.user.username} has a role at this contest, so they cannot enter it."
            )
    await invites.refuse_twice(ctx, contest_scope(contest), Grant.MEMBER, target, row.id)
    rules.refuse_full(
        len(await member_ids(ctx, row.id)) + await _waiting(ctx, row.id),
        settings.entrants.most,
    )
    await _refuse_too_many_sent(ctx, row.id)
    made = await invites.make(
        ctx,
        contest_scope(contest),
        Grant.MEMBER,
        target,
        invited_by=session.user_id,
        entrant=row.id,
    )
    log.info("entrants.invited", entrant=str(row.id), invite=str(made.id), by=session.user_id)
    return made


@action
async def withdraw_invite(
    ctx: Context, session: Session, contest: ContestId, invite: uuid.UUID
) -> invites.Invite:
    """Take back a member's invite into the signed-in person's entrant that
    is still waiting. `NotFound` for any other.
    """
    await _own(ctx, session, contest)
    found = (
        await ctx.db.execute(
            select(InviteRow)
            .where(
                InviteRow.id == invite,
                InviteRow.scope == contest,
                InviteRow.grants == Grant.MEMBER,
                InviteRow.status == InviteStatus.PENDING,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if found is None or found.entrant_id is None:
        raise NotFound(invites.NO_SUCH_INVITE)
    await _hold_entrants(ctx, contest, [found.entrant_id])
    if session.user_id not in await member_ids(ctx, found.entrant_id):
        raise NotFound(invites.NO_SUCH_INVITE)
    found.status = InviteStatus.WITHDRAWN
    found.decided_at = ctx.now
    await ctx.db.flush()
    log.info("entrants.invite_withdrawn", invite=str(invite), by=session.user_id)
    return await invites.view(ctx, found)


@action
async def leave(ctx: Context, session: Session, contest: ContestId) -> None:
    """Leave the signed-in person's entrant before its roster locks. Their
    access to its workspace goes; what it made stays the entrant's.
    """
    settings, _ = await _own(ctx, session, contest)
    await guards._hold_people(ctx, contest, [session.user_id])
    member = await membership(ctx, contest, session.user_id)
    if member is None:
        raise NotFound(NOT_IN_ONE)
    await _hold_invites_into(ctx, [member.entrant_id])
    (row,) = await _hold_entrants(ctx, contest, [member.entrant_id])
    registration.refuse_undecidable(Status(row.status), registration.REGISTERED, "left")
    rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, row))
    await _take_out(ctx, row, member, settings)
    log.info("entrants.left", entrant=str(row.id), user_id=session.user_id)


@action
async def rename(ctx: Context, session: Session, contest: ContestId, name: str) -> Entrant:
    """Give the signed-in person's entrant a new name, before its roster
    locks.
    """
    settings, _ = await _own(ctx, session, contest)
    checked = _new_name(settings, name)
    row = await _held_own(ctx, contest, session.user_id)
    rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, row))
    await _rename(ctx, row, checked)
    log.info("entrants.renamed", entrant=str(row.id), by=session.user_id)
    return await _view(ctx, row, settings)
