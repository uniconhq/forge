"""Taking a removed contestant's access to their workspace away. A workspace
is where a contestant works for one contest: a desk their questions live in,
and one place per task to submit to. The place to submit a task is made once
they are approved or at the task's first publication (`places`), or else at
their first upload or submit to it (`submissions`), and its name comes from
the contest and the contestant's user id, so nothing about it is stored.
Closing it takes their access away from whichever parts were made and keeps
what is in them.
"""

from forge.db.tables import Contestant
from forge.domain.errors import NotFound
from forge.domain.ids import ContestId
from forge.domain.names import UserOwner
from forge.log import get_logger
from forge.runtime.context import Context

log = get_logger(__name__)


async def close(ctx: Context, contestant: Contestant) -> None:
    """Take the contestant's access to their workspace away and keep what is
    in it. A contestant whose account is gone has no access left to take.
    """
    workspace = ctx.forge.workspaces.workspace_of(
        ContestId(contestant.contest_id), UserOwner(contestant.user_id)
    )
    try:
        await ctx.forge.workspaces.close_workspace(workspace, [contestant.user_id])
    except NotFound:
        log.info("workspaces.account_gone", user_id=contestant.user_id)
        return
    log.info("workspaces.closed", contest=contestant.contest_id, user_id=contestant.user_id)
