"""Marks staff enter for a submission's `entered` values (SIMPLER-CORE.md
14.2, stage 5 section 6 items 8 and 9): a mark within its block's bounds
is kept and counts, one outside them is refused, exactly; a value is
missing until a mark is entered; every entry is kept and the latest
counts; a regrade never touches one; a manager of the task, a role at its
contest included, enters one and an observer is refused; a value the live
version gives no `entered` block takes none; and until the grading queue
decides when, an entry reaches organisers alone.
"""

from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import func, select

from forge.db.tables import Entry
from forge.db.tables.entrants import Entrant
from forge.domain.errors import Forbidden, InvalidMark, NotFound
from forge.domain.grading import GradingStatus
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import entries, gradings, submissions
from forge.testing import FakeClock
from tests.services.conftest import Acme, organiser
from tests.services.test_pipelines import (
    BEFORE_CLOSE,
    ISLANDS,
    _graded_both,
    _grading,
    _rows,
    _values,
)
from tests.services.test_regrades import _held
from tests.services.test_run_task_saves import TASKS

REVIEWED = (
    TASKS["islands"][0]
    .replace(
        "    total:\n",
        "    review: {block: entered, at_least: 0, at_most: 10}\n"
        "    reviewed: {block: add, of: [{value: sample_score}, {value: review}]}\n"
        "    total:\n",
    )
    .replace(
        "now: [sample_score, sample_time, sample_tests, samples/]",
        "now: [sample_score, sample_time, sample_tests, samples/, review, reviewed]",
    )
)
"""islands with a reviewer's mark out of 10, and a value reading it, both
listed under `now`."""
CONTEST = Scope("acme", "spring")


async def _owner(setup: Setup) -> EntrantOwner:
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    return EntrantOwner(entrant)


async def _organised(setup: Setup, acme: Acme, task: Any) -> dict[str, Any]:
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    seen = await submissions.organised(setup, manager, task, await _owner(setup), 1)
    assert seen.grading is not None
    return _values(seen.grading)


async def _enter(setup: Setup, acme: Acme, task: Any, mark: str, value: str = "review") -> Any:
    return await entries.enter(setup, acme.ada, task, await _owner(setup), 1, value, mark)


async def _entries(setup: Setup) -> list[tuple[str, str, int]]:
    async with setup.unit_of_work() as ctx:
        rows = await ctx.db.execute(
            select(Entry.value, Entry.mark, Entry.entered_by).order_by(Entry.seq)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def test_a_value_is_missing_until_a_mark_is_entered_and_the_latest_counts(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    before = await _organised(setup, acme, islands.task)
    assert (before["review"], before["reviewed"]) == (None, None)

    entered = await _enter(setup, acme, islands.task, "7.50")
    assert (entered.value, entered.mark, entered.by, entered.at) == (
        "review",
        "7.5",
        7,
        clock.now(),
    )
    after = await _organised(setup, acme, islands.task)
    assert (after["review"], after["reviewed"]) == (Fraction(15, 2), Fraction(17, 2))

    await _enter(setup, acme, islands.task, "10")
    assert (await _organised(setup, acme, islands.task))["review"] == 10
    assert await _entries(setup) == [("review", "7.5", 7), ("review", "10", 7)]


@pytest.mark.parametrize("mark", ["10.000001", "-0.5", "1e1", "seven", "", "NaN", "Infinity"])
async def test_a_mark_outside_its_bounds_or_not_a_plain_number_is_refused(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, mark: str
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    with pytest.raises(InvalidMark) as refused:
        await _enter(setup, acme, islands.task, mark)

    assert refused.value.detail == "Enter a number from 0 to 10 for review, such as 0."
    assert await _entries(setup) == []


@pytest.mark.parametrize("value", ["reviewed", "total", "nothing"])
async def test_a_value_the_live_version_gives_no_entered_block_takes_no_mark(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, value: str
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    with pytest.raises(InvalidMark) as refused:
        await _enter(setup, acme, islands.task, "5", value=value)

    assert refused.value.detail == (
        f"{value} takes no entered mark: the task's live version gives it no entered block."
    )
    assert await _entries(setup) == []


async def test_a_manager_of_the_contest_enters_a_mark_and_an_observer_is_refused(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    owner = await _owner(setup)

    for scope in (ISLANDS, CONTEST):
        with pytest.raises(Forbidden):
            await entries.enter(
                setup, _held(acme, scope, Role.OBSERVER), islands.task, owner, 1, "review", "5"
            )
    manager = _held(acme, CONTEST, Role.MANAGER)
    await entries.enter(setup, manager, islands.task, owner, 1, "review", "5")

    assert await _entries(setup) == [("review", "5", 9)]


async def test_a_submission_the_entrant_does_not_have_takes_no_mark(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    with pytest.raises(NotFound):
        await entries.enter(setup, acme.ada, islands.task, await _owner(setup), 2, "review", "5")


async def test_an_entry_is_kept_through_a_regrade(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "4")

    await gradings.rejudge(setup, acme.ada, islands.task)
    await setup.settle()

    # Every item is reused from the store, so the new attempt has finished.
    assert [row.status for row in await _rows(setup, "early") if row.attempt == 2] == [
        GradingStatus.DONE
    ]
    assert (await _organised(setup, acme, islands.task))["review"] == 4
    assert await _entries(setup) == [("review", "4", 7)]
    async with setup.unit_of_work() as ctx:
        assert await ctx.db.scalar(select(func.count()).select_from(Entry)) == 1


async def test_an_entry_and_what_reads_it_reach_organisers_alone_for_now(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "6")

    seen = _values(await _grading(setup, session, islands))

    assert set(seen) == {"sample_score", "sample_time"}
    assert {"review", "reviewed"} <= set(await _organised(setup, acme, islands.task))
