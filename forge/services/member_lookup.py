"""Who is in an entrant, read from outside the entrants service.

`release` and `places` need this and the entrants service needs them, so
the lookup lives here and neither side imports the other for it.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import EntrantMember
from forge.domain.entrants import MemberStatus
from forge.domain.ids import ContestId, WorkspaceId
from forge.log import get_logger
from forge.runtime.context import Context

log = get_logger(__name__)


async def of_person(ctx: Context, contest: ContestId, user_id: int) -> EntrantRow | None:
    """The entrant the person is in now, whatever its status, or none."""
    return (
        await ctx.db.execute(
            select(EntrantRow)
            .join(EntrantMember, EntrantMember.entrant_id == EntrantRow.id)
            .where(
                EntrantMember.contest_id == contest,
                EntrantMember.user_id == user_id,
                EntrantMember.status == MemberStatus.MEMBER,
            )
        )
    ).scalar_one_or_none()


async def member_ids(ctx: Context, entrant: uuid.UUID) -> list[int]:
    """The entrant's members now, the earliest joined first."""
    found = await ctx.db.scalars(
        select(EntrantMember.user_id)
        .where(EntrantMember.entrant_id == entrant, EntrantMember.status == MemberStatus.MEMBER)
        .order_by(EntrantMember.joined_at, EntrantMember.id)
    )
    return list(found)


async def settle(
    ctx: Context, entrant: uuid.UUID, workspace: WorkspaceId, made_with: Sequence[int]
) -> tuple[int, ...]:
    """After a part of the entrant's workspace was made with `made_with` and
    no lock held, hold the entrant and, when its members have changed
    meanwhile, make exactly the members now the people who reach every part
    of it, so someone who joined is added and someone who left, or was left
    behind by a change that failed halfway, is taken off. The members now,
    none for an entrant that is gone.
    """
    held = (
        await ctx.db.execute(
            select(EntrantRow)
            .where(EntrantRow.id == entrant)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current = tuple(await member_ids(ctx, entrant)) if held is not None else ()
    if set(current) != set(made_with):
        await ctx.forge.workspaces.share_workspace(workspace, current)
        log.info("entrants.access_settled", entrant=str(entrant), members=list(current))
    return current
