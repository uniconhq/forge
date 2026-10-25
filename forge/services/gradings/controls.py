import uuid

from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.errors import Conflict, NotFound, WrongStatus
from forge.domain.grading import AT_THE_CI, FINISHED, UNFINISHED, GradingStatus, cancel_reason
from forge.domain.ids import PublicationId, RunId, TaskId
from forge.domain.regrades import RegradeCause
from forge.domain.roles import Role, task_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrant_view, timelines
from forge.services.access import Organiser, require
from forge.services.gradings import attempt_history, regrading
from forge.services.gradings.attempt_history import record
from forge.services.gradings.lookups import finish, staff_cancelled
from forge.services.gradings.records import GradingRecord, Rejudged
from forge.services.gradings.room import _over_limit
from forge.services.gradings.row_state import (
    latest_with_result,
    lost,
    overdue_of,
    previous_with_result,
    status_of,
)
from forge.services.gradings.row_steps import _cancel_after_commit, _line, _managed, _settle

log = get_logger(__name__)


@action
async def cancel(
    ctx: Context, organiser: Organiser, grading: uuid.UUID, reason: str
) -> GradingRecord:
    """End a submission whose latest grading reads as a system error by
    cancelling that grading, with `reason`, a sentence its contestant reads.
    One that reads so only because it is overdue or lost has that written
    on its row as its error, and its run at the CI is stopped once the
    cancel has committed, holding nothing while the CI is called. A
    submission an earlier attempt of which finished with a result keeps
    counting as that result; one with no such attempt is void.
    `InvalidReason` for an empty sentence or one over `CANCEL_REASON_MAX`
    characters, `WrongStatus` for a grading that is not a system error, and
    `Conflict` for one with a later attempt, which is the one to cancel. The
    task is held alone, as by a retry, so a retry by itself under way lands
    its attempt first and the cancel finds it.
    """
    sentence = cancel_reason(reason)
    found = await _managed(ctx, organiser, grading, lock=False)
    await timelines.hold_task(ctx, TaskId(found.task_id))
    gone = await lost(ctx, [found])
    attempts = await _line(ctx, found)
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status is not GradingStatus.SYSTEM_ERROR:
        raise WrongStatus(
            f"Only a grading that failed with a system error can be cancelled, and this one is "
            f"{status.value}.",
            current=status.value,
        )
    if any(other.attempt > row.attempt for other in attempts):
        raise Conflict("This submission has a later attempt. Cancel that one instead.")
    stored = GradingStatus(row.status)
    if stored in AT_THE_CI and row.run_id is not None:
        _cancel_after_commit(ctx, RunId(row.run_id))
    finish(ctx, row, GradingStatus.CANCELLED, error=overdue_of(ctx, row, gone) or row.error)
    row.cancel_reason = sentence
    await ctx.db.flush()
    log.info("gradings.cancelled", grading=str(row.id), user_id=organiser.user.id)
    good = previous_with_result(attempts, row)
    return record(ctx, row, gone, latest=True, last_good=good.attempt if good else None)


@action
async def retry(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> GradingRecord:
    """A new attempt of a submission's latest grading once it is finished,
    against the publication it graded against, the old attempt kept as it
    is. `WrongStatus` for one that is not finished and for a submission
    staff cancelled, which that ends, and `Conflict` for one with a later
    attempt, which is the one to retry, or while another attempt of it is
    graded. The task is held alone, as by a save and a rejudge, so no
    regrade numbers an attempt from rows read before this one landed. A
    submission with no result, delayed or over the limit, is graded again
    only while its entrant has room under the task's `submissions.max`, and
    otherwise the new attempt ends `over_limit` at once, as the grading's
    `retry_limit` said it would; no one grades one past the most. The new
    attempt belongs to the regrade the old one belonged to.
    """
    found = await _managed(ctx, organiser, grading, lock=False)
    await timelines.hold_task(ctx, TaskId(found.task_id))
    gone = await lost(ctx, [found])
    attempts = await _line(ctx, found)
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status not in FINISHED:
        raise WrongStatus(
            f"This grading is {status.value} and has not finished, so it cannot be retried yet.",
            current=status.value,
        )
    if staff_cancelled(row):
        raise WrongStatus(
            "The organisers cancelled this submission, which ends it, so it is not graded again.",
            current=status.value,
        )
    if any(other.attempt > row.attempt for other in attempts):
        raise Conflict("This submission has a later attempt. Retry that one instead.")
    if any(status_of(ctx, other, gone) in UNFINISHED for other in attempts):
        raise Conflict(
            "Another attempt of this submission is still being graded. Wait for it to finish."
        )
    over = await _over_limit(ctx, row, attempts, gone)
    _settle(ctx, row, gone)
    made = attempt_history._next_attempt(
        ctx, row, PublicationId(row.publication_id), attempts, over_limit=over
    )
    await ctx.db.flush()
    log.info(
        "gradings.retried", grading=str(row.id), attempt=made.attempt, over_limit=over is not None
    )
    good = latest_with_result(attempts)
    return record(ctx, made, latest=True, last_good=good.attempt if good else None)


async def free_user(ctx: Context, user: int) -> bool:
    """Free `user`, a row's (`pipelines`), from the row not
    finished that holds it when that row is past what its state may take
    (`overdue_of`): it is written as a system error, with why, and its run
    stopped once the unit of work commits, as a retry or a cancel settles
    it. Without this, a row nobody reads again, such as an automatic retry
    whose run the CI lost, a late row after its contest, or the save row of
    a task since removed, would hold its user for good. A row another unit
    of work holds is left to it. Whether the user is free now.
    """
    holder = await ctx.db.scalar(
        select(Grading)
        .where(Grading.run_user == user, Grading.status.in_([each.value for each in UNFINISHED]))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if holder is None:
        return True
    if overdue_of(ctx, holder) is None:
        return False
    _settle(ctx, holder, ())
    await ctx.db.flush()
    log.info("gradings.user_freed", grading=str(holder.id), user=user)
    return True


@action
async def rejudge(ctx: Context, organiser: Organiser, task: TaskId) -> Rejudged:
    """A new attempt of every submission's latest attempt, against the
    task's current publication. `NotFound` for a task with no publication.
    The task is held alone, as a save holds it, so a save's regrade and
    this one each start from every attempt the other made. It is recorded
    as a regrade of many, a `rejudge` by the organiser.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    await timelines.hold_task(ctx, task)
    current = await entrant_view.task(ctx, task)
    if current is None:
        raise NotFound(
            "The task has no publication to grade against yet. Save it so that it publishes."
        )
    return await regrading.regrade(
        ctx,
        task,
        current.publication.id,
        cause=RegradeCause.REJUDGE,
        by=organiser.user.id,
        limit=current.definition.submissions.max,
    )
