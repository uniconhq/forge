"""Work that happens with nobody clicking, in two shapes and with no jobs
table. A poller takes waiting rows from a table that already carries a status,
each under `FOR UPDATE SKIP LOCKED`, so two processes never take the same row.
A timed pass runs on a clock and takes a Postgres advisory lock first, so it
runs once however many processes are up.
"""

import asyncio
import contextlib
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from forge.db.engine import SessionFactory
from forge.log import get_logger

log = get_logger(__name__)

RowWork = Callable[[AsyncSession, Any], Awaitable[None]]
RowFailure = Callable[[Any, Exception], None]
PassWork = Callable[[AsyncSession], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Poller:
    """Takes rows of `table` whose `status` is one of `waiting`, in batches,
    and calls `work` on each inside the transaction that holds the row. A
    failure hands the row to `failed`, which records the error on it.
    """

    name: str
    table: type[Any]
    status: InstrumentedAttribute[str]
    waiting: Sequence[str]
    work: RowWork
    failed: RowFailure
    interval: timedelta = timedelta(seconds=2)
    batch: int = 10

    async def tick(self, factory: SessionFactory) -> int:
        async with factory() as db:
            rows: Sequence[Any] = (
                (
                    await db.execute(
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
                    await self.work(db, row)
                except Exception as exc:
                    log.exception("poller.row_failed", poller=self.name)
                    self.failed(row, exc)
            await db.commit()
            return len(rows)


@dataclass(frozen=True, slots=True)
class TimedPass:
    """Runs `work` every `interval` under an advisory lock keyed by `name`. A
    process that finds the lock held skips that tick.
    """

    name: str
    work: PassWork
    interval: timedelta

    @property
    def lock_key(self) -> int:
        return zlib.crc32(self.name.encode())

    async def tick(self, factory: SessionFactory) -> bool:
        async with factory() as db:
            held = (
                await db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": self.lock_key}
                )
            ).scalar()
            if not held:
                return False
            await self.work(db)
            await db.commit()
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

    def start(self, factory: SessionFactory) -> None:
        for poller in self.pollers:
            self._tasks.append(
                asyncio.create_task(self._run(poller.name, poller.interval, poller.tick, factory))
            )
        for timed in self.passes:
            self._tasks.append(
                asyncio.create_task(self._run(timed.name, timed.interval, timed.tick, factory))
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
        tick: Callable[[SessionFactory], Awaitable[Any]],
        factory: SessionFactory,
    ) -> None:
        while True:
            try:
                await tick(factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("loop.tick_failed", loop=name)
            await asyncio.sleep(interval.total_seconds())
