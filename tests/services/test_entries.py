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
from sqlalchemy import func, select, update

from forge.db.tables import Entry
from forge.db.tables.entrants import Entrant
from forge.domain.errors import Forbidden, InvalidMark, NotFound
from forge.domain.grading import GradingStatus
from forge.domain.names import EntrantOwner
from forge.domain.release import Extension
from forge.domain.roles import Role, Scope, contest_id_of, task_scope
from forge.runtime.setup import Setup
from forge.services import entries, gradings, published, scores, submissions
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
from tests.services.test_steps import _save

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
LATER = REVIEWED.replace("samples/, review, reviewed]", "samples/]")
"""REVIEWED with the mark and what reads it listed under no time, so a
save may drop them."""
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


async def _entries(setup: Setup) -> list[tuple[str, str | None, int]]:
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


async def test_a_mark_of_40_characters_is_kept_exactly_and_counts(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    tiny = "." + "0" * 38 + "1"
    many = "9." + "9" * 38

    kept = await _enter(setup, acme, islands.task, tiny)
    assert kept.mark == "0." + "0" * 38 + "1"
    assert (await _organised(setup, acme, islands.task))["review"] == Fraction(1, 10**39)
    assert (await _enter(setup, acme, islands.task, many)).mark == many
    assert (await _organised(setup, acme, islands.task))["review"] == Fraction(many)


async def test_a_kept_entry_that_is_no_mark_is_a_bug_not_a_missing_mark(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "5")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Entry).values(mark="five"))

    with pytest.raises(ValueError, match=r"An entry for review holds 'five', which is no mark\."):
        await _organised(setup, acme, islands.task)


async def test_a_value_removed_and_added_again_starts_with_no_marks(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, LATER)
    await _enter(setup, acme, islands.task, "4")
    unreviewed = TASKS["islands"][0]

    for text in (unreviewed, LATER):
        await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
        await setup.settle()
        [*_, save] = await _rows(setup, "save")
        assert save.status == GradingStatus.DONE
        await setup.settle()

    assert (await _organised(setup, acme, islands.task))["review"] is None
    await _enter(setup, acme, islands.task, "6")
    assert (await _organised(setup, acme, islands.task))["review"] == 6
    # The old mark is kept, and a start before the new one.
    assert await _entries(setup) == [("review", "4", 7), ("review", None, 7), ("review", "6", 7)]


async def test_a_value_kept_through_a_save_keeps_its_marks(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, LATER)
    await _enter(setup, acme, islands.task, "4")

    await _save(
        setup,
        acme,
        islands.task,
        {"task.yaml": LATER.replace("at_most: 10", "at_most: 20").encode()},
    )
    await setup.settle()

    assert (await _organised(setup, acme, islands.task))["review"] == 4
    assert await _entries(setup) == [("review", "4", 7)]


async def test_the_shared_scoring_gives_what_reads_a_mark_to_organisers_alone(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "6")
    [early] = [row for row in await _rows(setup, "early") if row.attempt == 1]

    async with setup.unit_of_work() as ctx:
        settings = await published.contest(ctx, contest_id_of(task_scope(islands.task)))
        current = await published.task(ctx, islands.task, settings)
        assert current is not None
        late = await scores.late_of(ctx, early)
        scored = await scores.run_scored(ctx, current, settings, Extension(), early, late)

    marked = {"review", "reviewed"}
    assert marked.isdisjoint(scored.whole.outcomes)
    assert marked.isdisjoint(scored.early.outcomes)
    assert scored.organised.outcomes["review"].number == 6
    assert set(scored.whole.outcomes) == set(scored.organised.outcomes) - marked
