
from sqlalchemy import select

from forge.db.tables import Grading, Regrade
from forge.db.tables.gradings import FRONT, LATE
from forge.domain.grading import AT_THE_CI, UNFINISHED, GradingStatus
from forge.domain.ids import PublicationId, RunId, TaskId
from forge.runtime.context import Context
from forge.services.gradings.lookups import finish
from forge.services.gradings.row_steps import _cancel_after_commit, _regraded
from forge.services.gradings.starting import _superseded, new_row


async def follow(ctx: Context, early: Grading) -> Grading | None:
    """The late row of a run task's early row that has just ended `done`,
    none when a later early attempt of its submission is already made: the
    next attempt on the submission's late line, against the early row's
    publication and in its regrade, each late row still unfinished stopped
    first, and started in the background, so the report that calls this
    waits on no start. The late line is held after the early row the report
    holds, the order a regrade holds them in, so the two never wait on each
    other in a circle.
    """
    if await _superseded(ctx, early):
        return None
    lates = list(
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.submission_id == early.submission_id, Grading.pipeline == LATE)
                .order_by(Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    for row in lates:
        if GradingStatus(row.status) in UNFINISHED:
            stop(ctx, row)
    return new_row(
        ctx,
        task=TaskId(early.task_id),
        workspace=early.workspace_id,
        submission=early.submission_id,
        number=early.submission_number,
        version=early.submission_version,
        submitted_at=early.submitted_at,
        publication=PublicationId(early.publication_id),
        attempt=max((row.attempt for row in lates), default=0) + 1,
        key=None,
        submitted_by=early.submitted_by,
        regrade=early.regrade_id,
        pipeline=LATE,
        early=early.id,
        released_split=early.released_split,
        unwaited=True,
    )


def stop(ctx: Context, row: Grading) -> None:
    """Cancel an unfinished grading a rejudge, a later late row or a later
    save replaces, and its run at the CI once that has committed; a run the
    CI does not stop is refused its reports, since the grading is cancelled.
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
