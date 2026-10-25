import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import Invite as InviteRow
from forge.domain import entrants as rules
from forge.domain import registration
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import (
    ContestFull,
    EntrantChanged,
    InviteLimit,
    IsStaff,
    NotFound,
    PortError,
    RegistrationClosed,
)
from forge.domain.ids import ContestId
from forge.domain.invites import Grant, InviteStatus
from forge.domain.registration import Status
from forge.domain.roles import Role, contest_scope
from forge.domain.sessions import Session
from forge.runtime.context import Context
from forge.services import entrant_view, release, roles
from forge.services.access import Organiser, require
from forge.services.entrants.queries import membership
from forge.services.entrants.records import (
    MEMBER_DAILY_MAX,
    NO_SUCH_ENTRANT,
    NOT_IN_ONE,
    PEOPLE_LOCK,
    PLACES_LOCK,
)
from forge.services.member_lookup import member_ids

# The pieces.


async def _own(
    ctx: Context, session: Session, contest: ContestId
) -> tuple[ContestDefinition, release.Reader]:
    """The contest's settings and the person, once the contest is one they
    see or one they are in an entrant of, whatever its visibility says of
    them now. `NotFound` otherwise.
    """
    settings = await entrant_view.contest(ctx, contest)
    person = await release.reader(ctx, session, contest)
    if not release.sees(settings, person) and person.entrant is None:
        raise NotFound(entrant_view.NO_SUCH_CONTEST)
    return settings, person


async def _held_own(ctx: Context, contest: ContestId, user_id: int) -> EntrantRow:
    """The person's entrant, held until the unit of work ends, once they are
    found still in it. `NotFound` for someone in none.
    """
    member = await membership(ctx, contest, user_id)
    if member is None:
        raise NotFound(NOT_IN_ONE)
    (row,) = await _hold_entrants(ctx, contest, [member.entrant_id])
    if user_id not in await member_ids(ctx, row.id):
        raise EntrantChanged("Your team changed in the meantime. Try again.")
    return row


async def _managed(ctx: Context, organiser: Organiser, contest: ContestId) -> ContestDefinition:
    require(organiser, contest_scope(contest), Role.MANAGER)
    return await entrant_view.contest(ctx, contest)


async def _decided(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    entrant: uuid.UUID,
    allowed: tuple[Status, ...],
    doing: str,
) -> tuple[ContestDefinition, EntrantRow]:
    """The contest's settings and the entrant, held until the unit of work
    ends, once the organiser is checked to manage the contest and the
    entrant's status allows `doing`.
    """
    settings = await _managed(ctx, organiser, contest)
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    registration.refuse_undecidable(Status(row.status), allowed, doing)
    return settings, row


def _refuse_shut(settings: ContestDefinition, now: datetime) -> None:
    """`RegistrationClosed` unless the contest is open and has not ended:
    the one thing an organiser's invite, which passes the window and the
    roster's lock, still needs.
    """
    if settings.state is State.ARCHIVED or now >= settings.end:
        raise RegistrationClosed("The contest has ended, so registration is closed.")
    if settings.state is not State.PUBLISHED:
        raise RegistrationClosed(
            "This contest is not open yet, so nobody can enter it. Try again once the organisers "
            "open it."
        )


def _teams_fixed(settings: ContestDefinition, now: datetime) -> bool:
    """Whether a new entrant would start with its roster locked in a
    contest whose entrants need more than one person: it could never grow
    to `entrants.fewest`, so it may not register.
    """
    return settings.entrants.fewest > 1 and rules.locked(settings, now, submitted=False)


def _refuse_fixed(settings: ContestDefinition, now: datetime) -> None:
    """`RegistrationClosed` once `_teams_fixed`."""
    if _teams_fixed(settings, now):
        raise RegistrationClosed(
            "Teams were fixed when the contest started, so no new team can register now. Ask "
            "the organisers to add you to one."
        )


def _refuse_window(settings: ContestDefinition, now: datetime) -> None:
    """`RegistrationClosed` unless the window is open and the contest still
    running or to come.
    """
    if settings.state is State.ARCHIVED or now >= settings.end:
        raise RegistrationClosed("The contest has ended, so registration is closed.")
    registration.refuse_closed(settings.registration, now)


async def _refuse_staff(ctx: Context, user_id: int, contest: ContestId) -> None:
    if await roles.holds_role_in_contest(ctx, user_id, contest):
        raise IsStaff("You have a role at this contest, so you cannot also enter it.")


async def _emails(ctx: Context, user_id: int, settings: ContestDefinition) -> tuple[str, ...]:
    """The person's confirmed addresses, asked of the forge only when some
    way takes only some addresses.
    """
    if not registration.asks_address(settings.registration):
        return ()
    return await ctx.forge.identity.verified_emails(user_id)


async def _emails_or_none(
    ctx: Context, user_id: int, settings: ContestDefinition
) -> tuple[str, ...] | None:
    """`_emails`, or none when the forge does not answer, for a panel that
    says so rather than failing, and never blames the person's addresses.
    """
    try:
        return await _emails(ctx, user_id, settings)
    except PortError:
        return None


async def _hold_people(ctx: Context, contest: ContestId, user_ids: Sequence[int]) -> None:
    """Hold each person in the contest, in the order of their ids, until the
    unit of work ends, so two changes of who one person is with happen one
    after another.
    """
    for user_id in sorted(set(user_ids)):
        await ctx.db.execute(
            text("SELECT pg_advisory_xact_lock(:space, hashtext(:person))"),
            {"space": PEOPLE_LOCK, "person": f"{contest}|{user_id}"},
        )


async def _hold_places(ctx: Context, contest: ContestId) -> None:
    """Wait for any other registration for the contest to finish, and hold
    the contest's places until this unit of work ends.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:contest))"),
        {"space": PLACES_LOCK, "contest": contest},
    )


async def _hold_invites_into(ctx: Context, entrants: Sequence[uuid.UUID]) -> None:
    """Hold every invite still waiting into the entrants, in the order of
    their ids, until the unit of work ends: a change that may leave an
    entrant empty, which withdraws them (`_end`), takes them before the
    entrant, as accepting or withdrawing one does, so the two never wait on
    each other.
    """
    await ctx.db.execute(
        select(InviteRow.id)
        .where(
            InviteRow.entrant_id.in_(sorted(set(entrants))),
            InviteRow.status == InviteStatus.PENDING,
        )
        .order_by(InviteRow.id)
        .with_for_update()
    )


async def _hold_entrants(
    ctx: Context, contest: ContestId, entrants: Sequence[uuid.UUID]
) -> list[EntrantRow]:
    """Each entrant of the contest, held until the unit of work ends, taken
    in the order of their ids and given back in the order asked.
    `NotFound` for one that is not the contest's.
    """
    held: dict[uuid.UUID, EntrantRow] = {}
    for entrant in sorted(set(entrants)):
        found = (
            await ctx.db.execute(
                select(EntrantRow)
                .where(EntrantRow.id == entrant, EntrantRow.contest_id == contest)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFound(NO_SUCH_ENTRANT)
        held[entrant] = found
    return [held[entrant] for entrant in entrants]


async def _refuse_full(ctx: Context, contest: ContestId, settings: ContestDefinition) -> None:
    capacity = settings.registration.capacity
    if capacity is None:
        return
    taken = await ctx.db.scalar(
        select(func.count())
        .select_from(EntrantRow)
        .where(EntrantRow.contest_id == contest, EntrantRow.status.in_(registration.REGISTERED))
    )
    if (taken or 0) >= capacity:
        raise ContestFull("Every place in this contest is taken.")


async def _waiting(ctx: Context, entrant: uuid.UUID) -> int:
    """How many invites into the entrant are still waiting."""
    found = await ctx.db.scalar(
        select(func.count())
        .select_from(InviteRow)
        .where(
            InviteRow.entrant_id == entrant,
            InviteRow.status == InviteStatus.PENDING,
            InviteRow.expires_at > ctx.now,
        )
    )
    return found or 0


async def _refuse_too_many_sent(ctx: Context, entrant: uuid.UUID) -> None:
    """`InviteLimit` once the entrant's members have sent `MEMBER_DAILY_MAX`
    invites into it in the last day, the ones withdrawn since included.
    """
    since = ctx.now - timedelta(days=1)
    sent = await ctx.db.scalar(
        select(func.count())
        .select_from(InviteRow)
        .where(
            InviteRow.entrant_id == entrant,
            InviteRow.grants == Grant.MEMBER,
            InviteRow.created_at > since,
        )
    )
    if (sent or 0) >= MEMBER_DAILY_MAX:
        retry_at = ctx.now + timedelta(hours=1)
        raise InviteLimit(
            f"Your team has sent {MEMBER_DAILY_MAX} invites in the last day, the most it can. "
            "Send more later.",
            retry_at=retry_at.isoformat(),
        )
