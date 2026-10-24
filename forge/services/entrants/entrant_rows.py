import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import EntrantMember
from forge.db.tables import Invite as InviteRow
from forge.domain import entrants as rules
from forge.domain.definitions import Approval, ContestDefinition
from forge.domain.entrants import MemberStatus
from forge.domain.errors import (
    EntrantNameRequired,
    EntrantNameTaken,
    EntrantsAlone,
    InEntrant,
    NotFound,
)
from forge.domain.ids import ContestId, WorkspaceId, new_id
from forge.domain.invites import InviteStatus
from forge.domain.registration import Qualified, Status
from forge.log import get_logger
from forge.runtime.context import AfterRollback, Context
from forge.services import places
from forge.services.entrants.queries import submitted, workspace_of
from forge.services.member_lookup import member_ids

log = get_logger(__name__)


def _new_name(settings: ContestDefinition, name: str | None) -> str | None:
    """The name a new or renamed entrant takes: needed and checked when the
    contest's entrants may be more than one person, refused when they are
    one each.
    """
    if settings.entrants.most == 1:
        if name is not None:
            raise EntrantsAlone(
                "Everyone enters this contest alone, so there is no team name to give."
            )
        return None
    if name is None:
        raise EntrantNameRequired("Give the team a name.")
    return rules.checked_name(name)


async def _free_name(ctx: Context, contest: ContestId, username: str) -> str:
    """The name an entrant made by an invite takes: the person's username,
    or that with the first number after it that no entrant of the contest
    has, ignoring case.
    """
    taken: set[str | None] = set(
        (
            await ctx.db.scalars(
                select(func.lower(EntrantRow.name)).where(
                    EntrantRow.contest_id == contest, EntrantRow.name.is_not(None)
                )
            )
        ).all()
    )
    name, number = username, 1
    while name.lower() in taken:
        number += 1
        name = f"{username} ({number})"
    return name


async def _make(
    ctx: Context,
    contest: ContestId,
    name: str | None,
    user_id: int,
    qualified: Qualified,
    *,
    by: int | None,
) -> EntrantRow:
    """A new pending entrant with the person as its only member."""
    if name is not None:
        await _refuse_taken(ctx, contest, name, None)
    row = EntrantRow(
        id=new_id(),
        contest_id=contest,
        name=name,
        status=Status.PENDING,
        registered_at=ctx.now,
        registered_by_user_id=by,
    )
    ctx.db.add(row)
    _add_member(ctx, row, user_id, qualified)
    await _flush_once(ctx)
    return row


async def _rename(ctx: Context, row: EntrantRow, name: str | None) -> None:
    if name is not None:
        await _refuse_taken(ctx, ContestId(row.contest_id), name, row.id)
    row.name = name
    await _flush_once(ctx)


async def _refuse_taken(
    ctx: Context, contest: ContestId, name: str, other_than: uuid.UUID | None
) -> None:
    query = select(EntrantRow.id).where(
        EntrantRow.contest_id == contest, func.lower(EntrantRow.name) == name.lower()
    )
    if other_than is not None:
        query = query.where(EntrantRow.id != other_than)
    if await ctx.db.scalar(query.limit(1)) is not None:
        raise EntrantNameTaken(
            f"There is already a team called {name} in this contest. Choose another name."
        )


async def _flush_once(ctx: Context) -> None:
    """Write what is pending; a name another entrant took in the same instant
    is refused as taken, and a person another change put in an entrant in
    the same instant as in one already.
    """
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError as exc:
        if "ix_entrants_contest_id_name" in str(exc.orig):
            raise EntrantNameTaken(
                "There is already a team with that name in this contest. Choose another name."
            ) from exc
        if "ix_entrant_members_one_entrant" in str(exc.orig):
            raise InEntrant("That person has already entered this contest.") from exc
        raise


def _add_member(ctx: Context, row: EntrantRow, user_id: int, qualified: Qualified) -> None:
    ctx.db.add(
        EntrantMember(
            entrant_id=row.id,
            contest_id=row.contest_id,
            user_id=user_id,
            status=MemberStatus.MEMBER,
            joined_at=ctx.now,
            way=qualified.way,
            way_name=qualified.name,
            way_kind=qualified.kind,
            approval=qualified.approval,
            eligibility=qualified.eligibility,
        )
    )


async def _join(
    ctx: Context,
    row: EntrantRow,
    settings: ContestDefinition,
    user_id: int,
    qualified: Qualified,
) -> None:
    """Put someone in an entrant the caller holds, within the contest's
    `entrants.most`: access to every part of its workspace first, then the
    member row, then the approval rule.
    """
    current = await member_ids(ctx, row.id)
    rules.refuse_full(len(current), settings.entrants.most)
    workspace = workspace_of(ctx, row)
    await ctx.forge.workspaces.share_workspace(workspace, [*current, user_id])
    ctx.after_rollback(_shared_again(ctx, workspace, current))
    _add_member(ctx, row, user_id, qualified)
    await _flush_once(ctx)
    await _after_join(ctx, row, settings, qualified.approval)


async def _after_join(
    ctx: Context, row: EntrantRow, settings: ContestDefinition, joined: Approval
) -> None:
    """Once someone joined by a way with `joined` approval: a pending
    entrant that is complete and whose every member came in by a way that
    approves straight away is approved, and an approved one that someone
    joined by a way an organiser approves is pending again.
    """
    status = Status(row.status)
    if status is Status.PENDING:
        await _approve_if_due(ctx, row, settings)
    elif status is Status.APPROVED and joined is Approval.MANUAL:
        row.status = Status.PENDING
        row.decided_at = None
        row.decided_by_user_id = None
        row.reason = None
        await ctx.db.flush()
        log.info("entrants.pending_again", entrant=str(row.id))


async def _approve_if_due(ctx: Context, row: EntrantRow, settings: ContestDefinition) -> None:
    """Approve a pending entrant that is complete and whose every member
    came in by a way that approves straight away, whether someone just
    joined it or the member who held it back just left.
    """
    approvals = await ctx.db.scalars(
        select(EntrantMember.approval).where(
            EntrantMember.entrant_id == row.id, EntrantMember.status == MemberStatus.MEMBER
        )
    )
    if rules.approved_on_its_own(settings, [Approval(found) for found in approvals]):
        await _approve(ctx, row, decided_by=None)


async def _approve(ctx: Context, row: EntrantRow, *, decided_by: int | None) -> None:
    _decide(ctx, row, Status.APPROVED, decided_by=decided_by, reason=None)
    await ctx.db.flush()
    places.ahead_for(ctx, ContestId(row.contest_id), row.id)
    log.info("entrants.approved", entrant=str(row.id), by=decided_by)


def _decide(
    ctx: Context, row: EntrantRow, status: Status, *, decided_by: int | None, reason: str | None
) -> None:
    row.status = status
    row.decided_at = ctx.now
    row.decided_by_user_id = decided_by
    row.reason = reason


async def _take_out(
    ctx: Context, row: EntrantRow, member: EntrantMember, settings: ContestDefinition
) -> bool:
    """End a membership: the person's access to the entrant's workspace taken
    away first, then the row `left`, and an entrant left with nobody and no
    submissions deleted; a pending one that the rest now approve on their
    own is approved. Whether the entrant is still there.
    """
    workspace = workspace_of(ctx, row)
    members = await member_ids(ctx, row.id)
    await _close(ctx, workspace, [member.user_id])
    ctx.after_rollback(_shared_again(ctx, workspace, members))
    member.status = MemberStatus.LEFT
    member.left_at = ctx.now
    await ctx.db.flush()
    if not await member_ids(ctx, row.id) and not await submitted(ctx, row):
        await _end(ctx, row)
        log.info("entrants.emptied", entrant=str(row.id))
        return False
    if Status(row.status) is Status.PENDING:
        await _approve_if_due(ctx, row, settings)
    return True


async def _end(ctx: Context, row: EntrantRow) -> None:
    """Delete an entrant, its members' rows kept `left`, with when, so each
    person's uploads afterwards are told from its, and the invites into it
    still waiting withdrawn.
    """
    await ctx.db.execute(
        update(EntrantMember)
        .where(EntrantMember.entrant_id == row.id, EntrantMember.status == MemberStatus.MEMBER)
        .values(status=MemberStatus.LEFT, left_at=ctx.now)
    )
    await ctx.db.execute(
        update(InviteRow)
        .where(InviteRow.entrant_id == row.id, InviteRow.status == InviteStatus.PENDING)
        .values(status=InviteStatus.WITHDRAWN, decided_at=ctx.now)
    )
    await ctx.db.delete(row)
    await ctx.db.flush()


def _shared_again(ctx: Context, workspace: WorkspaceId, user_ids: Sequence[int]) -> AfterRollback:
    """Work that puts the workspace back to exactly `user_ids`, the members
    it had before this change, for a change that gave or took access at the
    forge and then failed, so access follows the membership that stands.
    """
    members = list(user_ids)

    async def share_again() -> None:
        await ctx.forge.workspaces.share_workspace(workspace, members)
        log.info("entrants.access_restored", workspace=workspace, user_ids=members)

    return share_again


async def _close(ctx: Context, workspace: WorkspaceId, user_ids: Sequence[int]) -> None:
    """Take the people's access to the workspace away and keep what is in
    it; someone whose account is gone has no access left to take.
    """
    if not user_ids:
        return
    try:
        await ctx.forge.workspaces.close_workspace(workspace, list(user_ids))
    except NotFound:
        log.info("entrants.account_gone", workspace=workspace)
        return
    log.info("entrants.closed", workspace=workspace, user_ids=list(user_ids))
