from sqlalchemy import select, update

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import EntrantMember
from forge.db.tables import Invite as InviteRow
from forge.domain import entrants as rules
from forge.domain import registration
from forge.domain.definitions import ContestDefinition
from forge.domain.entrants import MemberStatus
from forge.domain.errors import InEntrant, InviteRequired, NotFound
from forge.domain.identity import User
from forge.domain.ids import ContestId
from forge.domain.invites import Grant, InviteStatus
from forge.domain.registration import ORGANISER, Qualified, Status, WayKind
from forge.domain.roles import contest_id_of
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrant_view, invites
from forge.services.entrants import guards
from forge.services.entrants.entrant_rows import _after_join, _free_name, _join, _make, _take_out
from forge.services.entrants.guards import (
    _emails,
    _hold_entrants,
    _hold_invites_into,
    _hold_places,
    _refuse_full,
    _refuse_shut,
    _refuse_staff,
    _refuse_window,
)
from forge.services.entrants.queries import membership, ref, submitted
from forge.services.entrants.records import Entered
from forge.services.member_lookup import member_ids

log = get_logger(__name__)


# Accepting an invite to a place in a contest.


async def take_place(
    ctx: Context, session: Session, invite: InviteRow, *, code: str | None
) -> Entered:
    """What accepting `invite` does for the signed-in person, an organiser's
    place in the contest or a member's in an entrant, as this module's
    account of accepting says. The caller holds the org's lock on role
    changes and the invite.
    """
    contest = contest_id_of(invites.scope_of(invite.scope))
    settings = await entrant_view.contest(ctx, contest)
    _refuse_shut(settings, ctx.now)
    by_organiser = invite.grants == Grant.CONTESTANT
    user = await ctx.forge.identity.find_user(session.user_id)
    if invite.entrant_id is None:
        return await _register_invited(ctx, contest, settings, user)
    invited = by_organiser or await invites.accepted_place(ctx, contest, user.id)
    emails = () if by_organiser else await _emails(ctx, user.id, settings)
    await _refuse_staff(ctx, user.id, contest)
    await guards._hold_people(ctx, contest, [user.id])
    current = await membership(ctx, contest, user.id)
    if current is not None and current.entrant_id == invite.entrant_id:
        raise InEntrant("You are in this team already.")
    wanted = [invite.entrant_id, *([current.entrant_id] if current is not None else [])]
    if current is not None:
        await _hold_invites_into(ctx, [current.entrant_id])
    held = {row.id: row for row in await _hold_entrants(ctx, contest, wanted)}
    target = held[invite.entrant_id]
    registration.refuse_undecidable(Status(target.status), registration.REGISTERED, "joined")
    if not by_organiser:
        _refuse_window(settings, ctx.now)
        rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, target))
    rules.refuse_full(len(await member_ids(ctx, target.id)), settings.entrants.most)
    if by_organiser:
        qualified = ORGANISER
    else:
        qualified = registration.qualify(
            settings.registration, emails=emails, code=code, invited=invited
        )
    if current is not None:
        source = held[current.entrant_id]
        await _refuse_moving(ctx, settings, source)
        await _take_out(ctx, source, current, settings)
    await _join(ctx, target, settings, user.id, qualified)
    log.info("entrants.joined", entrant=str(target.id), user_id=user.id, invite=str(invite.id))
    return Entered(await ref(ctx, target), Status(target.status), "joined")


async def _register_invited(
    ctx: Context, contest: ContestId, settings: ContestDefinition, user: User
) -> Entered:
    """Register the person by the contest's invite way, as an organiser
    asked, or find them in the entrant they are in already.
    """
    await guards._hold_people(ctx, contest, [user.id])
    current = await membership(ctx, contest, user.id)
    if current is not None:
        row = await ctx.db.get(EntrantRow, current.entrant_id)
        assert row is not None
        return Entered(await ref(ctx, row), Status(row.status), "already")
    qualified = _by_invite_way(settings)
    await _refuse_staff(ctx, user.id, contest)
    await _hold_places(ctx, contest)
    await _refuse_full(ctx, contest, settings)
    name = await _free_name(ctx, contest, user.username) if settings.entrants.most > 1 else None
    row = await _make(ctx, contest, name, user.id, qualified, by=user.id)
    log.info("entrants.registered", entrant=str(row.id), contest=contest, user_id=user.id)
    await _after_join(ctx, row, settings, qualified.approval)
    return Entered(await ref(ctx, row), Status(row.status), "made")


def _by_invite_way(settings: ContestDefinition) -> Qualified:
    """How someone an organiser invited qualifies: by the contest's invite
    way. `InviteRequired` once the contest has none.
    """
    index = settings.registration.invite_way
    if index is None:
        raise InviteRequired(
            "This contest no longer takes people by invite. Ask the organisers how to enter."
        )
    way = settings.registration.ways[index]
    return Qualified(index, WayKind.INVITE, None, way.approval, {"invited": True})


async def _refuse_moving(ctx: Context, settings: ContestDefinition, source: EntrantRow) -> None:
    """`InEntrant` unless the person may leave the entrant they are in for
    another: its roster is open, it has no submissions, and it has not been
    turned away.
    """
    if (
        Status(source.status) not in registration.REGISTERED
        or await submitted(ctx, source)
        or rules.locked(settings, ctx.now, submitted=False)
    ):
        raise InEntrant(
            "You are already in another team in this contest, and can no longer leave it."
        )


async def forget_person(ctx: Context, user_id: int) -> None:
    """For someone whose account is going: withdraw the invites into their
    entrants they sent that still wait, and take them out of every entrant
    whose roster is still open, as leaving would, so they hold no seat and
    count toward no team's size. Where the roster has locked they stay, shown
    as `DELETED_USER`: the team was fixed with them in it, and only the
    organisers change it from then on.
    """
    await ctx.db.execute(
        update(InviteRow)
        .where(
            InviteRow.invited_by_user_id == user_id,
            InviteRow.grants == Grant.MEMBER,
            InviteRow.status == InviteStatus.PENDING,
        )
        .values(status=InviteStatus.WITHDRAWN, decided_at=ctx.now)
    )
    held = (
        await ctx.db.scalars(
            select(EntrantMember).where(
                EntrantMember.user_id == user_id, EntrantMember.status == MemberStatus.MEMBER
            )
        )
    ).all()
    for found in held:
        contest = ContestId(found.contest_id)
        try:
            settings = await entrant_view.contest(ctx, contest)
        except NotFound:
            continue
        await guards._hold_people(ctx, contest, [user_id])
        await _hold_invites_into(ctx, [found.entrant_id])
        (row,) = await _hold_entrants(ctx, contest, [found.entrant_id])
        member = await membership(ctx, contest, user_id)
        if member is None or rules.locked(settings, ctx.now, submitted=await submitted(ctx, row)):
            continue
        await _take_out(ctx, row, member, settings)
        log.info("entrants.account_left", entrant=str(row.id), user_id=user_id)
