"""Making something at the forge takes several calls and can fail halfway,
and the forge only knows whether a thing exists. `run` walks the steps of
making one thing in order and keeps the record on its `provisioning` row:
the step that last completed, written in a transaction of its own as soon as
it completes, so it survives whatever the caller's unit of work does next. A
step that fails leaves the row `failed`, naming the step and the error, and
the failure is raised to the caller. A rerun of the same thing starts at the
step after the last one that completed, so the failed step is tried again
and the ones before it are not.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.tables import Provisioning
from forge.log import get_logger
from forge.runtime.context import Context

log = get_logger(__name__)

PENDING = "pending"
RUNNING = "running"
READY = "ready"
FAILED = "failed"

Work = Callable[["Attempt"], Awaitable[None]]


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
    """What the row says about one thing being made."""

    id: uuid.UUID
    status: str
    last_step: str | None
    error: str | None
    attempts: int
    ready_at: datetime | None


async def run(ctx: Context, kind: str, target_id: str, steps: Sequence[Step]) -> Record:
    """Make the thing `kind` and `target_id` name by running `steps` in
    order, from the step after the last one recorded as complete. Returns
    the row's final state; raises what a failing step raised.
    """
    names = [step.name for step in steps]
    if len(set(names)) != len(names):
        raise ValueError(f"provisioning steps of {kind} {target_id} are not distinct: {names}")
    record = await _claim(ctx, kind, target_id)
    if record.status == READY:
        return record
    attempt = Attempt(record.attempts)
    start = names.index(record.last_step) + 1 if record.last_step in names else 0
    for step in steps[start:]:
        try:
            await step.work(attempt)
        except Exception as exc:
            log.warning(
                "provisioning.step_failed",
                kind=kind,
                target=target_id,
                step=step.name,
                attempt=attempt.number,
            )
            await _write(ctx, record.id, status=FAILED, error=f"{step.name}: {exc}")
            raise
        await _write(ctx, record.id, last_step=step.name)
    log.info("provisioning.ready", kind=kind, target=target_id, attempt=attempt.number)
    return await _write(ctx, record.id, status=READY, error=None, ready_at=ctx.now)


async def record_of(ctx: Context, kind: str, target_id: str) -> Record | None:
    """The row for one thing, or none when nothing has tried to make it."""
    row = await _find(ctx.db, kind, target_id)
    return _record(row) if row is not None else None


async def _claim(ctx: Context, kind: str, target_id: str) -> Record:
    """The row for the thing, made if needed, marked running with one more
    attempt counted. In a transaction of its own so the record of the
    attempt exists before any step runs.
    """
    async with ctx.transactions() as own:
        row = await _find(own, kind, target_id)
        if row is None:
            row = Provisioning(kind=kind, target_id=target_id, status=PENDING, attempts=0)
            own.add(row)
            await own.flush()
        if row.status != READY:
            row.status = RUNNING
            row.attempts = row.attempts + 1
        record = _record(row)
        await own.commit()
    return record


async def _find(db: AsyncSession, kind: str, target_id: str) -> Provisioning | None:
    return (
        await db.execute(
            select(Provisioning).where(
                Provisioning.kind == kind, Provisioning.target_id == target_id
            )
        )
    ).scalar_one_or_none()


async def _write(ctx: Context, row_id: uuid.UUID, **values: Any) -> Record:
    async with ctx.transactions() as own:
        await own.execute(update(Provisioning).where(Provisioning.id == row_id).values(**values))
        row = (
            await own.execute(select(Provisioning).where(Provisioning.id == row_id))
        ).scalar_one()
        record = _record(row)
        await own.commit()
    return record


def _record(row: Provisioning) -> Record:
    return Record(
        id=row.id,
        status=row.status,
        last_step=row.last_step,
        error=row.error,
        attempts=row.attempts,
        ready_at=row.ready_at,
    )
