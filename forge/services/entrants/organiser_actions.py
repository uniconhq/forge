import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import select

from forge.db.tables import Entrant as EntrantRow
from forge.domain import entrants as rules
from forge.domain import registration
from forge.domain.errors import EntrantChanged, EntrantHasSubmissions, IsStaff, NotFound
from forge.domain.ids import ContestId
from forge.domain.registration import ORGANISER, Status
from forge.domain.roles import Role, contest_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrant_view, roles, timelines
from forge.services.access import Organiser, require
from forge.services.entrants import entrant_rows, guards
from forge.services.entrants.entrant_rows import (
    _approve,
    _decide,
    _end,
    _join,
    _new_name,
    _rename,
    _take_out,
)
from forge.services.entrants.guards import (
    _decided,
    _hold_entrants,
    _hold_invites_into,
    _hold_places,
    _managed,
    _refuse_full,
)
from forge.services.entrants.queries import membership, submitted, workspace_of
from forge.services.entrants.records import Entrant
from forge.services.entrants.views import _view, _views
from forge.services.member_lookup import member_ids

log = get_logger(__name__)


# The organisers' actions.


@action
async def list_entrants(
    ctx: Context, organiser: Organiser, contest: ContestId
) -> tuple[Entrant, ...]:
    """Every entrant of the contest, the earliest registered first. Needs
    the observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await entrant_view.contest(ctx, contest)
    rows = (
        await ctx.db.scalars(
            select(EntrantRow)
            .where(EntrantRow.contest_id == contest)
            .order_by(EntrantRow.registered_at, EntrantRow.id)
        )
    ).all()
    return await _views(ctx, rows, settings)


@action
async def approve(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> Entrant:
    """Approve a pending entrant that has as many members as the contest
    asks. Needs the manager role at the contest.
    """
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.PENDING,), "approved")
    rules.refuse_incomplete(settings, len(await member_ids(ctx, row.id)))
    await _approve(ctx, row, decided_by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def reject(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID, reason: str
) -> Entrant:
    """Reject a pending entrant with a reason its members read; they can no
    longer write its workspace, which one approved before may have. Needs
    the manager role at the contest.
    """
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.PENDING,), "rejected")
    checked = registration.checked_reason(reason)
    _decide(ctx, row, Status.REJECTED, decided_by=organiser.user.id, reason=checked)
    await ctx.db.flush()
    await entrant_rows._close(ctx, workspace_of(ctx, row), await member_ids(ctx, row.id))
    log.info("entrants.rejected", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def reopen(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> Entrant:
    """Take a rejection back: the entrant is pending again, waiting for a
    decision, with the reason gone. It takes a place again, so it is refused
    like a new registration when a member holds a role at the contest by now
    (`is_staff`) or every place is taken (`contest_full`), checked under the
    same locks. Its members reach its workspace again, as before the
    rejection took that away. Needs the manager role at the contest.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    for user_id in await member_ids(ctx, entrant):
        if await roles.holds_role_in_contest(ctx, user_id, contest):
            raise IsStaff(
                "A member of this entrant now has a role at this contest, so the entrant cannot "
                "be reopened."
            )
    await _hold_places(ctx, contest)
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.REJECTED,), "reopened")
    await _refuse_full(ctx, contest, settings)
    row.status = Status.PENDING
    row.decided_at = None
    row.decided_by_user_id = None
    row.reason = None
    await ctx.db.flush()
    await ctx.forge.workspaces.share_workspace(
        workspace_of(ctx, row), await member_ids(ctx, row.id)
    )
    log.info("entrants.reopened", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def remove(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> Entrant:
    """End an approved entrant: its members can no longer write its
    workspace, and what is in it stays. Needs the manager role at the
    contest.
    """
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.APPROVED,), "removed")
    _decide(ctx, row, Status.REMOVED, decided_by=organiser.user.id, reason=None)
    await ctx.db.flush()
    await entrant_rows._close(ctx, workspace_of(ctx, row), await member_ids(ctx, row.id))
    log.info("entrants.removed", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def extend(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    entrant: uuid.UUID,
    extension: timedelta,
    tasks: Sequence[str] | None = None,
) -> Entrant:
    """Give the entrant `extension` on the contest's `tasks`, by name, every
    task when none are named, in place of any it had, so an extension of
    nothing takes it away. It moves the entrant's due and close on those
    tasks. Needs the manager role at the contest, and the entrant pending or
    approved. `InvalidExtension` when it names a task the contest does not
    list, would let the entrant submit to a task whose reveal has passed, or
    would leave one of its submissions after the due or the close it was
    made before.
    """
    settings, row = await _decided(
        ctx, organiser, contest, entrant, registration.REGISTERED, "given time"
    )
    checked = registration.checked_extension(extension)
    named = registration.checked_tasks(tasks, [entry.id for entry in settings.tasks])
    await timelines.refuse_extension(
        ctx,
        contest,
        settings,
        workspace_of(ctx, row),
        timelines.of_entrant(row),
        timelines.extension_of(int(checked.total_seconds()), named),
    )
    row.time_extension_seconds = int(checked.total_seconds())
    row.extension_tasks = [*named] if named is not None else None
    await ctx.db.flush()
    log.info(
        "entrants.extended",
        entrant=str(entrant),
        seconds=row.time_extension_seconds,
        by=organiser.user.id,
    )
    return await _view(ctx, row, settings)


@action
async def organise_rename(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID, name: str
) -> Entrant:
    """Give an entrant a new name. Needs the manager role at the contest."""
    settings = await _managed(ctx, organiser, contest)
    checked = _new_name(settings, name)
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    await _rename(ctx, row, checked)
    log.info("entrants.renamed", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def remove_member(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID, user_id: int
) -> Entrant | None:
    """Take someone out of an entrant, their access to its workspace taken
    away; none when that leaves an entrant with nobody and no submissions,
    which is deleted. Needs the manager role at the contest.
    """
    settings = await _managed(ctx, organiser, contest)
    await guards._hold_people(ctx, contest, [user_id])
    await _hold_invites_into(ctx, [entrant])
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    member = await membership(ctx, contest, user_id)
    if member is None or member.entrant_id != row.id:
        raise NotFound("That person is not in this entrant.")
    kept = await _take_out(ctx, row, member, settings)
    log.info("entrants.member_removed", entrant=str(entrant), user_id=user_id, by=organiser.user.id)
    return await _view(ctx, row, settings) if kept else None


@action
async def move_member(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    entrant: uuid.UUID,
    user_id: int,
    to: uuid.UUID,
) -> Entrant:
    """Move someone from an entrant to another of the contest, within its
    `entrants.most` (`entrant_full`), and answer with the one they are in
    now. Refused with `entrant_has_submissions` when they are the last member
    of an entrant that has submitted, whose results would have nobody; with
    `wrong_status` into an entrant neither pending nor approved; and with
    `is_staff` for someone who holds a role at the contest by now. Needs the
    manager role at the contest.
    """
    settings = await _managed(ctx, organiser, contest)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    await guards._hold_people(ctx, contest, [user_id])
    await _hold_invites_into(ctx, [entrant])
    held = {row.id: row for row in await _hold_entrants(ctx, contest, [entrant, to])}
    source, target = held[entrant], held[to]
    member = await membership(ctx, contest, user_id)
    if member is None or member.entrant_id != source.id:
        raise NotFound("That person is not in this entrant.")
    if source.id == target.id:
        return await _view(ctx, target, settings)
    registration.refuse_undecidable(Status(target.status), registration.REGISTERED, "joined")
    if await roles.holds_role_in_contest(ctx, user_id, contest):
        raise IsStaff("That person has a role at this contest, so they cannot be in an entrant.")
    others = [other for other in await member_ids(ctx, source.id) if other != user_id]
    if not others and await submitted(ctx, source):
        raise EntrantHasSubmissions(
            "That person is the last member of an entrant that has submitted, and its results "
            "need someone in it. Move someone else in first."
        )
    rules.refuse_full(len(await member_ids(ctx, target.id)), settings.entrants.most)
    await _take_out(ctx, source, member, settings)
    await _join(ctx, target, settings, user_id, ORGANISER)
    log.info(
        "entrants.member_moved",
        entrant=str(entrant),
        to=str(to),
        user_id=user_id,
        by=organiser.user.id,
    )
    return await _view(ctx, target, settings)


@action
async def delete(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> None:
    """Delete an entrant that has submitted nothing, taking every member's
    access to its workspace away and withdrawing the invites into it. Needs
    the manager role at the contest.
    """
    await _managed(ctx, organiser, contest)
    people = await member_ids(ctx, entrant)
    await guards._hold_people(ctx, contest, people)
    await _hold_invites_into(ctx, [entrant])
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    if not set(await member_ids(ctx, row.id)) <= set(people):
        # Someone joined between the first read and the hold, without their
        # own lock held here.
        raise EntrantChanged("This entrant changed in the meantime. Try again.")
    if await submitted(ctx, row):
        raise EntrantHasSubmissions(
            "This entrant has submitted, so it cannot be deleted: it is kept with its results."
        )
    current = await member_ids(ctx, row.id)
    if current:
        await entrant_rows._close(ctx, workspace_of(ctx, row), current)
    await _end(ctx, row)
    log.info("entrants.deleted", entrant=str(entrant), by=organiser.user.id)
