import uuid
from collections.abc import Collection, Iterable

from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.gradings import EARLY, LATE
from forge.domain.grading import UNFINISHED, GradingStatus
from forge.domain.ids import PublicationId, TaskId
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import timelines
from forge.services.gradings import attempt_history
from forge.services.gradings.lookups import find
from forge.services.gradings.records import NOTICED_KEPT, START_RETRY_WAIT
from forge.services.gradings.room import _over_limit
from forge.services.gradings.row_state import lost, status_of
from forge.services.gradings.row_steps import _line, _settle

log = get_logger(__name__)


async def notice(ctx: Context, rows: Iterable[Grading], lost: Collection[uuid.UUID] = ()) -> None:
    """Start, once this unit of work commits, the retry the platform makes by
    itself of each of `rows`, the latest attempts of their submissions, that
    reads as a system error now and was not itself such a retry, and, for
    one whose run could not be started, failed `START_RETRY_WAIT` ago or
    more (`start_waits`). Each is started once by this process in
    `NOTICED_KEPT` however many readers find it, and `retry_by_itself`
    makes it once whatever any other process does. Nothing is started
    while `retry_failed` is off.
    """
    if not ctx.settings.retry_failed:
        return
    for row in rows:
        if row.automatic or status_of(ctx, row, lost) is not GradingStatus.SYSTEM_ERROR:
            continue
        if start_waits(ctx, row):
            continue
        if not await _first_to_notice(ctx, row.id):
            continue
        grading = row.id

        async def retry_it(later: Context, grading: uuid.UUID = grading) -> None:
            await retry_by_itself(later, grading)

        ctx.in_background(retry_it)


async def notice_late(ctx: Context, fronts: Iterable[Grading]) -> None:
    """The lost check and the retry by itself (`notice`) over the latest
    late row of each submission of `fronts`, the early rows a read finds:
    no entrant reads a late row, so whatever reads its early row sees to it
    as it does to that one.
    """
    submissions = sorted({row.submission_id for row in fronts})
    if not submissions:
        return
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.submission_id.in_(submissions), Grading.pipeline == LATE)
                .order_by(Grading.submission_id, Grading.attempt.desc())
                .distinct(Grading.submission_id)
            )
        )
        .scalars()
        .all()
    )
    if rows:
        await notice(ctx, rows, await lost(ctx, rows))


def start_waits(ctx: Context, row: Grading) -> bool:
    """Whether a grading whose run could not be started (a system error
    with no run) failed less than `START_RETRY_WAIT` ago, so its retry by
    itself waits. A failure the CI reported, and an overdue or lost run,
    wait for nothing.
    """
    return (
        row.status == GradingStatus.SYSTEM_ERROR
        and row.run_id is None
        and row.finished_at is not None
        and ctx.now < row.finished_at + START_RETRY_WAIT
    )


async def _first_to_notice(ctx: Context, grading: uuid.UUID) -> bool:
    """Whether this reader is the first in this process, in `NOTICED_KEPT`,
    to find the grading failed: the first one's claim is kept, and every
    later reader is handed it and not its own.
    """
    mine = object()

    async def claim() -> object:
        return mine

    return await ctx.memo.remembered(f"gradings.noticed.{grading}", NOTICED_KEPT, claim) is mine


async def retry_by_itself(ctx: Context, grading: uuid.UUID) -> Grading | None:
    """The retry the platform makes by itself of a grading that ended in
    `system_error`, a new attempt marked `automatic` against the publication
    it graded against, or none when there is nothing to retry: the grading
    is not the latest attempt of its submission any more, does not read as a
    system error, its run could not be started and it still waits
    (`start_waits`), staff ended it, it was itself such a retry, or another
    attempt is still being graded; or, for a late row, its early row is no
    longer its submission's latest early attempt, the late row settled then
    if it still waits. The task and the submission's attempts are held as a
    staff retry holds them, so two of these at once, or one beside a staff
    retry, a cancel or a rejudge, make one attempt between them. One that
    reads failed only because it is overdue or lost is ended first, with the
    reason written on its row, its run cancelled at the CI once this
    commits. A submission with no result is graded again only while its
    entrant has room under the task's `submissions.max`, and otherwise its
    new attempt ends `over_limit` (`_over_limit`). The new attempt belongs
    to the regrade the failed one belonged to.
    """
    found = await find(ctx, grading)
    if found is None or found.automatic:
        return None
    await timelines.hold_task(ctx, TaskId(found.task_id))
    gone = await lost(ctx, [found])
    attempts = await _line(ctx, found)
    row = next(attempt for attempt in attempts if attempt.id == grading)
    if row.automatic or any(other.attempt > row.attempt for other in attempts):
        return None
    if status_of(ctx, row, gone) is not GradingStatus.SYSTEM_ERROR or start_waits(ctx, row):
        return None
    if row.pipeline == LATE and await _follows_an_earlier_attempt(ctx, row):
        _settle(ctx, row, gone)
        log.info("gradings.late_left", grading=str(row.id))
        return None
    if any(status_of(ctx, other, gone) in UNFINISHED for other in attempts):
        return None
    over = await _over_limit(ctx, row, attempts, gone)
    _settle(ctx, row, gone)
    made = attempt_history._next_attempt(
        ctx, row, PublicationId(row.publication_id), attempts, automatic=True, over_limit=over
    )
    await ctx.db.flush()
    log.info(
        "gradings.retried_by_itself",
        grading=str(row.id),
        attempt=made.attempt,
        over_limit=over is not None,
    )
    return made


async def _follows_an_earlier_attempt(ctx: Context, late: Grading) -> bool:
    """Whether a late row follows an early row that is no longer its
    submission's latest early attempt, since a rejudge or a save's regrade
    made another: its runs are read by no one, so it is not tried again.
    """
    latest = await ctx.db.scalar(
        select(Grading.id)
        .where(Grading.submission_id == late.submission_id, Grading.pipeline == EARLY)
        .order_by(Grading.attempt.desc())
        .limit(1)
    )
    return latest != late.early_id
