import uuid
from collections.abc import Collection, Iterable
from datetime import datetime

from sqlalchemy import ColumnElement, and_, not_, or_

from forge.db.tables import Grading
from forge.domain.errors import NotFound, PortError
from forge.domain.grading import (
    LOST_CHECK_AFTER,
    MACHINE_WAIT,
    START_WAIT,
    UNFINISHED,
    GradingStatus,
    RunState,
    overdue,
    worth_asking,
)
from forge.domain.ids import RunId, TaskId
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services.gradings.lookups import find
from forge.services.gradings.records import NO_SUCH_GRADING, RUN_STATE_KEPT

log = get_logger(__name__)


def has_result(row: Grading) -> bool:
    """Whether an attempt finished with a result, one its submission may
    count: `GOOD` told on a row in hand.
    """
    return row.status == GradingStatus.DONE and row.result is not None


HAS_RESULT = and_(Grading.status == GradingStatus.DONE, Grading.result.is_not(None))
"""`good` told in a query."""


def latest_with_result(attempts: Iterable[Grading]) -> Grading | None:
    """The latest of one submission's attempts that finished with a result,
    the one the submission counts, or none while none has.
    """
    return max(
        (other for other in attempts if has_result(other)),
        key=lambda other: other.attempt,
        default=None,
    )


def previous_with_result(attempts: Iterable[Grading], row: Grading) -> Grading | None:
    """The latest attempt of `row`'s submission before it that finished
    with a result, or none.
    """
    return latest_with_result(other for other in attempts if other.attempt < row.attempt)


def overdue_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> str | None:
    """Why the grading is past what its state may take, or none. `lost`
    names the gradings whose runs the CI was found to have lost.
    """
    return overdue(
        GradingStatus(row.status),
        created_at=row.queued_at,
        dispatched_at=row.dispatched_at,
        deadline=row.deadline_at,
        now=ctx.now,
        lost=row.id in lost,
    )


def status_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> GradingStatus:
    """Where the grading stands now: one past what its state may take, or
    whose run the CI has lost, is a system error (`grading.overdue`).
    """
    if overdue_of(ctx, row, lost) is not None:
        return GradingStatus.SYSTEM_ERROR
    return GradingStatus(row.status)


def _overdue(now: datetime) -> ColumnElement[bool]:
    """The rows `overdue` finds past what their state may take at `now`,
    told in the query: `queued` `START_WAIT` after it was made,
    `dispatched` `MACHINE_WAIT` after its run was started, and `running`
    past its deadline.
    """
    return or_(
        and_(Grading.status == GradingStatus.QUEUED, Grading.queued_at <= now - START_WAIT),
        and_(
            Grading.status == GradingStatus.DISPATCHED,
            Grading.dispatched_at.is_not(None),
            Grading.dispatched_at <= now - MACHINE_WAIT,
        ),
        and_(
            Grading.status == GradingStatus.RUNNING,
            Grading.deadline_at.is_not(None),
            Grading.deadline_at <= now,
        ),
    )


def _worth_asking(now: datetime) -> ColumnElement[bool]:
    """The rows `lost` asks the CI about at `now`, told in the query as
    `worth_asking` tells them: `dispatched` with a run, `LOST_CHECK_AFTER`
    after it was started and not yet `MACHINE_WAIT`.
    """
    return and_(
        Grading.status == GradingStatus.DISPATCHED,
        Grading.run_id.is_not(None),
        Grading.dispatched_at.is_not(None),
        Grading.dispatched_at <= now - LOST_CHECK_AFTER,
        Grading.dispatched_at > now - MACHINE_WAIT,
    )


def _may_read_as(status: GradingStatus, now: datetime) -> ColumnElement[bool]:
    """The rows that may read as `status` at `now`, as `status_of` reads
    them, all but whether the CI lost a run told in the query: for
    `system_error`, the rows stored so, overdue, or whose run the CI is to
    be asked about; for an unfinished status, the rows stored so and not
    overdue, a lost one among them left out once the CI is asked; and for
    a finished one, the rows stored so.
    """
    stored = Grading.status == status
    if status == GradingStatus.SYSTEM_ERROR:
        return or_(stored, _overdue(now), _worth_asking(now))
    if status in UNFINISHED:
        return and_(stored, not_(_overdue(now)))
    return stored


async def lost(ctx: Context, rows: Iterable[Grading]) -> frozenset[uuid.UUID]:
    """The gradings among `rows` whose run the CI no longer holds, or holds
    as finished while the grading still waits for its envelope: a run that
    ended before the harness began, its checkout failed or the run killed at
    the CI, is as lost as one the CI dropped. Only a grading worth asking
    about is asked about, each run at most once every `RUN_STATE_KEPT` by
    this process, and the connection is let go of before the CI is asked. A
    CI that does not answer loses nothing: the grading reads as its row
    says.
    """
    asking = [
        (row.id, RunId(row.run_id))
        for row in rows
        if row.run_id is not None
        and worth_asking(GradingStatus(row.status), row.dispatched_at, ctx.now)
    ]
    if not asking:
        return frozenset()
    await ctx.let_go()
    found: set[uuid.UUID] = set()
    for grading, run in asking:
        if await _run_state(ctx, grading, run) in (RunState.LOST, RunState.FINISHED):
            found.add(grading)
    return frozenset(found)


async def _run_state(ctx: Context, grading: uuid.UUID, run: RunId) -> RunState | None:
    async def ask() -> RunState | None:
        try:
            state = await ctx.forge.grading.run_state(run)
        except PortError as exc:
            log.warning("gradings.run_state_unanswered", run=run, error=type(exc).__name__)
            return None
        if state is RunState.LOST:
            log.warning("gradings.run_lost", grading=str(grading), run=run)
        return state

    return await ctx.memo.remembered(f"gradings.run_state.{run}", RUN_STATE_KEPT, ask)


@action
async def task_of(ctx: Context, grading: uuid.UUID) -> TaskId:
    """The task a grading is of, the scope the host's guard checks an
    organiser at before a control that names only the grading. `NotFound`
    for no such grading.
    """
    row = await find(ctx, grading)
    if row is None:
        raise NotFound(NO_SUCH_GRADING)
    return TaskId(row.task_id)
