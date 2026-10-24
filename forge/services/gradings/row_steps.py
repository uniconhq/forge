import uuid
from collections.abc import Collection, Sequence

from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.gradings import SAVE
from forge.domain.errors import NotFound
from forge.domain.grading import UNFINISHED, GradingStatus
from forge.domain.ids import RunId, TaskId
from forge.domain.roles import Role, holds, task_scope
from forge.runtime.context import Context
from forge.services.access import Organiser, require
from forge.services.gradings.lookups import find, finish, staff_cancelled
from forge.services.gradings.records import NO_SUCH_GRADING
from forge.services.gradings.row_state import overdue_of
from forge.services.gradings.starting import _cancel_quietly, _on_line


def _settle(ctx: Context, row: Grading, gone: Collection[uuid.UUID]) -> None:
    """Write an attempt that reads as a system error while its row says it
    is still being graded, overdue or lost, as one, with why, and stop any
    run of it the CI may still hold once the unit of work has committed.
    """
    stored = GradingStatus(row.status)
    if stored in UNFINISHED:
        finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=overdue_of(ctx, row, gone))
    if row.run_id is not None and stored not in (GradingStatus.DONE, GradingStatus.CANCELLED):
        _cancel_after_commit(ctx, RunId(row.run_id))


def _cancel_after_commit(ctx: Context, run: RunId) -> None:
    """Cancel the run at the CI once the unit of work has committed, holding
    nothing while the CI is called; a run the CI does not stop is refused its
    reports, since its grading is finished.
    """

    async def cancel(after: Context) -> None:
        await _cancel_quietly(after, run)

    ctx.after_commit(cancel)


async def _managed(
    ctx: Context, organiser: Organiser, grading: uuid.UUID, *, lock: bool = True
) -> Grading:
    """The grading, held until the unit of work ends when `lock`, for an
    organiser who manages its task. `NotFound` for one whose task they do
    not observe, and for a save's row, which no organiser acts on yet.
    """
    row = await find(ctx, grading, lock=lock)
    if row is None or row.pipeline == SAVE:
        raise NotFound(NO_SUCH_GRADING)
    scope = task_scope(TaskId(row.task_id))
    if not holds(organiser.grants, scope, Role.OBSERVER):
        raise NotFound(NO_SUCH_GRADING)
    require(organiser, scope, Role.MANAGER)
    return row


async def _line(ctx: Context, row: Grading) -> list[Grading]:
    """Every attempt on `row`'s line, held until the unit of work ends and
    read afresh, in the order a rejudge takes them, so a retry and a rejudge
    never wait on each other in a circle.
    """
    return list(
        (
            await ctx.db.execute(
                select(Grading)
                .where(_on_line(row))
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _regraded(rows: Sequence[Grading]) -> dict[str, Grading]:
    """The latest attempt of each submission `regrade` gives a new one, from
    every attempt to the task in submission and attempt order: all of them
    but one staff cancelled.
    """
    latest: dict[str, Grading] = {}
    for row in rows:
        latest[row.submission_id] = row
    return {submission: row for submission, row in latest.items() if not staff_cancelled(row)}
