"""A task's registration for grading at the CI, made once, as the org's own
account: the CI takes the task, is told to trust it for `volumes` and
nothing else, and forgets the event push the registration left behind,
since the platform starts every run itself.

Every publication makes sure of it through `ensure`, and the `provisioning`
row of kind `registration` is its record. A registration made at once is
recorded as made; one that fails does not undo the publication, and is left
to the poller as a waiting row, so `ensure` says it is pending until the
poller has made it. A task with a publication and no record, because the
unit of work that published it never committed, is registered by its next
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

KIND = "registration"
(STEP,) = STEPS[KIND]

Registration = Literal["done", "pending", "not_needed"]


async def ensure(ctx: Context, task: TaskId) -> Registration:
    """Where the task's registration stands once a publication is made:
    `done` when it was made now, `pending` while it waits for the poller,
    and `not_needed` when an earlier publication made it.
    """
    row = await provisioning.find(ctx, KIND, task)
    if row is not None:
        return "not_needed" if row.status == provisioning.READY else "pending"
    try:
        await register(ctx, task)
    except (PortError, CannotDecrypt) as exc:
        log.warning("registrations.deferred", task=task, error=type(exc).__name__, detail=str(exc))
        with contextlib.suppress(Conflict):
            await provisioning.request(ctx, KIND, task, {})
        return "pending"
    with contextlib.suppress(Conflict):
        await provisioning.record_made(ctx, KIND, task, last_step=STEP)
    return "done"


async def register(ctx: Context, task: TaskId) -> None:
    """Register the task as its org's account. Registering twice changes
    nothing at the CI, so a rerun is safe.
    """
    account = await org_accounts.identity(ctx, OrgName(task_scope(task).org))
    await ctx.forge.grading.register(account, task)
    log.info("registrations.registered", task=task)


async def provision(ctx: Context, row: Provisioning) -> None:
    """The poller's work over a registration a publication could not make."""

    async def registered(attempt: Attempt) -> None:
        await register(ctx, TaskId(row.target_id))

    await provisioning.run(ctx, row, provisioning.steps(KIND, {STEP: registered}))
