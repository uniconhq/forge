"""Work that happens with nobody clicking, in two shapes. A poller takes
waiting rows from a table that already carries a status, each under
`FOR UPDATE SKIP LOCKED`, so two processes never take the same row. A timed
pass runs on a clock and takes a Postgres advisory lock first, so it runs once
however many processes are up. Each tick is one unit of work, and its work
runs with the `Context` over it, as a building block does: the forge, the
settings and the clock are there, and the unit of work commits or rolls back.
"""

import asyncio
import contextlib
import zlib
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import InstrumentedAttribute

from forge.log import get_logger
from forge.runtime.context import Context

log = get_logger(__name__)

UnitOfWork = Callable[[], AbstractAsyncContextManager[Context]]
RowWork = Callable[[Context, Any], Awaitable[None]]
RowFailure = Callable[[Any, Exception], None]
PassWork = Callable[[Context], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class Poller:
    """Takes rows of `table` whose `status` is one of `waiting`, in batches,
    and calls `work` on each under a savepoint of its own. A failure rolls the
    row's savepoint back and hands the row to `failed`, which records the
    error on it, so one bad row costs neither the batch nor its own record.
    """

    name: str
    table: type[Any]
    status: InstrumentedAttribute[str]
    waiting: Sequence[str]
    work: RowWork
    failed: RowFailure
    interval: timedelta = timedelta(seconds=2)
    batch: int = 10

    async def tick(self, unit_of_work: UnitOfWork) -> int:
        async with unit_of_work() as ctx:
            rows: Sequence[Any] = (
                (
                    await ctx.db.execute(
                        select(self.table)
                        .where(self.status.in_(list(self.waiting)))
                        .with_for_update(skip_locked=True)
                        .limit(self.batch)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                try:
                    async with ctx.db.begin_nested():
                        await self.work(ctx, row)
                except Exception as exc:
                    log.exception("poller.row_failed", poller=self.name)
                    await ctx.db.refresh(row)
                    self.failed(row, exc)
            return len(rows)


@dataclass(frozen=True, slots=True)
class TimedPass:
    """Runs `work` every `interval` under an advisory lock keyed by `name`. A
    process that finds the lock held skips that tick. The lock is the
    transaction's, so it is let go when the unit of work ends.
    """

    name: str
    work: PassWork
    interval: timedelta

    @property
    def lock_key(self) -> int:
        return zlib.crc32(self.name.encode())

    async def tick(self, unit_of_work: UnitOfWork) -> bool:
        async with unit_of_work() as ctx:
            held = (
                await ctx.db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": self.lock_key}
                )
            ).scalar()
            if not held:
                return False
            await self.work(ctx)
            return True


@dataclass
class Loops:
    """The pollers and timed passes a process runs, started together beside
    whatever else the process does and stopped cleanly so a row half-taken is
    released.
    """

    pollers: list[Poller] = field(default_factory=list)
    passes: list[TimedPass] = field(default_factory=list)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def add(self, *loops: Poller | TimedPass) -> None:
        for loop in loops:
            if isinstance(loop, Poller):
                self.pollers.append(loop)
            else:
                self.passes.append(loop)

    def start(self, unit_of_work: UnitOfWork) -> None:
        for poller in self.pollers:
            self._tasks.append(
                asyncio.create_task(
                    self._run(poller.name, poller.interval, poller.tick, unit_of_work)
                )
            )
        for timed in self.passes:
            self._tasks.append(
                asyncio.create_task(self._run(timed.name, timed.interval, timed.tick, unit_of_work))
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    @staticmethod
    async def _run(
        name: str,
        interval: timedelta,
        tick: Callable[[UnitOfWork], Awaitable[Any]],
        unit_of_work: UnitOfWork,
    ) -> None:
        while True:
            try:
                await tick(unit_of_work)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("loop.tick_failed", loop=name)
            await asyncio.sleep(interval.total_seconds())
