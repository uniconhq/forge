"""Staff releasing a run task's results and taking a release back
(SIMPLER-CORE.md 9.4, stage 5 section 6 items 2 and 10): what `on_release`
lists is shown to entrants, its values, runs and files, only while the
results are released; a take-back warns before it hides; each action is a
row with who and when, added only when it changes the state; gradings
made while released take the split that runs `on_release` early, and a
grading keeps the split it was made with; an extension, or a later close,
is refused while released and allowed after a take-back; a save may not drop what a
release shows while it stands, nor have a name shown during the contest
read fewer runs; and only a manager or admin of the task acts.
"""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.entrants import Entrant
from forge.domain.content import Edit
from forge.domain.definitions import ContestDefinition, parse_contest
from forge.domain.errors import (
    ConfirmationRequired,
    Forbidden,
    InvalidExtension,
    NotARunTask,
    NotFound,
)
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.live import Nudge, NudgeKind
from forge.domain.roles import Role, Scope, task_scope
from forge.domain.run_plan import RELEASE_MOVED
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import (
    entrants,
    gradings,
    landing,
    publications,
    results,
    steps,
    tasks,
    timelines,
)
from forge.services.publications import Draft, Published
from forge.services.results import ResultsState
from forge.testing import FakeClock
from tests.services.conftest import SPRING, Acme, Entered
from tests.services.test_pipelines import (
    AFTER_CLOSE,
    BEFORE_CLOSE,
    ISLANDS,
    LIVE,
    _checked,
    _early,
    _graded_both,
    _grading,
    _rows,
    _runs_seen,
    _submit,
    _values,
)
from tests.services.test_regrades import _held
from tests.services.test_run_task_saves import TASKS
from tests.services.test_steps import _batch, _report, _step

RELEASING = TASKS["islands"][0].replace(
    "after_close: [st1, st2, total, all]",
    "after_close: [st1, all]\n    on_release: [st2, st2_tests, tests/4.in]",
)
"""islands showing st2, its runs and one of its files on a release."""
CONTEST = Scope("acme", "spring")


async def _release(setup: Setup, acme: Acme, task: TaskId) -> bool:
    return await results.release(setup, acme.ada, task, confirmed=True)


async def _hide(setup: Setup, acme: Acme, task: TaskId) -> bool:
    return await results.hide_again(setup, acme.ada, task, confirmed=True)


async def _file(setup: Setup, task: TaskId, path: str) -> bytes | None:
    try:
        return (await landing.public_file(setup, task, path)).content
    except NotFound:
        return None


async def test_what_on_release_lists_is_shown_only_while_the_results_are_released(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme, RELEASING)
    hidden = {"sample_score": 1, "sample_time": 12}
    samples = [("sample_tests", 1, "accepted"), ("sample_tests", 2, "accepted")]
    shown = [*samples, ("st2_tests", 1, "accepted"), ("st2_tests", 2, "accepted")]

    async def seen() -> tuple[dict[str, Any], list[Any], bytes | None]:
        grading = await _grading(setup, session, islands)
        return _values(grading), _runs_seen(grading), await _file(setup, islands.task, "tests/4.in")

    assert await seen() == (hidden, samples, None)
    await _release(setup, acme, islands.task)
    assert await seen() == ({**hidden, "st2": 1}, shown, b"4\n")
    await _hide(setup, acme, islands.task)
    assert await seen() == (hidden, samples, None)
    await _release(setup, acme, islands.task)
    assert await seen() == ({**hidden, "st2": 1}, shown, b"4\n")


async def test_each_action_asks_first_and_changes_nothing_unconfirmed(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)

    with pytest.raises(ConfirmationRequired) as asked:
        await results.release(setup, acme.ada, islands.task)
    assert asked.value.extra == {"action": "release"}
    assert asked.value.detail == (
        "Every entrant of islands will be shown, from now on, what its scoring's show lists "
        "under on_release: st2, st2_tests, tests/4.in. Gradings from now on run those runs "
        "early. You can hide the results again later, though entrants may have seen them by "
        "then."
    )
    assert (await tasks.state(setup, acme.ada, islands.task)).results == ResultsState(False, ())

    await _release(setup, acme, islands.task)
    with pytest.raises(ConfirmationRequired) as warned:
        await results.hide_again(setup, acme.ada, islands.task)
    assert warned.value.extra == {"action": "hide_again"}
    assert warned.value.detail == (
        "Entrants may already have seen these results; hiding them again won't undo that."
    )
    assert (await tasks.state(setup, acme.ada, islands.task)).results.released


async def test_each_action_is_kept_with_who_and_when_and_one_that_changes_nothing_adds_none(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)
    first = clock.now()

    assert await results.hide_again(setup, acme.ada, islands.task) is False
    assert await _release(setup, acme, islands.task) is True
    assert await results.release(setup, acme.ada, islands.task) is False
    clock.advance(timedelta(minutes=2))
    assert await _hide(setup, acme, islands.task) is True
    assert await _hide(setup, acme, islands.task) is False

    state = (await tasks.state(setup, acme.ada, islands.task)).results
    assert state == await results.state(setup, acme.ada, islands.task)
    assert not state.released
    assert [(each.action, each.by, each.by_name, each.at) for each in state.history] == [
        ("released", 7, "ada", first),
        ("hidden_again", 7, "ada", first + timedelta(minutes=2)),
    ]


async def test_a_grading_keeps_the_split_it_was_made_with(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)

    async def early_runs(row: Grading) -> set[str]:
        async with setup.unit_of_work() as ctx:
            plan, _ = await steps.plan_of(ctx, row)
            return {run.id for run in plan.runs if run.early}

    await _submit(setup, acme, islands, user=8)
    await _release(setup, acme, islands.task)
    await _submit(setup, acme, islands, user=9)
    by = {row.submitted_by: row for row in await _rows(setup, "early")}
    before, during = by[8], by[9]
    assert (before.released_split, during.released_split) == (False, True)
    assert await early_runs(before) == {"r1", "r2"}
    assert await early_runs(during) == {"r1", "r2", "r3", "r4"}

    # The grading made before the release keeps its runs late.
    await _early(setup, acme, before.id)
    [late] = await _rows(setup, "late")
    assert (late.early_id, late.released_split) == (before.id, False)

    await _hide(setup, acme, islands.task)
    acme.fake.add_user(10, "dee")
    await _submit(setup, acme, islands, user=10)
    after = {row.submitted_by: row for row in await _rows(setup, "early")}[10]
    assert after.released_split is False
    assert await early_runs(after) == {"r1", "r2"}

    # A regrade made while released takes the released split.
    await _release(setup, acme, islands.task)
    await gradings.rejudge(setup, acme.ada, islands.task)
    again = [row for row in await _rows(setup, "early") if row.attempt == 2]
    assert again and all(row.released_split for row in again)


async def test_a_release_nudges_the_tasks_entrants_and_organisers(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)
    told: list[Nudge] = []
    nudge = Context.nudge

    def heard(ctx: Context, made: Nudge) -> None:
        told.append(made)
        nudge(ctx, made)

    monkeypatch.setattr(Context, "nudge", heard)
    await _release(setup, acme, islands.task)
    await _hide(setup, acme, islands.task)

    expected = Nudge(
        NudgeKind.RESULTS, islands.task, scope=task_scope(islands.task), contest=SPRING
    )
    assert told == [expected, expected]


async def test_a_manager_of_the_contest_releases_and_an_observer_is_refused(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)

    for scope in (ISLANDS, CONTEST):
        with pytest.raises(Forbidden):
            await results.release(
                setup, _held(acme, scope, Role.OBSERVER), islands.task, confirmed=True
            )
    manager = _held(acme, CONTEST, Role.MANAGER)
    assert await results.release(setup, manager, islands.task, confirmed=True)
    state = await results.state(setup, _held(acme, ISLANDS, Role.OBSERVER), islands.task)
    assert state.released
    assert [each.by for each in state.history] == [9]


async def test_a_task_that_does_not_grade_by_runs_has_no_results_to_release(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    with pytest.raises(NotARunTask):
        await results.release(setup, acme.ada, entered.task, confirmed=True)


async def test_an_extension_is_refused_while_released_and_allowed_after_a_take_back(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)
    await _submit(setup, acme, islands)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    manager = _held(acme, CONTEST, Role.MANAGER)

    await _release(setup, acme, islands.task)
    with pytest.raises(InvalidExtension, match="islands's results are released"):
        await entrants.extend(setup, manager, SPRING, entrant, timedelta(minutes=30))
    await _hide(setup, acme, islands.task)
    extended = await entrants.extend(setup, manager, SPRING, entrant, timedelta(minutes=30))
    assert extended.time_extension == timedelta(minutes=30)


async def _try_save(setup: Setup, acme: Acme, task: TaskId, text: str) -> Published | Draft:
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {"task.yaml": Edit(text.encode(), head.tokens.get("task.yaml"))}
    try:
        return await publications.save(setup, acme.ada, task, edits, confirm=True)
    except ConfirmationRequired as asked:
        return await publications.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.extra["asked"]
        )


def _errors(result: Published | Draft) -> list[tuple[str, str]]:
    assert isinstance(result, Draft), result
    return [(error["path"], error["message"]) for error in result.errors]


async def test_a_save_keeps_what_a_release_shows_while_it_stands_and_may_drop_it_after(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, RELEASING)
    dropped = RELEASING.replace("on_release: [st2, st2_tests, tests/4.in]", "on_release: [st2]")

    await _release(setup, acme, islands.task)
    assert _errors(await _try_save(setup, acme, islands.task, dropped)) == [
        (
            "scoring.show",
            f"Entrants have been shown {name} once results are released, and what they have "
            "been shown cannot be hidden again. List it as soon as before.",
        )
        for name in ("st2_tests", "tests/4.in")
    ]
    await _hide(setup, acme, islands.task)
    assert isinstance(await _try_save(setup, acme, islands.task, dropped), Published)


async def test_a_group_shown_during_the_contest_may_not_later_read_fewer_runs(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme)
    fewer = TASKS["islands"][0].replace(
        "sample_tests: {input: [samples/1.in, samples/2.in]}",
        "sample_tests: {input: [samples/1.in]}",
    )

    found = _errors(await _try_save(setup, acme, islands.task, fewer))

    assert (
        "scoring.groups.sample_tests",
        "Entrants have been shown sample_tests during the contest over 2 runs the task still "
        "has, and it would now read 1 fewer. What they have been shown cannot be hidden again, "
        "so keep those runs in it.",
    ) in found


async def test_a_task_whose_results_are_released_may_not_close_later(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)

    def settings(minute: int) -> ContestDefinition:
        entry = f"  - {{id: islands, closes: 2026-09-26T14:{minute}:00Z}}\n"
        return parse_contest(LIVE.replace("  - id: sum\n", "").replace("  - id: islands\n", entry))

    async def problems() -> list[tuple[str, str]]:
        async with setup.unit_of_work() as ctx:
            found = await timelines.check_contest(ctx, SPRING, settings(55), settings(58))
        return [(each["path"], each["message"]) for each in found]

    assert await problems() == []
    await _release(setup, acme, islands.task)
    assert await problems() == [
        (
            "tasks[0].closes",
            "islands's results are released, so a later closing time would let entrants submit "
            "knowing them.",
        )
    ]
    await _hide(setup, acme, islands.task)
    assert await problems() == []


OPEN = RELEASING.replace(
    "after_close: [st1, all]\n    on_release: [st2, st2_tests, tests/4.in]",
    "after_close: [st1, all, st2_tests, tests/4.in]\n    on_release: [st2]",
)
"""RELEASING with st2_tests and its file shown at the reveal instead."""


def _kept(path: str, names: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (
            path,
            f"Entrants have been shown {name} once results are released, and what they have "
            "been shown cannot be hidden again. List it as soon as before.",
        )
        for name in names
    ]


async def test_while_released_what_a_release_shows_stays_shown_until_the_reveal(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, OPEN)
    await _release(setup, acme, islands.task)

    # Before the reveal, what the reveal would show may move to the release,
    # which shows it now, but not back.
    assert isinstance(await _try_save(setup, acme, islands.task, RELEASING), Published)
    assert _errors(await _try_save(setup, acme, islands.task, OPEN)) == _kept(
        "scoring.show.after_close", ("st2_tests", "tests/4.in")
    )

    # Once the reveal has passed, it may show what the release showed, and
    # what it showed may not move back to the release.
    clock.set(AFTER_CLOSE)
    assert isinstance(await _try_save(setup, acme, islands.task, OPEN), Published)
    assert _errors(await _try_save(setup, acme, islands.task, RELEASING)) == [
        (
            "scoring.show.on_release",
            f"Entrants have been shown {name} at the reveal, and what they have been shown "
            "cannot be hidden again. List it as soon as before.",
        )
        for name in ("st2_tests", "tests/4.in")
    ]


async def test_what_no_release_showed_never_holds_a_save_back(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, RELEASING)
    dropped = RELEASING.replace("on_release: [st2, st2_tests, tests/4.in]", "on_release: [st2]")
    assert isinstance(await _try_save(setup, acme, islands.task, dropped), Published)

    await _release(setup, acme, islands.task)

    again = dropped.replace("max: 50", "max: 49")
    assert isinstance(await _try_save(setup, acme, islands.task, again), Published)


async def test_a_value_shown_before_what_it_reads_is_refused_and_a_group_shown_sooner_noted(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)
    sooner = RELEASING.replace("after_close: [st1, all]", "after_close: [st1, total, all]")

    assert _errors(await _try_save(setup, acme, islands.task, sooner)) == [
        (
            "scoring.show.after_close[1]",
            f"total reads {name}, which is shown once results are released, so showing total "
            f"at the reveal would give {name} away sooner. List total no sooner than {name}.",
        )
        for name in ("st2", "st2_tests")
    ]
    saved = await _try_save(setup, acme, islands.task, RELEASING.replace("max: 50", "max: 49"))
    assert isinstance(saved, Published)
    assert (
        "all, shown at the reveal, holds runs st2 reads, so entrants see those runs before a "
        "release shows st2."
    ) in saved.notes


async def test_a_save_that_moves_only_what_a_release_runs_early_regrades_only_while_released(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    files_only = RELEASING.replace(
        "on_release: [st2, st2_tests, tests/4.in]", "on_release: [tests/4.in]"
    )
    islands, _ = await _graded_both(setup, acme, files_only)

    quiet = await _try_save(setup, acme, islands.task, RELEASING)
    assert isinstance(quiet, Published)
    assert (quiet.grading_changed, quiet.changes) == (False, ())
    assert isinstance(await _try_save(setup, acme, islands.task, files_only), Published)

    await _release(setup, acme, islands.task)
    moved = await _try_save(setup, acme, islands.task, RELEASING)
    assert isinstance(moved, Published)
    # Checked first by its save row, it grades again once that row passes.
    assert (moved.grading_changed, moved.changes) == (True, (RELEASE_MOVED,))


async def test_while_released_a_late_row_tells_its_entrant_only_for_a_grading_made_before(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.set(BEFORE_CLOSE)
    text = TASKS["islands"][0].replace(
        "after_close: [st1, st2, total, all]", "after_close: [all]\n    on_release: [st1_tests]"
    )
    islands = await _checked(setup, acme, text)
    await _submit(setup, acme, islands, user=8)
    await _release(setup, acme, islands.task)
    await _submit(setup, acme, islands, user=9)
    told: list[int | None] = []
    tell = gradings.told_late

    def heard(ctx: Context, row: Grading) -> None:
        told.append(row.submitted_by)
        tell(ctx, row)

    monkeypatch.setattr(gradings, "told_late", heard)
    late: dict[int, Grading] = {}
    for user in (8, 9):
        early = {row.submitted_by: row for row in await _rows(setup, "early")}[user]
        if user == 8:
            await _early(setup, acme, early.id)
        else:
            # The same code compiled once already, for 8.
            await _step(setup, acme, early.id, "early", "solution", _batch())
            await _step(setup, acme, early.id, "early", "compare", _batch(score=True))
            await _report(setup, early.id, "early")
            await setup.settle()
        late[user] = {row.submitted_by: row for row in await _rows(setup, "late")}[user]
        if late[user].status != GradingStatus.DONE:
            await _step(setup, acme, late[user].id, "late", "solution", _batch())
            await _step(setup, acme, late[user].id, "late", "compare", _batch(score=True))
            await _report(setup, late[user].id, "late")
        assert {row.submitted_by: row for row in await _rows(setup, "late")}[user].status == (
            GradingStatus.DONE
        )

    # The grading made while released ran r3 early, so its late row's end
    # shows nothing new, and telling it would only time the hidden run.
    assert (late[8].released_split, late[9].released_split) == (False, True)
    assert told == [8]
