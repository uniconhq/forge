"""Two pollers over one table never take the same row, a row that fails is
recorded without losing the batch, two timed passes ticking together do the
work once, and the work of either runs with the context of its unit of work.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from forge.db.tables import Provisioning
from forge.runtime.background import Loops, Poller, TimedPass
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.testing import FakeClock


async def _seed(setup: Setup, count: int) -> None:
    async with setup.unit_of_work() as ctx:
        for index in range(count):
            ctx.db.add(Provisioning(kind="org", target_id=f"org-{index}", status="pending"))


async def _rows(setup: Setup) -> list[Any]:
    async with setup.unit_of_work() as ctx:
        found = await ctx.db.execute(
            Provisioning.__table__.select().order_by(Provisioning.target_id)
        )
        return list(found)


async def test_two_pollers_never_take_the_same_row(setup: Setup) -> None:
    await _seed(setup, 6)
    taken: list[tuple[str, str]] = []
    gate = asyncio.Event()

    def poller(name: str) -> Poller:
        async def work(ctx: Context, row: Any) -> None:
            taken.append((name, row.target_id))
            row.status = "ready"
            await gate.wait()

        return Poller(name, Provisioning, Provisioning.status, ["pending"], work, _failed, batch=3)

    ticks = asyncio.gather(
        poller("one").tick(setup.unit_of_work), poller("two").tick(setup.unit_of_work)
    )
    await asyncio.sleep(0.5)
    gate.set()
    counts = await ticks

    assert sorted(counts) == [3, 3]
    assert len({target for _, target in taken}) == 6


async def test_a_failed_row_is_recorded_and_the_batch_survives(setup: Setup) -> None:
    await _seed(setup, 2)

    async def work(ctx: Context, row: Any) -> None:
        if row.target_id == "org-0":
            await ctx.db.execute(text("select * from no_such_table"))
        row.status = "ready"

    await Poller("p", Provisioning, Provisioning.status, ["pending"], work, _failed).tick(
        setup.unit_of_work
    )

    rows = await _rows(setup)
    assert [(row.target_id, row.status) for row in rows] == [
        ("org-0", "failed"),
        ("org-1", "ready"),
    ]
    assert "no_such_table" in rows[0].error
    assert rows[0].attempts == 1


async def test_two_passes_ticking_together_do_the_work_once(setup: Setup) -> None:
    runs: list[str] = []
    gate = asyncio.Event()

    def timed(name: str) -> TimedPass:
        async def work(ctx: Context) -> None:
            runs.append(name)
            await gate.wait()

        return TimedPass("nightly", work, timedelta(hours=1))

    ticks = asyncio.gather(
        timed("one").tick(setup.unit_of_work), timed("two").tick(setup.unit_of_work)
    )
    await asyncio.sleep(0.5)
    gate.set()
    done = await ticks

    assert sorted(done) == [False, True]
    assert len(runs) == 1


async def test_a_pass_runs_with_the_forge_the_settings_and_the_clock(
    setup: Setup, clock: FakeClock
) -> None:
    seen: list[Context] = []

    async def work(ctx: Context) -> None:
        seen.append(ctx)

    await TimedPass("look", work, timedelta(hours=1)).tick(setup.unit_of_work)

    (ctx,) = seen
    assert ctx.forge is setup.forge
    assert ctx.settings is setup.settings
    assert ctx.now == clock.now()


async def test_loops_start_and_stop_cleanly(setup: Setup) -> None:
    await _seed(setup, 1)
    seen: list[str] = []

    async def work(ctx: Context, row: Any) -> None:
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
    loops.start(setup.unit_of_work)
    await asyncio.sleep(0.3)
    await loops.stop()

    assert seen == ["org-0"]
    assert loops.running is False


def _failed(row: Any, error: Exception, now: datetime) -> None:
    row.status = "failed"
    row.error = str(error)
    row.attempts = (row.attempts or 0) + 1
