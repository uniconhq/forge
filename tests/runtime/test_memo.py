"""An answer kept for a while: the first caller works it out and the ones
arriving meanwhile wait for that answer rather than working it out again, and
it is worked out afresh once its time has passed by the setup's clock, or
once it is forgotten, even while it was being worked out.
"""

import asyncio
from datetime import timedelta

from forge.domain.clock import FakeClock
from forge.runtime.memo import Memo


async def test_callers_arriving_together_share_one_answer() -> None:
    memo = Memo(FakeClock())
    worked = 0

    async def work() -> int:
        nonlocal worked
        worked += 1
        await asyncio.sleep(0)
        return worked

    answers = await asyncio.gather(
        *(memo.remembered("list", timedelta(seconds=5), work) for _ in range(20))
    )

    assert answers == [1] * 20
    assert worked == 1


async def test_an_answer_is_worked_out_again_once_its_time_has_passed() -> None:
    clock = FakeClock()
    memo = Memo(clock)
    answers = iter(["first", "second"])

    async def work() -> str:
        return next(answers)

    kept = await memo.remembered("list", timedelta(seconds=5), work)
    clock.advance(timedelta(seconds=4))
    still = await memo.remembered("list", timedelta(seconds=5), work)
    clock.advance(timedelta(seconds=1))
    fresh = await memo.remembered("list", timedelta(seconds=5), work)

    assert (kept, still, fresh) == ("first", "first", "second")


async def test_a_forgotten_answer_is_worked_out_again() -> None:
    memo = Memo(FakeClock())
    answers = iter(["first", "second"])

    async def work() -> str:
        return next(answers)

    kept = await memo.remembered("list", timedelta(seconds=5), work)
    memo.forget("list")
    fresh = await memo.remembered("list", timedelta(seconds=5), work)

    assert (kept, fresh) == ("first", "second")


async def test_an_answer_forgotten_while_it_was_worked_out_is_not_kept() -> None:
    memo = Memo(FakeClock())
    answers = iter(["stale", "fresh"])

    async def work() -> str:
        answer = next(answers)
        if answer == "stale":
            memo.forget("list")
        return answer

    stale = await memo.remembered("list", timedelta(seconds=5), work)
    fresh = await memo.remembered("list", timedelta(seconds=5), work)

    assert (stale, fresh) == ("stale", "fresh")
