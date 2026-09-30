"""Turning queued gradings into runs at the CI, and noticing runs that died.

The `gradings.dispatch` poller takes `queued` and `dispatching` rows whose
`retry_at` has come, oldest in the queue first, each under
`FOR UPDATE SKIP LOCKED`, so two processes never start one run twice. For
each, as the org's own account:

1. it looks for a run of the task already carrying the grading's id, started
   since the grading entered the queue and not ended, which is what a start
   whose answer was lost left behind;
2. finding none, it starts one on the task, with the run's variables, pinned
   to `pool:platform`, so only a machine the platform controls takes it;
3. it records the run, when it was started and the start's deadline, one run
   timeout later, and the row is `dispatched`, waiting for a machine.

Anything but a run coming back is a failed start: the row goes back to
`queued` with a `wait_reason` an organiser reads, or stays `dispatching`
when the start was sent and no answer came back, since that start may have
made a run the next try finds; either way it is tried again after a wait
that doubles from five seconds up to five minutes. The CI answers a start
with an empty 204 when the platform's extension would not say what the run
is, and keeps a run of it that ended at once; the next try passes over such
a run; a grading whose start has failed 20 times, more than an hour of
tries, and is then answered that way ends in `system_error`. A grading whose
publication is gone cannot be run at all and ends in `system_error` too.

The `gradings.overdue` pass looks every minute at the runs the CI holds
whose deadline has passed. A run the CI reports ended, or no longer has, died
without a verdict. A run still going past a running grading's deadline can
report nothing any more, since its token is refused, so it is cancelled at
the CI and died the same way. A run the CI has not handed to a machine yet,
or is still checking out, is looked at again five minutes later. A grading
whose run died goes back to the queue once, to get a fresh machine, and the
second time ends in `system_error`. Its next run is handed new secrets, so
the run given up on can neither fetch the next one's envelope nor report for
it.
"""

import contextlib
from datetime import datetime, timedelta

from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.errors import Forbidden, NotFound, PortError, Rejected, Unavailable
from forge.domain.grading import (
    AT_THE_CI,
    ENDED,
    FIND_MARGIN,
    PENDING_RECHECK,
    RUN_TIMEOUT,
    WAITING,
    GradingStatus,
    RunStatus,
    start_retry_wait,
)
from forge.domain.ids import RunId
from forge.log import get_logger
from forge.runtime.background import Poller
from forge.runtime.context import Context
from forge.services import gradings, org_accounts
from forge.services.credentials import CannotDecrypt

log = get_logger(__name__)

POLL_INTERVAL = timedelta(seconds=2)
OVERDUE_INTERVAL = timedelta(minutes=1)
OVERDUE_BATCH = 50
REQUEUES = 1
REFUSALS = 20

WAITING_FOR_MACHINE = "Waiting for a grading machine to take its run."
CHECKING_OUT = "Its run is checking out the task and the submission."
REQUEUED = "Its run ended without a verdict; it is being tried once more."
ACCOUNT_NOT_READY = "The org's grading account is not ready yet."
NOT_ACTIVATED = "The task is not taken for grading at the CI yet."
REFUSED = "The CI refused the org's grading account."
NO_RUN = "The CI answered without starting a run."
NO_ANSWER = "The CI did not answer."
UNEXPECTED = "Something went wrong starting its run."
PUBLICATION_GONE = "The publication it grades against is gone."
DIED_TWICE = "Its run ended without a verdict twice."
GAVE_UP = "The CI answered its start without a run 20 times."


def poller(interval: timedelta = POLL_INTERVAL) -> Poller:
    """The poller over the gradings whose run is still to be started."""
    return Poller(
        "gradings.dispatch",
        Grading,
        Grading.status,
        [status.value for status in WAITING],
        dispatch,
        failed,
        interval=interval,
        due=Grading.retry_at,
        order=Grading.queued_at,
    )


async def dispatch(ctx: Context, row: Grading) -> None:
    """Start the grading's run, or find the one a lost answer left, and
    record it; or record why not and when it is tried again.
    """
    try:
        account = await org_accounts.identity(ctx, gradings.org_of(row))
    except (NotFound, CannotDecrypt) as exc:
        log.warning("dispatch.no_account", grading=str(row.id), error=type(exc).__name__)
        _wait(row, ACCOUNT_NOT_READY, ctx.now)
        return
    try:
        run = await gradings.run_of(ctx, row)
    except NotFound:
        log.warning("dispatch.publication_gone", grading=str(row.id), task=row.task_id)
        gradings.finish(row, GradingStatus.SYSTEM_ERROR, ctx.now, error=PUBLICATION_GONE)
        return
    except PortError as exc:
        log.warning("dispatch.forge_unavailable", grading=str(row.id), error=type(exc).__name__)
        _wait(row, NO_ANSWER, ctx.now)
        return
    sent = False
    try:
        earlier = await ctx.forge.grading.find_run(account, run, since=row.queued_at - FIND_MARGIN)
        if earlier is not None and earlier.id != row.run_id and earlier.status not in ENDED:
            found = earlier.id
            log.info("dispatch.found", grading=str(row.id), run=found)
        else:
            sent = True
            found = await ctx.forge.grading.start_run(account, run)
    except PortError as exc:
        log.warning(
            "dispatch.start_failed",
            grading=str(row.id),
            task=row.task_id,
            sent=sent,
            error=type(exc).__name__,
            detail=exc.detail,
        )
        if sent:
            lost = isinstance(exc, Unavailable)
            row.status = GradingStatus.DISPATCHING if lost else GradingStatus.QUEUED
        _wait(row, _reason(exc), ctx.now)
        if isinstance(exc, Rejected) and row.start_failures >= REFUSALS:
            log.warning("dispatch.given_up", grading=str(row.id), failures=row.start_failures)
            gradings.finish(row, GradingStatus.SYSTEM_ERROR, ctx.now, error=GAVE_UP)
        return
    row.status = GradingStatus.DISPATCHED
    row.run_id = found
    row.dispatched_at = ctx.now
    row.deadline_at = ctx.now + RUN_TIMEOUT
    row.wait_reason = WAITING_FOR_MACHINE
    row.retry_at = None
    row.start_failures = 0
    log.info("dispatch.dispatched", grading=str(row.id), run=found)


def failed(row: Grading, exc: Exception, now: datetime) -> None:
    """What the poller records when the work itself raised: the row waits and
    is tried again, as after a failed start.
    """
    log.warning("dispatch.work_failed", grading=str(row.id), error=type(exc).__name__)
    _wait(row, UNEXPECTED, now)


def _wait(row: Grading, reason: str, now: datetime) -> None:
    row.start_failures = row.start_failures + 1
    row.wait_reason = reason
    row.retry_at = now + start_retry_wait(row.start_failures)


def _reason(exc: PortError) -> str:
    match exc:
        case Rejected():
            return NO_RUN
        case NotFound():
            return NOT_ACTIVATED
        case Forbidden():
            return REFUSED
    return NO_ANSWER


async def overdue(ctx: Context) -> int:
    """A timed pass over the runs the CI holds whose deadline has passed.
    Returns how many gradings it found dead.
    """
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(
                    Grading.status.in_([status.value for status in AT_THE_CI]),
                    Grading.deadline_at <= ctx.now,
                )
                .order_by(Grading.deadline_at)
                .limit(OVERDUE_BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    dead = 0
    for row in rows:
        try:
            if await _is_dead(ctx, row):
                died(ctx, row)
                dead += 1
        except PortError as exc:
            log.warning("dispatch.overdue_unread", grading=str(row.id), error=type(exc).__name__)
    await ctx.db.flush()
    if dead:
        log.info("dispatch.overdue", dead=dead)
    return dead


async def _is_dead(ctx: Context, row: Grading) -> bool:
    """Whether the overdue grading's run died; one that did not is looked at
    again later.
    """
    if row.run_id is None:
        return True
    try:
        state = await ctx.forge.grading.read_run(RunId(row.run_id))
    except NotFound:
        return True
    if state.status in ENDED:
        return True
    if row.status == GradingStatus.RUNNING:
        with contextlib.suppress(NotFound):
            await ctx.forge.grading.cancel_run(RunId(row.run_id))
        return True
    row.deadline_at = ctx.now + PENDING_RECHECK
    row.wait_reason = WAITING_FOR_MACHINE if state.status is RunStatus.PENDING else CHECKING_OUT
    return False


def died(ctx: Context, row: Grading) -> None:
    """A run of the grading ended without a verdict: back to the queue the
    first time, with the next run's token, and `system_error` the second.
    """
    if row.requeues >= REQUEUES:
        log.warning("dispatch.died_twice", grading=str(row.id), run=row.run_id)
        gradings.finish(row, GradingStatus.SYSTEM_ERROR, ctx.now, error=DIED_TWICE)
        return
    log.warning("dispatch.requeued", grading=str(row.id), run=row.run_id)
    row.status = GradingStatus.QUEUED
    row.requeues = row.requeues + 1
    gradings.renew_token(ctx, row)
    row.queued_at = ctx.now
    row.wait_reason = REQUEUED
    row.retry_at = None
    row.start_failures = 0
    row.dispatched_at = None
    row.started_at = None
    row.deadline_at = None
    row.progress = None
