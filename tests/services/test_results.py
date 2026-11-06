"""Staff releasing a task's results and taking a release back
(SIMPLER-CORE.md 9.4, stage 5 section 6 items 2 and 10): what `after_release`
lists is shown to entrants, its values and runs, only while the
results are released, and an entrant reads the runs their grading showed
alone until the reveal but while released; a take-back warns before it hides;
each action is a row with who and when, added only when it changes the state;
gradings made while released take the split that shows `after_release` and
runs it first, and a grading keeps the split it was made with; an extension, or a later close,
is refused while released and allowed after a take-back; a save may not drop what a
release shows while it stands, nor have a value shown during the contest
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
    results,
    steps,
    task_save,
    tasks,
    timelines,
)
from forge.services.results import ResultsState
from forge.services.task_save import Draft, Published
from forge.services.task_save.records import Preparing
from forge.testing import FakeClock, prepared
from tests.services.conftest import SPRING, Acme
from tests.services.test_pipelines import (
    AFTER_CLOSE,
    BEFORE_CLOSE,
    ISLANDS,
    LIVE,
    _checked,
    _checked_again,
    _graded,
    _graded_submission,
    _grading,
    _rows,
    _runs_seen,
    _submit,
    _values,
)
from tests.services.test_regrades import _held
from tests.services.test_run_task_saves import TASKS
from tests.test_run_task import ISLANDS_AFTER

AFTER_ST1 = """\
    after_deadline:
      values: [st1]
      runs: [{of: all, fields: [verdict, passed, score, time_ms]}]
"""
RELEASING = TASKS["islands"][0].replace(
    ISLANDS_AFTER,
    AFTER_ST1 + "    after_release:\n      values: [st2]\n"
    "      runs: [{of: st2_tests, fields: [input, verdict, passed, score]}]\n",
)
"""islands showing st2, and its runs with their inputs, on a release."""
DROPPED = RELEASING.replace(
    "{of: st2_tests, fields: [input, verdict, passed, score]}",
    "{of: st2_tests, fields: [verdict, passed, score]}",
)
"""RELEASING with its released runs' inputs no longer shown."""
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


async def test_what_after_release_lists_is_shown_only_while_the_results_are_released(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_submission(setup, acme, RELEASING)
    hidden = {"sample_score": 1, "sample_time": 12}
    samples = [("sample_tests", 1, "accepted"), ("sample_tests", 2, "accepted")]
    shown = [*samples, ("st2_tests", 1, "accepted"), ("st2_tests", 2, "accepted")]
    inputs = [{"file": "tests/3.in"}, {"file": "tests/4.in"}]

    async def seen() -> tuple[dict[str, Any], list[Any], list[Any]]:
        grading = await _grading(setup, session, islands)
        given = [
            run["tuples"][0]["key"]["input"] for run in grading.runs if run["group"] == "st2_tests"
        ]
        return _values(grading), _runs_seen(grading), given

    assert await seen() == (hidden, samples, [])
    await _release(setup, acme, islands.task)
    assert await seen() == ({**hidden, "st2": 1}, shown, inputs)
    await _hide(setup, acme, islands.task)
    assert await seen() == (hidden, samples, [])
    await _release(setup, acme, islands.task)
    assert await seen() == ({**hidden, "st2": 1}, shown, inputs)
    # A test file is never served beside the statement, shown or not.
    assert await _file(setup, islands.task, "tests/4.in") is None


async def test_an_entrant_reads_the_runs_its_grading_showed_alone_until_the_reveal_or_released(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_submission(setup, acme, RELEASING)
    # st1 moved to always shows r3 for new gradings; this one did not show it.
    sooner = RELEASING.replace(
        "values: [sample_score, sample_time]", "values: [sample_score, sample_time, st1]"
    ).replace("      values: [st1]\n", "")
    assert isinstance(await _try_save(setup, acme, islands.task, sooner), Published)
    await setup.settle()
    await _checked_again(setup, acme)

    async def st1() -> Any:
        return _values(await _grading(setup, session, islands))["st1"]

    # From the runs this grading showed st1 reads r3, which it did not show,
    # so it is missing (item 134); from every run, 1.
    assert await st1() is None
    await _release(setup, acme, islands.task)
    assert await st1() == 1
    await _hide(setup, acme, islands.task)
    assert await st1() is None
    clock.set(AFTER_CLOSE)
    assert await st1() == 1


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
        "under after_release: st2, input of st2_tests, verdict of st2_tests, passed of "
        "st2_tests, score of st2_tests. Gradings from now on run those runs first. You can "
        "hide the results again later, though entrants may have seen them by then."
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
    by = {row.submitted_by: row for row in await _rows(setup, "grading")}
    before, during = by[8], by[9]
    assert (before.released_split, during.released_split) == (False, True)
    assert await early_runs(before) == {"r1", "r2"}
    assert await early_runs(during) == {"r1", "r2", "r3", "r4"}
    assert await _graded(setup, acme, before.id) is GradingStatus.DONE

    await _hide(setup, acme, islands.task)
    acme.fake.add_user(10, "dee")
    await _submit(setup, acme, islands, user=10)
    after = {row.submitted_by: row for row in await _rows(setup, "grading")}[10]
    assert after.released_split is False
    assert await early_runs(after) == {"r1", "r2"}

    # A regrade made while released takes the released split.
    await _release(setup, acme, islands.task)
    await gradings.rejudge(setup, acme.ada, islands.task)
    again = [row for row in await _rows(setup, "grading") if row.attempt == 2]
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


async def test_an_extension_warns_both_while_released_and_after_a_take_back(
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
    for taken_back in (False, True):
        if taken_back:
            await _hide(setup, acme, islands.task)
        with pytest.raises(ConfirmationRequired) as asked:
            await entrants.extend(setup, manager, SPRING, entrant, timedelta(minutes=30))
        assert len(asked.value.hiding) == 1
        assert "islands's results were shown since" in asked.value.hiding[0]
        assert "this entrant submit knowing them" in asked.value.hiding[0]
    extended = await entrants.extend(
        setup, manager, SPRING, entrant, timedelta(minutes=30), confirm=True
    )
    assert extended.time_extension == timedelta(minutes=30)


async def _try_save(
    setup: Setup, acme: Acme, task: TaskId, text: str
) -> Published | Draft | Preparing:
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {"task.yaml": Edit(text.encode(), head.tokens.get("task.yaml"))}
    try:
        result = await task_save.save(setup, acme.ada, task, edits, confirm=True)
    except ConfirmationRequired as asked:
        result = await task_save.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.extra["asked"]
        )

    return await prepared(setup, acme.ada, task, result)


def _errors(result: Published | Draft | Preparing) -> list[tuple[str, str]]:
    assert isinstance(result, Draft), result
    return [(error["path"], error["message"]) for error in result.errors]


async def _warn_save(setup: Setup, acme: Acme, task: TaskId, text: str) -> tuple[str, ...]:
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {"task.yaml": Edit(text.encode(), head.tokens.get("task.yaml"))}
    with pytest.raises(ConfirmationRequired) as asked:
        await task_save.save(setup, acme.ada, task, edits, confirm=True)
    assert (await acme.fake.content.list_files(PLATFORM, task)).version == head.version
    assert asked.value.hiding
    assert isinstance(
        await task_save.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.value.extra["asked"]
        ),
        Published,
    )
    return asked.value.hiding


async def test_a_save_warns_about_a_releases_inputs_even_after_a_take_back(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_submission(setup, acme, RELEASING)
    await _release(setup, acme, islands.task)
    await _hide(setup, acme, islands.task)
    lines = await _warn_save(setup, acme, islands.task, DROPPED)
    assert len(lines) == 1
    assert "input of 2 runs once results are released" in lines[0]
    assert "seen or kept" in lines[0]


async def test_a_group_reading_fewer_shown_runs_warns_then_saves(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_submission(setup, acme)
    fewer = TASKS["islands"][0].replace(
        "sample_tests: {input: [samples/1.in, samples/2.in]}",
        "sample_tests: {input: [samples/1.in]}",
    )
    lines = await _warn_save(setup, acme, islands.task, fewer)
    fewer_lines = [line for line in lines if "read 1 fewer" in line]
    assert len(fewer_lines) == 2
    assert any("sample_score during the contest over 2 runs" in line for line in fewer_lines)
    assert any("sample_time during the contest over 2 runs" in line for line in fewer_lines)
    # And each field sample_tests showed of the run it no longer holds.
    hidden = [line for line in lines if line not in fewer_lines]
    assert [line.split("shown ")[1].split(" of")[0] for line in hidden] == [
        "input",
        "answer",
        "verdict",
        "passed",
        "score",
        "time_ms",
    ]
    assert all("of 1 run as soon as a grading ends" in line for line in hidden)


async def test_later_close_warns_about_results_even_after_a_take_back(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)

    def settings(minute: int) -> ContestDefinition:
        entry = f"  - {{id: islands, closes: 2026-09-26T14:{minute}:00Z}}\n"
        return parse_contest(LIVE.replace("  - id: sum\n", "").replace("  - id: islands\n", entry))

    async def warning() -> tuple[str, ...]:
        async with setup.unit_of_work() as ctx:
            found = await timelines.check_contest(ctx, SPRING, settings(55), settings(58))
        assert found.problems == []
        return found.hiding

    assert await warning() == ()
    await _release(setup, acme, islands.task)
    first = await warning()
    assert len(first) == 1
    assert "islands's results were shown to entrants since" in first[0]
    await _hide(setup, acme, islands.task)
    assert await warning() == first


OPEN = RELEASING.replace(
    "      runs: [{of: st2_tests, fields: [input, verdict, passed, score]}]\n", ""
).replace(
    "      runs: [{of: all, fields: [verdict, passed, score, time_ms]}]\n",
    "      runs:\n"
    "        - {of: all, fields: [verdict, passed, score, time_ms]}\n"
    "        - {of: st2_tests, fields: [input, verdict, passed, score]}\n",
)
"""RELEASING with st2_tests' runs and their inputs shown after the deadline
instead."""


async def test_released_files_moving_between_release_and_reveal_warn_only_when_hidden(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_submission(setup, acme, OPEN)
    await _release(setup, acme, islands.task)
    assert isinstance(await _try_save(setup, acme, islands.task, RELEASING), Published)
    await _checked_again(setup, acme)
    # Each field of st2_tests' two runs moved, its input among them.
    before = await _warn_save(setup, acme, islands.task, OPEN)
    assert len(before) == 4
    assert all("of 2 runs once results are released" in line for line in before)
    assert any("input of 2 runs" in line for line in before)
    clock.set(AFTER_CLOSE)
    # After the deadline all shows the rest anyway: only the inputs move.
    after = await _warn_save(setup, acme, islands.task, RELEASING)
    assert len(after) == 1
    assert "input of 2 runs after the deadline" in after[0]


async def test_what_no_release_showed_never_holds_a_save_back(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_submission(setup, acme, RELEASING)
    assert isinstance(await _try_save(setup, acme, islands.task, DROPPED), Published)

    await _release(setup, acme, islands.task)

    again = DROPPED.replace("max: 50", "max: 49")
    assert isinstance(await _try_save(setup, acme, islands.task, again), Published)


async def test_a_value_shown_before_what_it_reads_is_refused_and_a_group_shown_sooner_noted(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, RELEASING)
    sooner = RELEASING.replace("values: [st1]", "values: [st1, total]")

    assert _errors(await _try_save(setup, acme, islands.task, sooner)) == [
        (
            "scoring.show.after_deadline.values[1]",
            f"total reads {name}, which is shown once results are released, so showing total "
            f"after the deadline would give {name} away sooner. List total no sooner than "
            f"{name}.",
        )
        for name in ("st2", "st2_tests")
    ]
    saved = await _try_save(setup, acme, islands.task, RELEASING.replace("max: 50", "max: 49"))
    assert isinstance(saved, Published)
    assert (
        "all, shown after the deadline, holds runs st2 reads, so entrants see those runs before "
        "a release shows st2."
    ) in saved.notes


async def test_a_save_that_moves_only_what_a_release_shows_regrades_only_while_released(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    files_only = RELEASING.replace("      values: [st2]\n", "").replace(
        "{of: st2_tests, fields: [input, verdict, passed, score]}",
        "{of: st2_tests, fields: [input]}",
    )
    islands, _ = await _graded_submission(setup, acme, files_only)

    quiet = await _try_save(setup, acme, islands.task, RELEASING)
    assert isinstance(quiet, Published)
    assert (quiet.grading_changed, quiet.changes) == (False, ())
    assert isinstance(await _try_save(setup, acme, islands.task, files_only), Published)

    await _release(setup, acme, islands.task)
    moved = await _try_save(setup, acme, islands.task, RELEASING)
    assert isinstance(moved, Published)
    # Checked first by its save row, it grades again once that row passes.
    assert (moved.grading_changed, moved.changes) == (True, (RELEASE_MOVED,))


async def test_a_new_release_asks_again_when_it_adds_to_a_saves_hiding_lines(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_submission(setup, acme, RELEASING)
    content = DROPPED.replace("values: [sample_score, sample_time]", "values: [sample_score]")
    head = await acme.fake.content.list_files(PLATFORM, islands.task)
    changes = {"task.yaml": Edit(content.encode(), head.tokens["task.yaml"])}
    with pytest.raises(ConfirmationRequired) as first:
        await task_save.save(setup, acme.ada, islands.task, changes)
    assert len(first.value.hiding) == 1
    await _release(setup, acme, islands.task)
    with pytest.raises(ConfirmationRequired) as second:
        await task_save.save(
            setup, acme.ada, islands.task, changes, confirm=True, asked=first.value.extra["asked"]
        )
    assert len(second.value.hiding) == 2
    assert second.value.extra["asked"] != first.value.extra["asked"]
    assert (await acme.fake.content.list_files(PLATFORM, islands.task)).version == head.version
    assert isinstance(
        await task_save.save(
            setup, acme.ada, islands.task, changes, confirm=True, asked=second.value.extra["asked"]
        ),
        Published,
    )


async def test_a_release_during_package_preparation_does_not_publish_unconfirmed_hiding(
    setup: Setup,
    acme: Acme,
    spring: str,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import asynccontextmanager

    from forge.domain.environment import Environment, EnvironmentLock
    from forge.port.packages import PreparedEnvironment
    from forge.services.task_save import preparing

    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_submission(setup, acme, RELEASING)
    before = await acme.fake.workspaces.list_publications(islands.task)
    content = DROPPED.replace("values: [sample_score, sample_time]", "values: [sample_score]")
    content += "\nenvironment:\n  python:\n    packages: [numpy]\n"
    head = await acme.fake.content.list_files(PLATFORM, islands.task)
    changes = {"task.yaml": Edit(content.encode(), head.tokens["task.yaml"])}
    with pytest.raises(ConfirmationRequired) as asked:
        await task_save.save(setup, acme.ada, islands.task, changes)
    assert len(asked.value.hiding) == 1
    entered, released = asyncio.Event(), asyncio.Event()
    resolve = acme.fake.packages.prepare

    @asynccontextmanager
    async def delayed(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        entered.set()
        await released.wait()
        async with resolve(asked, cap=cap, previous=previous, proceed=proceed) as value:
            yield value

    monkeypatch.setattr(acme.fake.packages, "prepare", delayed)
    pending = await task_save.save(
        setup, acme.ada, islands.task, changes, confirm=True, asked=asked.value.extra["asked"]
    )
    assert isinstance(pending, Preparing)
    await asyncio.wait_for(entered.wait(), 5)
    await _release(setup, acme, islands.task)
    released.set()
    await setup.settle()
    state = await preparing.read(setup, acme.ada, islands.task)
    assert state.status == "refused" and "shown" in (state.reason or ""), state
    assert await acme.fake.workspaces.list_publications(islands.task) == before
    with pytest.raises(ConfirmationRequired) as changed:
        await task_save.save(setup, acme.ada, islands.task, {})
    assert len(changed.value.hiding) == 2
    final = await task_save.save(
        setup, acme.ada, islands.task, {}, confirm=True, asked=changed.value.extra["asked"]
    )
    assert isinstance(final, Published)
