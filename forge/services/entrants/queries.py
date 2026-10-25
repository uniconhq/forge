import uuid
from collections.abc import Collection

from sqlalchemy import select

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import EntrantMember, Grading
from forge.domain.entrants import DELETED_USER, EntrantRef, MemberStatus
from forge.domain.errors import NotFound, PortError
from forge.domain.ids import ContestId, WorkspaceId
from forge.domain.names import EntrantOwner
from forge.domain.registration import Status
from forge.runtime.context import Context
from forge.services.entrants.records import Standing
from forge.services.member_lookup import member_ids


async def membership(ctx: Context, contest: ContestId, user_id: int) -> EntrantMember | None:
    """The person's member row of the entrant they are in now, or none."""
    return (
        await ctx.db.execute(
            select(EntrantMember)
            .where(
                EntrantMember.contest_id == contest,
                EntrantMember.user_id == user_id,
                EntrantMember.status == MemberStatus.MEMBER,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def standing(ctx: Context, contest: ContestId, user_id: int) -> Standing | None:
    """The entrant the person is in for the contest, with its workspace's
    owner, every member, and when the person joined; none outside one.
    """
    member = await membership(ctx, contest, user_id)
    if member is None:
        return None
    row = await ctx.db.get(EntrantRow, member.entrant_id)
    if row is None:
        return None
    return Standing(
        row,
        EntrantOwner(row.id),
        tuple(await member_ids(ctx, row.id)),
        (member.joined_at, member.id),
    )


async def everyone_in(ctx: Context, entrant: uuid.UUID) -> frozenset[int]:
    """Everyone who is or was a member of the entrant, whose messages on its
    desk are the entrant's.
    """
    found = await ctx.db.scalars(
        select(EntrantMember.user_id).where(EntrantMember.entrant_id == entrant)
    )
    return frozenset(found)


async def entrant_ids(ctx: Context, user_id: int) -> frozenset[str]:
    """The id of every entrant the person is a member of, in any contest."""
    found = await ctx.db.scalars(
        select(EntrantMember.entrant_id).where(
            EntrantMember.user_id == user_id, EntrantMember.status == MemberStatus.MEMBER
        )
    )
    return frozenset(str(entrant) for entrant in found)


async def approved_contests(ctx: Context, user_id: int) -> frozenset[ContestId]:
    """Every contest where the person is in an approved entrant."""
    found = await ctx.db.scalars(
        select(EntrantRow.contest_id)
        .join(EntrantMember, EntrantMember.entrant_id == EntrantRow.id)
        .where(
            EntrantMember.user_id == user_id,
            EntrantMember.status == MemberStatus.MEMBER,
            EntrantRow.status == Status.APPROVED,
        )
    )
    return frozenset(ContestId(contest) for contest in found)


async def statuses(ctx: Context, user_id: int) -> dict[str, Status]:
    """The status of the entrant the person is in, by contest."""
    found = await ctx.db.execute(
        select(EntrantRow.contest_id, EntrantRow.status)
        .join(EntrantMember, EntrantMember.entrant_id == EntrantRow.id)
        .where(EntrantMember.user_id == user_id, EntrantMember.status == MemberStatus.MEMBER)
    )
    return {contest: Status(status) for contest, status in found}


async def workspaces_of(ctx: Context, contest: ContestId) -> list[WorkspaceId]:
    """The workspace of every entrant of the contest, whatever became of it,
    since a removed entrant's submissions are kept.
    """
    found = await ctx.db.scalars(
        select(EntrantRow.id)
        .where(EntrantRow.contest_id == contest)
        .order_by(EntrantRow.registered_at, EntrantRow.id)
    )
    return [ctx.forge.workspaces.workspace_of(contest, EntrantOwner(one)) for one in found.all()]


def workspace_of(ctx: Context, row: EntrantRow) -> WorkspaceId:
    return ctx.forge.workspaces.workspace_of(ContestId(row.contest_id), EntrantOwner(row.id))


async def submitted(ctx: Context, row: EntrantRow) -> bool:
    """Whether the entrant has made a submission."""
    found = await ctx.db.scalar(
        select(Grading.id).where(Grading.workspace_id == workspace_of(ctx, row)).limit(1)
    )
    return found is not None


async def hold_for_reading(ctx: Context, entrant: uuid.UUID) -> EntrantRow | None:
    """Hold the entrant's row for reading until the unit of work ends, so no
    change of who is in it, or of its extension, passes the caller, and
    answer with it as it stands once held, none once it is gone: what was
    read of it before the hold may have changed while the caller waited.
    """
    return (
        await ctx.db.execute(
            select(EntrantRow)
            .where(EntrantRow.id == entrant)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def ref(ctx: Context, row: EntrantRow) -> EntrantRef:
    """The entrant by its id and the name it is shown under."""
    return EntrantRef(row.id, (await display_names(ctx, [row]))[row.id])


async def display_names(ctx: Context, rows: Collection[EntrantRow]) -> dict[uuid.UUID, str]:
    """The name each entrant is shown under: its own, or the username of its
    member, the latest to leave when nobody is in it now, read from the forge
    once the connection is let go of; `DELETED_USER` for an account that is
    gone, and nothing when the forge did not say.
    """
    found = {row.id: row.name for row in rows if row.name is not None}
    nameless = [row.id for row in rows if row.name is None]
    whose: dict[uuid.UUID, int] = {}
    if nameless:
        members = await ctx.db.execute(
            select(EntrantMember.entrant_id, EntrantMember.user_id)
            .where(EntrantMember.entrant_id.in_(nameless))
            .order_by(
                (EntrantMember.status == MemberStatus.MEMBER).desc(),
                EntrantMember.left_at.desc().nulls_first(),
                EntrantMember.joined_at,
                EntrantMember.id,
            )
        )
        for entrant, member in members:
            whose.setdefault(entrant, member)
    await ctx.let_go()
    usernames: dict[int, str] = {}
    for entrant in nameless:
        user_id = whose.get(entrant)
        if user_id is None:
            found[entrant] = ""
            continue
        if user_id not in usernames:
            usernames[user_id] = await username_of(ctx, user_id)
        found[entrant] = usernames[user_id]
    return {entrant: name or "" for entrant, name in found.items()}


async def username_of(ctx: Context, user_id: int) -> str:
    """Someone's username, `DELETED_USER` once their account is gone, and
    nothing when the forge does not answer.
    """
    try:
        return (await ctx.forge.identity.find_user(user_id)).username
    except NotFound:
        return DELETED_USER
    except PortError:
        return ""
