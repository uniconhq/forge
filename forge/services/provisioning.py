"""Making something at the forge takes several calls and can fail halfway,
and the forge only knows whether a thing exists. So a request to make a
thing is a `pending` row in `provisioning` carrying what the steps will
need, written at once and answered at once, and the work is done by the
`provisioning` poller: it takes each waiting row, `pending` or `failed`,
and `run` walks the steps of making the thing in order, from the step after
the last one the row records as complete. A step that fails leaves the row
`failed`, naming the step in `failed_step` and the reason in `error`, and a
later tick tries it again, at once after the first failure and then after a
wait in `retry_at` that doubles each time, up to an hour, so a failure that
will not heal by itself costs the forge a call an hour; the steps before it
are not repeated. Work that fails
before it reaches a step waits the same way. The progress is written on the
poller's own unit of work, since the poller holds the row, and lands when
the tick commits; every step is safe to run again, so a tick lost halfway
costs a repeat and nothing else.

The steps of each kind are named in `forge.domain.provisioning.STEPS`, and
a service builds its list with `steps`, which puts its work in that order.
A record carries its kind's steps, the step a failed row stopped at, and
when it is tried again, so whoever follows it needs nothing else.

The setup hands `poller` the function that makes each kind of thing, and
the poller dispatches on `kind`. An operator's command runs the same steps
inline, on its own unit of work, through the same `run`. There is one row
per thing: two requests for it at once leave one row, and the second is
`Conflict`.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Provisioning
from forge.domain.errors import Conflict, ServiceError, UniconError
from forge.domain.provisioning import STEPS, steps_of
from forge.log import get_logger
from forge.runtime.background import Poller
from forge.runtime.context import Context

log = get_logger(__name__)

PENDING = "pending"
RUNNING = "running"
READY = "ready"
FAILED = "failed"
WAITING = (PENDING, FAILED)
POLL_INTERVAL = timedelta(seconds=2)
FIRST_RETRY_WAIT = timedelta(seconds=2)
LONGEST_RETRY_WAIT = timedelta(hours=1)
LONGEST_RETRY_DOUBLINGS = 11

Work = Callable[["Attempt"], Awaitable[None]]
RowWork = Callable[[Context, Provisioning], Awaitable[None]]

UNEXPECTED = "the step failed unexpectedly"
REASONS = {
    "not_found": "the forge did not find something this step needs",
    "forbidden": "the forge refused this step",
    "conflict": "the forge already holds something by this name",
    "rejected": "the forge refused this step",
    "forge_misconfigured": "the forge refused the platform's own registration",
    "forge_unavailable": "the forge or the CI did not answer",
}


@dataclass(frozen=True, slots=True)
class Attempt:
    """Which try this is at making the thing. The first is 1; a step that
    finds its work already done on a later try may take that as its own.
    """

    number: int

    @property
    def is_rerun(self) -> bool:
        return self.number > 1


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    work: Work


@dataclass(frozen=True, slots=True)
class Record:
    """What the row says about one thing being made. `steps` are the steps
    of its kind in order and `last_step` the last one completed. A failed
    row names the step it stopped at in `failed_step`, the one after
    `last_step`, or none when the work failed outside any step, and
    `retry_at` says when the row is next tried; both are none unless the row
    is failed. `error` is the reason the last try failed, in the platform's
    words.
    """

    id: uuid.UUID
    kind: str
    target_id: str
    status: str
    steps: tuple[str, ...]
    last_step: str | None
    failed_step: str | None
    error: str | None
    retry_at: datetime | None
    attempts: int
    ready_at: datetime | None


def steps(kind: str, work: Mapping[str, Work]) -> list[Step]:
    """The steps of making a thing of `kind`, in the order `STEPS` names
    them, each doing the work `work` gives for its name. `ValueError` when
    `work` leaves out a step of the kind or names one it does not have.
    """
    names = STEPS[kind]
    if set(work) != set(names):
        raise ValueError(f"the work for a {kind} does not match its steps {names}")
    return [Step(name, work[name]) for name in names]


async def request(
    ctx: Context, kind: str, target_id: str, payload: Mapping[str, Any]
) -> Provisioning:
    """The pending row that asks for the thing to be made, with what the
    steps will need in `payload`. `Conflict` when a row for it exists: the
    thing is there, or is being made.
    """
    _refuse_existing(await find(ctx, kind, target_id), kind, target_id)
    row = Provisioning(
        kind=kind, target_id=target_id, status=PENDING, attempts=0, payload=dict(payload)
    )
    try:
        async with ctx.db.begin_nested():
            ctx.db.add(row)
            await ctx.db.flush()
    except IntegrityError:
        _refuse_existing(await find(ctx, kind, target_id), kind, target_id)
        raise
    log.info("provisioning.requested", kind=kind, target=target_id)
    return row


async def record_made(ctx: Context, kind: str, target_id: str, *, last_step: str) -> Provisioning:
    """The row of a thing made inline in one go, ready at once, so a later
    request finds it made. `Conflict` when a row for it exists.
    """
    row = await request(ctx, kind, target_id, {})
    row.status = READY
    row.attempts = 1
    row.last_step = last_step
    row.ready_at = ctx.now
    await ctx.db.flush()
    log.info("provisioning.ready", kind=kind, target=target_id, attempt=1)
    return row


def _refuse_existing(row: Provisioning | None, kind: str, target_id: str) -> None:
    if row is None:
        return
    if row.status == READY:
        raise Conflict(f"The {kind} {target_id} already exists.")
    raise Conflict(f"The {kind} {target_id} is being made.")


async def run(ctx: Context, row: Provisioning, steps: Sequence[Step]) -> Record:
    """Make the thing `row` records by running `steps` in order, from the
    step after the last one recorded as complete, on the caller's unit of
    work. A step runs under a savepoint of its own, so a step that fails
    inside the database leaves the unit of work usable. A failure is written
    on the row and returned in the record, not raised: the row is the
    record, and the caller decides what a failed one means.
    """
    names = [step.name for step in steps]
    if len(set(names)) != len(names):
        raise ValueError(f"provisioning steps of {row.kind} {row.target_id} are not distinct")
    if row.status == READY:
        return record_from(row)
    row.status = RUNNING
    row.attempts = row.attempts + 1
    await ctx.db.flush()
    attempt = Attempt(row.attempts)
    start = names.index(row.last_step) + 1 if row.last_step in names else 0
    for step in steps[start:]:
        try:
            async with ctx.db.begin_nested():
                await step.work(attempt)
        except Exception as exc:
            log.warning(
                "provisioning.step_failed",
                kind=row.kind,
                target=row.target_id,
                step=step.name,
                attempt=attempt.number,
                error=type(exc).__name__,
                detail=str(exc),
            )
            row.status = FAILED
            row.failed_step = step.name
            row.error = reason(exc)
            row.retry_at = ctx.now + retry_wait(row.attempts)
            await ctx.db.flush()
            return record_from(row)
        row.last_step = step.name
        await ctx.db.flush()
    row.status = READY
    row.failed_step = None
    row.error = None
    row.retry_at = None
    row.ready_at = ctx.now
    await ctx.db.flush()
    log.info("provisioning.ready", kind=row.kind, target=row.target_id, attempt=attempt.number)
    return record_from(row)


def retry_wait(attempts: int) -> timedelta:
    """How long a row that has failed `attempts` times waits before the next
    try: nothing after the first failure, then two seconds, doubling each
    time, up to an hour.
    """
    if attempts <= 1:
        return timedelta(0)
    doublings = min(attempts - 2, LONGEST_RETRY_DOUBLINGS)
    wait: timedelta = FIRST_RETRY_WAIT * 2**doublings
    return min(wait, LONGEST_RETRY_WAIT)


def reason(exc: Exception) -> str:
    """What the row says about a failure: a refusal by the platform's own
    rules in its own words, and anything the forge or the CI said as a fixed
    sentence for its code, since the forge's words name its hosts and paths.
    """
    if isinstance(exc, ServiceError):
        return exc.detail
    if isinstance(exc, UniconError):
        return REASONS.get(exc.code, UNEXPECTED)
    return UNEXPECTED


def poller(work: Mapping[str, RowWork], interval: timedelta = POLL_INTERVAL) -> Poller:
    """The poller over the waiting rows, handing each to the function `work`
    names for its kind, in the order of what they make: a contestant's
    workspace and places start with the contestant's id, so every tick locks
    contestants in the same order.
    """
    makers = dict(work)

    async def make(ctx: Context, row: Provisioning) -> None:
        maker = makers.get(row.kind)
        if maker is None:
            raise RuntimeError(f"nothing makes a {row.kind}")
        await maker(ctx, row)

    return Poller(
        "provisioning",
        Provisioning,
        Provisioning.status,
        WAITING,
        make,
        failed,
        interval=interval,
        due=Provisioning.retry_at,
        order=Provisioning.target_id,
    )


def failed(row: Provisioning, exc: Exception, now: datetime) -> None:
    """What the poller records when the work itself raised, which `run`
    never does for a step: the row stays waiting, names what happened, and
    waits before the next try as a failed step does.
    """
    log.warning("provisioning.work_failed", kind=row.kind, target=row.target_id)
    row.status = FAILED
    row.failed_step = None
    row.error = reason(exc)
    row.attempts = row.attempts + 1
    row.retry_at = now + retry_wait(row.attempts)


async def record_of(ctx: Context, kind: str, target_id: str) -> Record | None:
    """The row for one thing, or none when nothing has asked to make it."""
    row = await find(ctx, kind, target_id)
    return record_from(row) if row is not None else None


async def find(ctx: Context, kind: str, target_id: str) -> Provisioning | None:
    return (
        await ctx.db.execute(
            select(Provisioning).where(
                Provisioning.kind == kind, Provisioning.target_id == target_id
            )
        )
    ).scalar_one_or_none()


def record_from(row: Provisioning) -> Record:
    failed = row.status == FAILED
    return Record(
        id=row.id,
        kind=row.kind,
        target_id=row.target_id,
        status=row.status,
        steps=steps_of(row.kind),
        last_step=row.last_step,
        failed_step=row.failed_step if failed else None,
        error=row.error,
        retry_at=row.retry_at if failed else None,
        attempts=row.attempts,
        ready_at=row.ready_at,
    )
