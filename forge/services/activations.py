"""A task's activation at the CI, made once, as the org's own account: the CI
takes the task for grading, is told to trust it for `volumes` and nothing
else, and forgets the event push the activation left behind, since the
platform starts every run itself.

Every publication makes sure of it through `ensure`, and the `provisioning`
row of kind `activation` is its record. An activation made at once is
recorded as made; one that fails does not undo the publication, and is left
to the poller as a waiting row, so `ensure` says it is pending until the
poller has made it. A task with a publication and no record, because the
unit of work that published it never committed, is activated by its next
save.
"""

import contextlib
from typing import Literal

from forge.db.tables import Provisioning
from forge.domain.errors import Conflict, PortError
from forge.domain.ids import OrgName, TaskId
from forge.domain.provisioning import STEPS
from forge.domain.roles import task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import org_accounts, provisioning
from forge.services.credentials import CannotDecrypt
from forge.services.provisioning import Attempt

log = get_logger(__name__)

KIND = "activation"
(STEP,) = STEPS[KIND]

Activation = Literal["done", "pending", "not_needed"]


async def ensure(ctx: Context, task: TaskId) -> Activation:
    """Where the task's activation stands once a publication is made: `done`
    when it was made now, `pending` while it waits for the poller, and
    `not_needed` when an earlier publication made it.
    """
    row = await provisioning.find(ctx, KIND, task)
    if row is not None:
        return "not_needed" if row.status == provisioning.READY else "pending"
    try:
        await activate(ctx, task)
    except (PortError, CannotDecrypt) as exc:
        log.warning("activations.deferred", task=task, error=type(exc).__name__, detail=str(exc))
        with contextlib.suppress(Conflict):
            await provisioning.request(ctx, KIND, task, {})
        return "pending"
    with contextlib.suppress(Conflict):
        await provisioning.record_made(ctx, KIND, task, last_step=STEP)
    return "done"


async def activate(ctx: Context, task: TaskId) -> None:
    """Activate the task as its org's account. Activating twice changes
    nothing at the CI, so a rerun is safe.
    """
    account = await org_accounts.identity(ctx, OrgName(task_scope(task).org))
    await ctx.forge.grading.activate(account, task)
    log.info("activations.activated", task=task)


async def provision(ctx: Context, row: Provisioning) -> None:
    """The poller's work over an activation a publication could not make."""

    async def activated(attempt: Attempt) -> None:
        await activate(ctx, TaskId(row.target_id))

    await provisioning.run(ctx, row, provisioning.steps(KIND, {STEP: activated}))
