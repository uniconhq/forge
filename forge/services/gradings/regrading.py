from sqlalchemy import select

from forge.db.tables import Grading, Regrade
from forge.db.tables.gradings import FRONT, LATE
from forge.domain.grading import UNFINISHED, GradingStatus
from forge.domain.ids import PublicationId, RunId, TaskId, new_id
from forge.domain.regrades import RegradeCause
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrant_view
from forge.services.gradings import attempt_history
from forge.services.gradings.following import stop
from forge.services.gradings.lookups import finish
from forge.services.gradings.records import Rejudged
from forge.services.gradings.room import _without_room, task_limit
from forge.services.gradings.row_state import lost, overdue_of, status_of
from forge.services.gradings.row_steps import _cancel_after_commit, _regraded

log = get_logger(__name__)


async def to_regrade(ctx: Context, task: TaskId, *, limit: int | None = None) -> int:
    """How many submissions `regrade` would grade again against a publication
    not made yet, chosen as it chooses them: each submission's latest
    attempt, but one staff cancelled, and but a delayed or over-the-limit
    one whose entrant has no room left under a `submissions.max` of `limit`,
    the task's latest publication's when none is given. What a save's
    confirmation says it will cost.
    """
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
    regraded = _regraded(rows)
    if limit is None:
        limit = await task_limit(ctx, task)
    if limit is None:
        return len(regraded)
    attempts: dict[str, list[Grading]] = {}
    for row in rows:
        attempts.setdefault(row.submission_id, []).append(row)
    gone = await lost(ctx, rows)
    return len(regraded) - len(_without_room(ctx, regraded, attempts, gone, limit))


async def regrade(
    ctx: Context,
    task: TaskId,
    publication: PublicationId,
    *,
    cause: RegradeCause,
    by: int | None,
    limit: int,
) -> Rejudged:
    """A new attempt of every submission's latest attempt to the task,
    against `publication`, for a rejudge and for a save that publishes a
    change to how the task grades, `cause` saying which and `by` naming the
    organiser who asked. A latest attempt still being graded
    against another publication is cancelled first, and one being graded
    against `publication` is left to finish; one that reads as finished only
    because it is overdue or lost is ended first, its old run cancelled at
    the CI once this has committed. A submission staff cancelled, its latest
    attempt `cancelled` with a sentence, is left as it is.

    A submission with no result, delayed or over the limit, is graded again
    only while its entrant has room under a `submissions.max` of `limit`,
    each entrant's taken in the order they were made: a delayed one with
    no room gets an attempt ended `over_limit` at once, and one already
    over the limit with none is left as it is. Every attempt made belongs to
    one regrade of many recorded for it, none when it makes none.

    A task is graded again on its early rows alone: each new early row
    makes its late row when it ends `done`, and the store runs again only
    what changed (11.6, 11.8). A late row still unfinished is stopped when
    its submission is graded again or it grades against another
    publication, so no late row of an old grading runs on. Each new early
    row takes the split of the task's results as they stand now
    (`published.results_released`), and its late row the same.
    """
    # Which runs the CI has lost is asked before any row is locked, since
    # asking takes the CI's time; the task itself is held by the caller.
    gone = await lost(
        ctx,
        (await ctx.db.execute(select(Grading).where(Grading.task_id == task, FRONT)))
        .scalars()
        .all(),
    )
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task, FRONT)
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # A task's late rows are held after its early rows, as `follow` holds
    # them after the early row it follows.
    lates = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(
                    Grading.task_id == task,
                    Grading.pipeline == LATE,
                    Grading.status.in_([status.value for status in UNFINISHED]),
                )
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    attempts: dict[str, list[Grading]] = {}
    for row in rows:
        attempts.setdefault(row.submission_id, []).append(row)
    released = await entrant_view.results_released(ctx, task)
    regraded = _regraded(rows)
    refused = _without_room(ctx, regraded, attempts, gone, limit)
    made: list[tuple[str, Grading]] = []
    left_running = 0
    for submission, row in regraded.items():
        status = status_of(ctx, row, gone)
        if status in UNFINISHED and row.publication_id == publication:
            left_running += 1
        elif not (submission in refused and status is GradingStatus.OVER_LIMIT):
            made.append((submission, row))
    again = {submission for submission, _ in made}
    for late in lates:
        if late.submission_id in again or late.publication_id != publication:
            stop(ctx, late)
    if not made:
        log.info("gradings.rejudged", task=task, publication=publication, queued=0)
        return Rejudged(task, publication, 0, 0, left_running)
    recorded = Regrade(
        id=new_id(),
        task_id=task,
        publication_id=publication,
        cause=cause,
        started_by=by,
        started_at=ctx.now,
    )
    ctx.db.add(recorded)
    queued = cancelled = over_limit = 0
    for submission, row in made:
        if status_of(ctx, row, gone) in UNFINISHED:
            stop(ctx, row)
            cancelled += 1
        elif GradingStatus(row.status) in UNFINISHED:
            finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=overdue_of(ctx, row, gone))
            if row.run_id is not None:
                _cancel_after_commit(ctx, RunId(row.run_id))
        over = limit if submission in refused else None
        attempt_history._next_attempt(
            ctx,
            row,
            publication,
            attempts[submission],
            regrade=recorded.id,
            over_limit=over,
            released_split=released,
        )
        if over is None:
            queued += 1
        else:
            over_limit += 1
    await ctx.db.flush()
    log.info(
        "gradings.rejudged",
        task=task,
        publication=publication,
        regrade=str(recorded.id),
        cause=cause.value,
        queued=queued,
        cancelled=cancelled,
        left_running=left_running,
        over_limit=over_limit,
    )
    return Rejudged(
        task, publication, queued, cancelled, left_running, over_limit, regrade=recorded.id
    )
