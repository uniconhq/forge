"""The inputs `show` shows, and the staff preview of a draft `show`
(SIMPLER-CORE.md 9.4): the task's page lists the runs whose inputs are
shown at a moment that has come, and the shared inputs shown then, each
file served through the download door by the run's group and place, to a
visitor of a public contest as to an entrant, and anything not shown now
not found as one not there; a folder serves only the files it holds;
the owner of a submission fetches the input of the first run it did not
pass; and the preview answers what an entrant would read at a moment of
a made-up graded submission under the draft, with nothing published,
with the count of runs it hides, to an observer of the task alone.
"""

import pytest

from forge.domain.errors import Forbidden, NotFound
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.roles import Scope
from forge.domain.types import Type
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition, path_text
from forge.runtime.setup import Setup
from forge.services import contest_home, entrant_view, landing, submissions
from forge.testing import FakeClock
from tests.services.conftest import SPRING, Acme, make_task
from tests.services.test_marks import _observer_of
from tests.services.test_pipelines import (
    BEFORE_CLOSE,
    _checked,
    _rows,
    _submit,
)
from tests.services.test_run_task_saves import TASKS
from tests.services.test_steps import _batch, _built, _report, _save, _step
from tests.test_run_task import ISLANDS_ALWAYS, deployed

SHOWN = TASKS["islands"][0].replace(
    ISLANDS_ALWAYS,
    ISLANDS_ALWAYS + "      inputs: [time_limit]\n",
)
"""islands showing its time limit, and its samples' input and answer, as
soon as a grading ends."""
FIRST = TASKS["islands"][0].replace(
    ISLANDS_ALWAYS,
    "    always:\n      runs: [{of: first_not_passed, fields: [input, verdict]}]\n",
)
"""islands showing, of each submission, the input and verdict of the
first run it did not pass, and nothing else as soon as a grading ends."""


async def test_the_task_page_lists_the_runs_and_settings_shown_now_and_serves_their_files(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, SHOWN)
    session = await _submit(setup, acme, islands)

    page = await contest_home.task(setup, session, islands.task)

    assert page.shown_inputs == {"time_limit": 2}
    assert [(run["group"], run["place"]) for run in page.runs] == [
        ("sample_tests", 1),
        ("sample_tests", 2),
    ]
    assert page.runs[0]["tuples"][0]["key"] == {
        "input": {"file": "samples/1.in"},
        "answer": {"file": "samples/1.ans"},
    }
    found = await contest_home.shown_input(setup, session, islands.task, "input", "sample_tests", 2)
    assert found.door is not None and found.files == ()
    # Saved under its own name, which the run's key above already shows.
    assert found.name == "2.in"
    # A run whose input is not shown, a setting given inline, and one not
    # shown at all are each not found, telling nothing of what is hidden.
    for name, group, place in (
        ("input", "all", 3),
        ("input", "sample_tests", 3),
        ("time_limit", None, None),
        ("memory_limit", None, None),
    ):
        with pytest.raises(NotFound):
            await contest_home.shown_input(setup, session, islands.task, name, group, place)


async def test_a_visitor_fetches_a_shown_input_and_a_hidden_one_is_as_one_not_there(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, SHOWN)

    found = await landing.shown_input(setup, islands.task, "input", "sample_tests", 1)

    assert found.door is not None and found.files == ()
    with pytest.raises(NotFound) as hidden:
        await landing.shown_input(setup, islands.task, "input", "all", 3)
    for name, group, place in (("nothing", None, None), ("input", "nowhere", 1)):
        with pytest.raises(NotFound) as missing:
            await landing.shown_input(setup, islands.task, name, group, place)
        assert str(missing.value) == str(hidden.value)


async def test_a_listing_of_a_shown_file_opens_no_door(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, SHOWN)
    acme.fake.reset_calls()

    found = await landing.shown_input(setup, islands.task, "input", "sample_tests", 1, listing=True)

    assert (found.door, found.name, found.files) == (None, "1.in", ())
    assert acme.fake.calls_to("media_door") == []


async def test_a_shown_folder_serves_the_files_it_holds_and_no_other_path(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    async with setup.unit_of_work() as ctx:
        published = await entrant_view.task(
            ctx, islands.task, await entrant_view.contest(ctx, SPRING)
        )
        assert published is not None
        folder = (Type.FOLDER, "samples")

        listed = await entrant_view.shown_input(ctx, published, folder, None)
        one = await entrant_view.shown_input(ctx, published, folder, "1.in")
        for path in ("9.in", "../tests/3.in", "samples/1.in"):
            with pytest.raises(NotFound, match=r"There is no such file\."):
                await entrant_view.shown_input(ctx, published, folder, path)

    assert listed.files == ("1.ans", "1.in", "2.ans", "2.in") and listed.door is None
    assert listed.name is None
    assert one.door is not None and one.name == "1.in"


async def test_the_owner_fetches_the_input_of_the_first_run_their_submission_did_not_pass(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, FIRST)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "grading")
    await _step(setup, acme, early.id, "grading", "solution", _batch({"r3": "wrong_answer"}))
    await _step(setup, acme, early.id, "grading", "compare", _batch(score=True))
    await _report(setup, early.id, "grading")
    await setup.settle()

    [mine] = await submissions.mine(setup, session, islands.task)
    assert mine.grading is not None
    assert [(run["group"], run["place"], run["verdict"]) for run in mine.grading.runs] == [
        ("first_not_passed", 1, "wrong_answer")
    ]
    found = await submissions.shown_input(
        setup, session, islands.task, 1, "first_not_passed", 1, "input"
    )
    assert found.door is not None
    for group, place, name in (
        ("first_not_passed", 1, "answer"),
        ("first_not_passed", 2, "input"),
        ("all", 3, "input"),
    ):
        with pytest.raises(NotFound):
            await submissions.shown_input(setup, session, islands.task, 1, group, place, name)
    # The task's page shows no run of first_not_passed: it is a
    # submission's own.
    assert (await contest_home.task(setup, session, islands.task)).runs == ()


async def test_the_preview_answers_the_entrants_page_of_a_made_up_submission_at_a_moment(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    draft = {
        "always": {"values": ["sample_score"]},
        "after_deadline": {"values": ["st1"]},
        "after_release": {
            "values": ["st2"],
            "runs": [{"of": "st2_tests", "fields": ["input", "score"]}],
        },
    }

    before = await submissions.preview(setup, acme.ada, islands.task, draft, "always")
    released = await submissions.preview(setup, acme.ada, islands.task, draft, "after_release")

    # Each moment is read alone: a release shows st2 and its runs, not
    # what the deadline shows.
    assert [value.name for value in before.result.run_values] == ["sample_score"]
    assert all(value.number is not None for value in before.result.run_values)
    assert (before.result.runs, before.hidden) == ((), len(before.sample))
    assert [value.name for value in released.result.run_values] == ["sample_score", "st2"]
    assert [(run["group"], run["place"]) for run in released.result.runs] == [
        ("st2_tests", 1),
        ("st2_tests", 2),
    ]
    assert released.hidden == len(released.sample) - 2
    # The runs take turns to pass, the same in every answer.
    assert [row["verdict"] for row in before.sample[:2]] == ["accepted", "wrong_answer"]
    assert before.sample == released.sample


async def test_the_preview_is_refused_to_one_who_does_not_observe_the_task(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    elsewhere = _observer_of(acme, Scope("acme", "spring", "sum"))

    with pytest.raises(Forbidden):
        await submissions.preview(setup, elsewhere, islands.task, {}, "always")


async def test_the_preview_checks_the_draft_alone(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)

    with pytest.raises(InvalidDefinition) as refused:
        await submissions.preview(
            setup,
            acme.ada,
            islands.task,
            {
                "always": {"values": ["nothing"], "outputs": ["time_ms"]},
                "after_deadline": {"values": ["st1"]},
                "after_release": {"values": ["total"]},
            },
            "always",
        )
    assert [path_text(problem["path"]) for problem in refused.value.errors] == [
        "scoring.show.always.values[0]",
        "scoring.show.always.outputs[0]",
        "scoring.show.after_release.values[0]",
    ]
    with pytest.raises(InvalidDefinition):
        await submissions.preview(setup, acme.ada, islands.task, {"now": ["total"]}, "always")
    with pytest.raises(InvalidDefinition):
        await submissions.preview(setup, acme.ada, islands.task, {}, "now")


GAME = """\
name: Guess
workflow: unicon/game@v1
inputs:
  code: {entrant: {label: Your solution, max_size: 1MB}}
  language: {entrant: {options: [python, cpp], default: python}}
  judge: judge/
  judge_language: python
  judge_manifest: judge/program.yaml
  time_limit: 1
  memory_limit: 256
  judge_time_limit: 3
  judge_memory_limit: 256
repeat:
  - inputs: [input]
    tuples: [tests/1.in, tests/2.in]
scoring:
  values:
    solved: {block: min, over: {runs: all}, of: {run: [all, passed]}}
  show:
    always:
      values: [solved]
"""
"""A task over `unicon/interactive@v4`, whose `log` is its step's
`entrant.log`, given once for the batch."""


async def test_the_preview_takes_an_output_given_once_under_its_ports_name(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    task = await make_task(setup, acme, "guess")
    workflow = await acme.fake.workflows.create_workflow(
        PLATFORM, "unicon", "game", deployed("unicon/interactive@v4")[1], Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, workflow, "v1")
    await _save(
        setup,
        acme,
        task,
        {
            "task.yaml": GAME.encode(),
            "statement.md": b"Guess the number.\n",
            "judge/judge.py": b"print('judged')\n",
            "judge/program.yaml": b"reports:\n  guesses: {better: lower, at_least: 0}\n",
            "tests/1.in": b"1\n",
            "tests/2.in": b"2\n",
        },
    )

    # Nothing is published yet: the preview reads the saved draft.
    assert (await submissions.preview(setup, acme.ada, task, {}, "always")).result.runs == ()
    [save] = await _rows(setup, "save")
    for step in ("build-game-judge", "build-decide"):
        await _step(setup, acme, save.id, "save", step, _built())
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()

    await submissions.preview(setup, acme.ada, task, {"always": {"outputs": ["log"]}}, "always")
    with pytest.raises(InvalidDefinition) as refused:
        await submissions.preview(
            setup,
            acme.ada,
            task,
            {"always": {"runs": [{"of": "all", "fields": ["log"]}]}},
            "always",
        )
    assert [path_text(problem["path"]) for problem in refused.value.errors] == [
        "scoring.show.always.runs[0].fields[0]"
    ]
