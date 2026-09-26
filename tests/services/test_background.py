"""Two pollers over one table never take the same row, and two timed passes
ticking together do the work once.
"""

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.engine import SessionFactory
from forge.db.tables import Provisioning
from forge.services.background import Loops, Poller, TimedPass


async def _seed(factory: SessionFactory, count: int) -> None:
    async with factory() as db:
        for index in range(count):
            db.add(Provisioning(kind="org", target_id=f"org-{index}", status="pending"))
        await db.commit()


async def test_two_pollers_never_take_the_same_row(factory: SessionFactory) -> None:
    await _seed(factory, 6)
    taken: list[tuple[str, str]] = []
    gate = asyncio.Event()

    def poller(name: str) -> Poller:
        async def work(db: AsyncSession, row: Any) -> None:
            taken.append((name, row.target_id))
            row.status = "ready"
            await gate.wait()

        return Poller(name, Provisioning, Provisioning.status, ["pending"], work, _failed, batch=3)

    first, second = poller("one"), poller("two")
    ticks = asyncio.gather(first.tick(factory), second.tick(factory))
    await asyncio.sleep(0.5)
    gate.set()
    counts = await ticks

    assert sorted(counts) == [3, 3]
    assert len({target for _, target in taken}) == 6


async def test_a_failed_row_goes_back_with_its_error(factory: SessionFactory) -> None:
    await _seed(factory, 1)

    async def work(db: AsyncSession, row: Any) -> None:
        raise RuntimeError("the forge said no")

    await Poller("p", Provisioning, Provisioning.status, ["pending"], work, _failed).tick(factory)

    async with factory() as db:
        row = (await db.execute(Provisioning.__table__.select())).one()
    assert row.status == "failed"
    assert row.error == "the forge said no"
    assert row.attempts == 1


async def test_two_passes_ticking_together_do_the_work_once(factory: SessionFactory) -> None:
    runs: list[str] = []
    gate = asyncio.Event()

    def timed(name: str) -> TimedPass:
        async def work(db: AsyncSession) -> None:
            runs.append(name)
            await gate.wait()

        return TimedPass("nightly", work, timedelta(hours=1))

    ticks = asyncio.gather(timed("one").tick(factory), timed("two").tick(factory))
    await asyncio.sleep(0.5)
    gate.set()
    done = await ticks

    assert sorted(done) == [False, True]
    assert len(runs) == 1


async def test_loops_start_and_stop_cleanly(factory: SessionFactory) -> None:
    await _seed(factory, 1)
    seen: list[str] = []

    async def work(db: AsyncSession, row: Any) -> None:
        seen.append(row.target_id)
        row.status = "ready"

    loops = Loops()
    loops.add(
        Poller(
            "p",
            Provisioning,
            Provisioning.status,
            ["pending"],
            work,
            _failed,
            interval=timedelta(milliseconds=50),
        )
    )
    loops.start(factory)
    await asyncio.sleep(0.3)
    await loops.stop()

    assert seen == ["org-0"]
    assert loops.running is False


def _failed(row: Any, error: Exception) -> None:
    row.status = "failed"
    row.error = str(error)
    row.attempts = (row.attempts or 0) + 1
