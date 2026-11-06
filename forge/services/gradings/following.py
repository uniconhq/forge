from sqlalchemy import select

from forge.db.tables import Grading, Regrade
from forge.db.tables.gradings import FRONT
from forge.domain.grading import AT_THE_CI, GradingStatus
from forge.domain.ids import PublicationId, RunId, TaskId
from forge.runtime.context import Context
from forge.services.gradings.lookups import finish
from forge.services.gradings.row_steps import _cancel_after_commit, _regraded


def stop(ctx: Context, row: Grading) -> None:
    """Cancel an unfinished grading a rejudge or a later save replaces, and
    its run at the CI once that has committed; a run the CI does not stop is
    refused its reports, since the grading is cancelled.
    """
    if GradingStatus(row.status) in AT_THE_CI and row.run_id is not None:
        _cancel_after_commit(ctx, RunId(row.run_id))
    finish(ctx, row, GradingStatus.CANCELLED)


async def behind(ctx: Context, task: TaskId, publication: PublicationId) -> bool:
    """Whether the task's submissions were never graded again against
    `publication`: it has no regrade recorded, and a submission's latest
    attempt that `regrade` would grade again grades against another. So a
    save that published a change to how the task grades and stopped before
    its regrade is told apart from one whose regrade found nothing to do,
    and from submissions made since, already graded against it.
    """
    recorded = await ctx.db.scalar(
        select(Regrade.id)
        .where(Regrade.task_id == task, Regrade.publication_id == publication)
        .limit(1)
    )
    if recorded is not None:
        return False
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task, FRONT)
                .order_by(Grading.submission_id, Grading.attempt)
            )
        )
        .scalars()
        .all()
    )
    return any(row.publication_id != publication for row in _regraded(rows).values())
