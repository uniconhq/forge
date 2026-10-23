"""A run task (SIMPLER-CORE.md 8 and 9): the islands, cartpole and tour
tasks parse into their runs, keys and early runs; repeat crosses its axes,
first outermost, keeps each tuple's values together and keys a run an
entrant fills by its part; each refusal of 8.5 and 9.5 names its path; a
run keeps its id across versions; what the entrant could set is marked
self-reported; what is shown when, and what was shown, is read from
`show`; and a check gives what the entrant does and expects what the task
can get, a file only a check names never shown.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from forge.domain.blocks.numbers import Better
from forge.domain.checks import check_only
from forge.domain.definitions import Scoring, parse_task
from forge.domain.grading import PLATFORM_MACHINE, Machine
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.run_task import (
    MOST_RUNS,
    Part,
    ProgramValue,
    RunsFile,
    RunTask,
    RunTaskCheck,
    Shown,
    check_run_task,
    fewer_runs,
    file_time,
    hidden_again,
    manifest_files,
    most_shown,
    self_reported_question,
    shown_under,
    unreleased,
)
from forge.domain.run_workflow import WorkflowFiles, check_run_workflow
from forge.domain.types import Type
from forge.domain.workflow_definition import parse_run_workflow
from forge.domain.yaml_models import InvalidDefinition
from forge.services.published import shown_now
from forge.testing import PLACEHOLDER_DIGEST, RUN_WORKFLOWS
from tests.conftest import sibling


def mirrored(name: str) -> PrimitiveDeclaration:
    text = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    return parse_primitive(f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n{text}")


@pytest.fixture(scope="module")
def primitives() -> dict[str, PrimitiveDeclaration]:
    return {
        "unicon/exec@v1": mirrored("exec"),
        "unicon/interact@v2": mirrored("interact"),
        "unicon/compile@v3": mirrored("compile"),
    }


def deployed(ref: str) -> tuple[str, dict[str, bytes]]:
    """A built-in run workflow's `workflow.yaml` and its files, from deploy's
    checkout.
    """
    top = sibling("deploy", RUN_WORKFLOWS[ref])
    held = {
        str(path.relative_to(top)): path.read_bytes()
        for path in top.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    return held["workflow.yaml"].decode(), held


CARTPOLE = """\
inputs:
  weights: {type: file, title: Trained weights}
  episodes: number
  env: text
  seed: number
steps:
  - id: runner
    use: unicon/compile@v3
    with: {source: "${{ workflow.runner/ }}", language: python}
  - id: episode
    use: unicon/exec@v1
    with:
      program: ${{ steps.runner.program }}
      manifest: ${{ workflow.runner/program.yaml }}
      loads: {weights: "${{ inputs.weights }}"}
      args: "--env ${{ inputs.env }} --seed ${{ inputs.seed }} --episodes ${{ inputs.episodes }}"
      time_limit: 60
      memory_limit: 1024
reports:
  reward: ${{ steps.episode.reward }}
"""
"""An RL task's run workflow: the organiser's runner loads the entrant's
weights and reports the reward, so the reward, the verdict and `passed` are
the entrant's to set."""
CARTPOLE_FILES = {
    "workflow.yaml": CARTPOLE.encode(),
    "runner/run.py": b"print('reward')\n",
    "runner/program.yaml": b"reports:\n  reward: {better: higher, at_least: 0}\n",
}

TOUR = """\
inputs:
  input: file
  route: file
  best: number
steps:
  - id: checker
    use: unicon/compile@v3
    with: {source: "${{ workflow.checker/ }}", language: python}
  - id: check
    use: unicon/exec@v1
    with:
      program: ${{ steps.checker.program }}
      manifest: ${{ workflow.checker/program.yaml }}
      files: {input: "${{ inputs.input }}", route: "${{ inputs.route }}"}
      args: "${{ inputs.best }}"
      time_limit: 5
      memory_limit: 256
reports:
  length: ${{ steps.check.length }}
  score: ${{ steps.check.score }}
"""
"""An output-only task's run workflow: a checker reads the entrant's route
for one input."""
TOUR_FILES = {
    "workflow.yaml": TOUR.encode(),
    "checker/check.py": b"print(1)\n",
    "checker/program.yaml": (
        b"reports:\n  length: {better: lower, at_least: 0}\n"
        b"  score: {better: higher, at_least: 0, at_most: 1}\n"
    ),
}

JUDGED = """\
inputs:
  code: folder
  judge: folder
  judge_manifest: file
  time_limit: number
  judge_time_limit: number
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: judge
    use: unicon/compile@v3
    with: {source: "${{ inputs.judge }}", language: python}
  - id: game
    use: unicon/interact@v2
    with:
      programs:
        entrant:
          program: ${{ steps.compile.program }}
          time_limit: ${{ inputs.time_limit }}
          memory_limit: 256
        judge:
          program: ${{ steps.judge.program }}
          manifest: ${{ inputs.judge_manifest }}
          time_limit: ${{ inputs.judge_time_limit }}
          memory_limit: 256
      links: [entrant -> judge, judge -> entrant]
reports:
  moves: {from: "${{ steps.game.judge.moves }}", unit: moves}
"""
"""An interactive workflow whose judge's manifest is the task's, so what
reads its reports is checked at the task's save."""

ISLANDS = """\
name: Islands
workflow: unicon/classic@v3
inputs:
  code: {entrant: {label: Your solution, max_size: 1MB}}
  language: {entrant: {options: [cpp, python], default: cpp}}
  time_limit: 2
  memory_limit: 256
repeat:
  - inputs: [input, answer]
    tuples:
      - [samples/1.in, samples/1.ans]
      - [samples/2.in, samples/2.ans]
      - [tests/3.in, tests/3.ans]
      - {answer: tests/4.ans, input: tests/4.in}
scoring:
  groups:
    sample_tests: {input: [samples/1.in, samples/2.in]}
    st1_tests: {input: [tests/3.in]}
    st2_tests: {input: [tests/3.in, tests/4.in]}
  values:
    sample_score:
      block: div
      of:
        - {block: sum, over: {runs: sample_tests}, of: {run: [sample_tests, score]}}
        - {block: count, over: {runs: sample_tests}}
    sample_time: {block: max, over: {runs: sample_tests}, of: {run: [sample_tests, time_ms]}}
    st1: {block: min, over: {runs: st1_tests}, of: {run: [st1_tests, score]}}
    st2: {block: min, over: {runs: st2_tests}, of: {run: [st2_tests, score]}}
    total:
      block: add
      of:
        - {block: mul, of: [40, {value: st1}]}
        - {block: mul, of: [60, {value: st2}]}
  show:
    now: [sample_score, sample_time, sample_tests, samples/]
    after_close: [st1, st2, total, all]
submissions: {max: 50, rate: {count: 1, per: 30}}
"""
ISLANDS_PATHS = frozenset(
    {
        "task.yaml",
        "statement.md",
        "samples/1.in",
        "samples/1.ans",
        "samples/2.in",
        "samples/2.ans",
        "tests/3.in",
        "tests/3.ans",
        "tests/4.in",
        "tests/4.ans",
    }
)

CARTPOLE_TASK = """\
name: Cartpole
workflow: cs2109s/cartpole@v1
inputs:
  weights: entrant
  episodes: 100
repeat:
  - inputs: [env]
    tuples: [cartpole, cartpole-wind]
  - inputs: [seed]
    tuples: [1, 2, 3]
scoring:
  groups:
    windy_runs: {env: cartpole-wind}
  values:
    reward:
      block: div
      of:
        - {block: sum, over: {runs: all}, of: {run: [all, reward]}}
        - {block: count, over: {runs: all}}
    windy:
      block: div
      of:
        - {block: sum, over: {runs: windy_runs}, of: {run: [windy_runs, reward]}}
        - {block: count, over: {runs: windy_runs}}
  show:
    now: [reward]
    after_close: [windy, all]
"""

TOUR_TASK = """\
name: Tour
workflow: acme/tour@v1
inputs:
  best: 1200
repeat:
  - inputs: [input, route]
    tuples:
      - [parts/1.in, entrant]
      - [parts/2.in, entrant]
scoring:
  values:
    total: {block: sum, over: {runs: all}, of: {run: [all, score]}}
  show:
    now: [total, all, parts/]
"""
TOUR_PATHS = frozenset({"task.yaml", "parts/1.in", "parts/2.in"})


def check(
    primitives: Mapping[str, PrimitiveDeclaration],
    task: str,
    workflow: str,
    files: Mapping[str, bytes],
    paths: frozenset[str],
    *,
    manifests: Mapping[str, bytes] | None = None,
    previous: RunsFile | None = None,
    submitted: bool = False,
    secrets: frozenset[str] = frozenset(),
    machine: Machine = PLATFORM_MACHINE,
) -> RunTaskCheck:
    definition = parse_task(task, runs=True)
    parsed = parse_run_workflow(workflow)
    checked = check_run_workflow(parsed, primitives, WorkflowFiles(frozenset(files), files))
    assert checked.problems == []
    return check_run_task(
        definition,
        parsed,
        checked,
        paths,
        manifests or {},
        previous=previous,
        submitted=submitted,
        secrets=secrets,
        machine=machine,
    )


def islands(
    primitives: Mapping[str, PrimitiveDeclaration], task: str = ISLANDS, **kw: Any
) -> RunTaskCheck:
    workflow, files = deployed("unicon/classic@v3")
    return check(primitives, task, workflow, files, kw.pop("paths", ISLANDS_PATHS), **kw)


def cartpole(
    primitives: Mapping[str, PrimitiveDeclaration], task: str = CARTPOLE_TASK, **kw: Any
) -> RunTaskCheck:
    return check(
        primitives, task, CARTPOLE, CARTPOLE_FILES, kw.pop("paths", frozenset({"task.yaml"})), **kw
    )


def tour(
    primitives: Mapping[str, PrimitiveDeclaration], task: str = TOUR_TASK, **kw: Any
) -> RunTaskCheck:
    return check(primitives, task, TOUR, TOUR_FILES, kw.pop("paths", TOUR_PATHS), **kw)


def ok(found: RunTaskCheck) -> RunTask:
    assert found.problems == []
    assert found.task is not None
    return found.task


def at(found: RunTaskCheck, path: str) -> list[str]:
    assert found.task is None
    return [problem["message"] for problem in found.problems if problem["path"] == path]


def keys(task: RunTask) -> list[tuple[str, dict[str, object]]]:
    return [(run.id, dict(run.key)) for run in task.runs]


# The three tasks


def test_islands_runs_each_tuple_once_and_shows_the_samples_early(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(islands(primitives))

    assert keys(task) == [
        ("r1", {"input": "samples/1.in", "answer": "samples/1.ans"}),
        ("r2", {"input": "samples/2.in", "answer": "samples/2.ans"}),
        ("r3", {"input": "tests/3.in", "answer": "tests/3.ans"}),
        ("r4", {"input": "tests/4.in", "answer": "tests/4.ans"}),
    ]
    assert task.early() == {"r1", "r2"}
    assert [run.id for run in task.runs if run.early] == ["r1", "r2"]
    assert task.groups["st2_tests"] == ("r3", "r4")
    assert task.groups["all"] == ("r1", "r2", "r3", "r4")
    assert not task.compile_early()
    assert task.self_reported == {}
    assert set(task.entrant) == {"code", "language"}
    assert task.entrant["code"].form.label == "Your solution"
    assert task.settings == {"time_limit": 2, "memory_limit": 256}
    assert task.values.through("total").groups == {"st1_tests", "st2_tests"}
    assert task.runs[0].label == "input samples/1.in, answer samples/1.ans"


def test_cartpole_crosses_two_axes_and_its_shown_reward_makes_every_run_early(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(cartpole(primitives))

    assert [(run.key["env"], run.key["seed"]) for run in task.runs] == [
        ("cartpole", 1),
        ("cartpole", 2),
        ("cartpole", 3),
        ("cartpole-wind", 1),
        ("cartpole-wind", 2),
        ("cartpole-wind", 3),
    ]
    assert [run.id for run in task.runs] == ["r1", "r2", "r3", "r4", "r5", "r6"]
    assert task.early() == {run.id for run in task.runs}
    assert task.groups["windy_runs"] == ("r4", "r5", "r6")


def test_cartpole_marks_what_the_entrants_weights_could_set(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(cartpole(primitives))

    assert task.self_reported == {
        "reward": {"weights"},
        "verdict": {"weights"},
        "passed": {"weights"},
    }
    assert task.self_reported_values() == {"reward", "windy"}
    assert task.runs_file().self_reported == task.self_reported
    # The save keeps the values it checked, which a result marks.
    read = RunsFile.from_bytes(task.runs_file().to_bytes())
    assert read.self_reported_values == {"reward", "windy"}


def test_tour_asks_for_one_route_per_part(primitives: dict[str, PrimitiveDeclaration]) -> None:
    task = ok(tour(primitives))

    assert keys(task) == [
        ("r1", {"input": "parts/1.in", "route": Part(1)}),
        ("r2", {"input": "parts/2.in", "route": Part(2)}),
    ]
    assert task.entrant["route"].parts == 2
    assert task.entrant["route"].type is Type.FILE
    assert task.runs[1].label == "input parts/2.in, route Part 2"
    assert task.early() == {"r1", "r2"}
    assert task.settings == {"best": 1200}


def test_a_number_the_entrant_gives_in_parts_is_a_self_reported_column(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = (
        TOUR_TASK.replace("inputs:\n  best: 1200\n", "")
        .replace("inputs: [input, route]", "inputs: [input, route, best]")
        .replace("entrant]", "entrant, entrant]")
        .replace(
            "  values:\n",
            "  values:\n    claimed: {block: sum, over: {runs: all}, of: {run: [all, best]}}\n",
        )
    )

    task = ok(tour(primitives, text))

    assert task.entrant["best"].parts == 2
    assert task.self_reported == {"best": {"best"}}
    assert task.self_reported_values() == {"claimed"}


# repeat


CROSSED_ENTRANT = """\
name: Tour
workflow: acme/tour@v1
inputs:
  best: 1200
  input: parts/1.in
repeat:
  - inputs: [route]
    tuples: [entrant, entrant]
  - inputs: [best_known]
    tuples: [1, 2, 3]
scoring:
  show: {now: [all]}
"""


def test_an_axis_the_entrant_fills_crosses_with_another(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    workflow = TOUR.replace("  best: number\n", "  best: number\n  best_known: number\n")
    files = {**TOUR_FILES, "workflow.yaml": workflow.encode()}
    task = ok(check(primitives, CROSSED_ENTRANT, workflow, files, TOUR_PATHS))

    assert [(run.key["route"], run.key["best_known"]) for run in task.runs] == [
        (Part(1), 1),
        (Part(1), 2),
        (Part(1), 3),
        (Part(2), 1),
        (Part(2), 2),
        (Part(2), 3),
    ]


def test_a_map_row_fills_each_input_by_its_name(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    """The fourth islands tuple is written answer first; its runs still read
    tests/4.in as input."""
    task = ok(islands(primitives))

    assert task.runs[3].key == {"input": "tests/4.in", "answer": "tests/4.ans"}


@pytest.mark.parametrize(
    ("row", "path", "words"),
    [
        ("{input: tests/4.in, answr: tests/4.ans}", "repeat[0].tuples[3]", "no value for answer"),
        ("{input: tests/4.in, answr: tests/4.ans}", "repeat[0].tuples[3].answr", "not one of"),
        ("[tests/4.in]", "repeat[0].tuples[3]", "has 1 value"),
        ("tests/4.in", "repeat[0].tuples[3]", "as a list"),
        ("[tests/4.in, {secret: key}]", "repeat[0].tuples[3][1]", "never in a tuple"),
    ],
)
def test_a_tuple_must_give_each_input_of_its_axis(row: str, path: str, words: str) -> None:
    text = ISLANDS.replace("{answer: tests/4.ans, input: tests/4.in}", row)
    with pytest.raises(InvalidDefinition) as raised:
        parse_task(text)
    assert any(
        problem["path"] == path and words in problem["message"] for problem in raised.value.errors
    )


def test_a_run_keeps_its_id_and_a_new_key_takes_the_next(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    first = ok(islands(primitives)).runs_file()
    without_two = ISLANDS.replace("      - [samples/2.in, samples/2.ans]\n", "")
    second = ok(islands(primitives, without_two, previous=first)).runs_file()
    added = ISLANDS.replace(
        "      - [tests/3.in, tests/3.ans]\n",
        "      - [tests/3.in, tests/3.ans]\n      - [tests/5.in, tests/5.ans]\n",
    )
    third = ok(
        islands(
            primitives,
            added,
            previous=RunsFile.from_bytes(second.to_bytes()),
            paths=ISLANDS_PATHS | {"tests/5.in", "tests/5.ans"},
        )
    )

    assert [run.id for run in second.runs] == ["r1", "r3", "r4"]
    assert [run.id for run in third.runs] == ["r1", "r2", "r3", "r5", "r4"]
    assert third.runs[3].key["input"] == "tests/5.in"


def test_the_runs_file_reads_back_as_written(primitives: dict[str, PrimitiveDeclaration]) -> None:
    written = ok(tour(primitives)).runs_file()

    read = RunsFile.from_bytes(written.to_bytes())

    assert read == written
    assert read.entrant == {"route": (Type.FILE, 2)}
    assert [run.early for run in read.runs] == [True, True]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("ids",), {"a": "r1", "b": "r1"}),
        (("ids",), {"a": "../r1"}),
        (("ids",), {"a": 1}),
        (("runs", 0, "id"), "r 1"),
        (("runs",), "twice"),
        (("runs", 0, "early"), "yes"),
        (("entrant", "route", "type"), "photo"),
        (("entrant", "route", "parts"), 0),
        (("entrant", "route", "parts"), True),
        (("self_reported",), {"score": [1]}),
        (("compile_early",), 0),
    ],
)
def test_a_runs_file_that_no_save_writes_does_not_read(
    primitives: dict[str, PrimitiveDeclaration], path: tuple[str | int, ...], value: object
) -> None:
    document = json.loads(ok(tour(primitives)).runs_file().to_bytes())
    if value == "twice":
        document["runs"].append(document["runs"][0])
    else:
        held = document
        for part in path[:-1]:
            held = held[part]
        held[path[-1]] = value

    with pytest.raises(ValueError, match="does not read"):
        RunsFile.from_bytes(json.dumps(document).encode())


def test_a_task_has_at_most_so_many_runs(primitives: dict[str, PrimitiveDeclaration]) -> None:
    seeds = ", ".join(str(seed) for seed in range(MOST_RUNS // 2 + 1))
    found = cartpole(primitives, CARTPOLE_TASK.replace("tuples: [1, 2, 3]", f"tuples: [{seeds}]"))

    assert at(found, "repeat") == [
        f"Every combination of these tuples is {2 * (MOST_RUNS // 2 + 1)} runs, and a task can "
        f"have at most {MOST_RUNS}. Give fewer tuples."
    ]


# 8.5: checked at the save


@pytest.mark.parametrize(
    ("old", "new", "path", "words"),
    [
        ("  time_limit: 2\n", "  time_limit: 2\n  colour: red\n", "inputs.colour", "no input"),
        (
            "  time_limit: 2\n",
            "  time_limit: 2\n  input: samples/1.in\n",
            "repeat[0].inputs[0]",
            "already filled at inputs.input",
        ),
        ("  time_limit: 2\n", "", "inputs.time_limit", "Give this input a number"),
        ("  time_limit: 2\n", "  time_limit: two\n", "inputs.time_limit", "must be a number"),
        (
            "  time_limit: 2\n",
            "  time_limit: {secret: key}\n",
            "inputs.time_limit",
            "Only a text input",
        ),
        ("default: cpp", "default: rust", "inputs.language.entrant.default", "must be one of"),
        (
            "[tests/3.in, tests/3.ans]",
            "[tests/3.in, tests/9.ans]",
            "repeat[0].tuples[2][1]",
            "no file tests/9.ans",
        ),
        (
            "[tests/3.in, tests/3.ans]",
            "[tests/3.in, tests/]",
            "repeat[0].tuples[2][1]",
            "does not end with /",
        ),
        (
            "[tests/3.in, tests/3.ans]",
            "[samples/1.in, samples/1.ans]",
            "repeat[0].tuples[2]",
            "same as tuple 1",
        ),
        (
            "[tests/3.in, tests/3.ans]",
            "[tests/3.in, entrant]",
            "repeat[0].tuples[2][1]",
            "Either every tuple",
        ),
    ],
)
def test_each_input_is_filled_once_and_fits(
    primitives: dict[str, PrimitiveDeclaration], old: str, new: str, path: str, words: str
) -> None:
    assert old in ISLANDS
    found = islands(primitives, ISLANDS.replace(old, new))

    assert any(words in message for message in at(found, path)), found.problems


def test_an_input_two_axes_fill_is_refused(primitives: dict[str, PrimitiveDeclaration]) -> None:
    text = CARTPOLE_TASK.replace("  - inputs: [seed]\n", "  - inputs: [env]\n")
    text = text.replace("tuples: [1, 2, 3]", "tuples: [a, b]")

    found = cartpole(primitives, text)

    assert any(
        "already filled at repeat[0].inputs[0]" in each for each in at(found, "repeat[1].inputs[0]")
    )


def test_a_task_needs_an_entrant_input(primitives: dict[str, PrimitiveDeclaration]) -> None:
    text = ISLANDS.replace(
        "  code: {entrant: {label: Your solution, max_size: 1MB}}\n"
        "  language: {entrant: {options: [cpp, python], default: cpp}}\n",
        "  code: samples/\n  language: cpp\n",
    )

    found = islands(primitives, text)

    assert at(found, "inputs") == [
        "No input is the entrant's, so there is nothing to submit. Make one the entrant's, such "
        "as code: entrant."
    ]


def test_an_input_raising_a_limit_cannot_be_the_entrants(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = islands(primitives, ISLANDS.replace("  time_limit: 2\n", "  time_limit: entrant\n"))

    assert at(found, "inputs.time_limit") == [
        "This value raises a limit, so the task must give it, not the entrant."
    ]


def test_a_secret_waits_for_org_secrets_and_then_for_a_secret_port(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = CARTPOLE_TASK.replace("  - inputs: [env]\n    tuples: [cartpole, cartpole-wind]\n", "")
    text = text.replace("  episodes: 100\n", "  episodes: 100\n  env: {secret: gym_key}\n")
    text = text.replace(", windy, all]", ", all]").replace(
        "    windy_runs: {env: cartpole-wind}\n", "    windy_runs: {seed: 1}\n"
    )

    held = cartpole(primitives, text)
    given = cartpole(primitives, text, secrets=frozenset({"gym_key"}))

    assert "Secrets come with org secrets" in at(held, "inputs.env")[0]
    assert at(given, "inputs.env") == [
        "env is a secret, and the workflow writes it into text at steps[1].with.args. Give a "
        "secret whole, to a port of its own."
    ]


def test_a_step_over_the_machines_memory_is_refused_at_its_input(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = islands(primitives, ISLANDS.replace("memory_limit: 256", "memory_limit: 20000"))

    assert at(found, "inputs.memory_limit") == [
        "With memory_limit at 20000, the step solution needs 20256 MB of memory, but no machine "
        "this task may run on has more than 16384."
    ]


def test_the_entrants_inputs_stay_theirs_once_anyone_has_submitted(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    before = ok(islands(primitives)).runs_file()
    text = ISLANDS.replace(
        "  language: {entrant: {options: [cpp, python], default: cpp}}\n", "  language: cpp\n"
    )

    refused = islands(primitives, text, previous=before, submitted=True)
    unsubmitted = islands(primitives, text, previous=before, submitted=False)

    assert at(refused, "inputs.language") == [
        "language was the entrant's, and entrants have submitted, so which inputs are theirs, "
        "and of what type, cannot change."
    ]
    assert unsubmitted.problems == []


def test_an_entrant_inputs_type_stays_once_anyone_has_submitted(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    before = RunsFile({}, (), {"code": (Type.FILE, None), "language": (Type.ENUM, None)}, {}, False)

    found = islands(primitives, previous=before, submitted=True)

    assert at(found, "inputs.code") == [
        "code was a file, and entrants have submitted, so which inputs are theirs, and of what "
        "type, cannot change."
    ]


# A manifest the task gives, and the judge's limit


JUDGED_TASK = """\
name: Game
workflow: acme/game@v1
inputs:
  code: entrant
  judge: judge/
  judge_manifest: judge/program.yaml
  time_limit: 1
  judge_time_limit: 3
scoring:
  values:
    moves: {block: max, over: {runs: all}, of: {run: [all, moves]}}
  show: {now: [moves]}
"""
JUDGED_PATHS = frozenset({"task.yaml", "judge/judge.py", "judge/program.yaml"})
JUDGE_MANIFEST = b"reports:\n  moves: {better: lower, at_least: 0}\n"


def judged(
    primitives: dict[str, PrimitiveDeclaration],
    task: str = JUDGED_TASK,
    manifest: bytes = JUDGE_MANIFEST,
    **kw: Any,
) -> RunTaskCheck:
    files = {"workflow.yaml": JUDGED.encode()}
    return check(
        primitives,
        task,
        JUDGED,
        files,
        JUDGED_PATHS,
        manifests={"judge/program.yaml": manifest},
        **kw,
    )


def test_a_manifest_the_task_gives_is_read_and_its_reports_checked(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    workflow = parse_run_workflow(JUDGED)
    checked = check_run_workflow(workflow, primitives, WorkflowFiles(frozenset({"workflow.yaml"})))

    assert manifest_files(parse_task(JUDGED_TASK), checked) == {"judge/program.yaml"}
    assert ok(judged(primitives)).values.reads["moves"].columns == {("all", "moves")}
    assert at(judged(primitives, manifest=b"reports:\n  score: {}\n"), "inputs.judge_manifest") == [
        "judge/program.yaml reports no moves, which the workflow reads at reports.moves."
    ]
    assert at(
        judged(primitives, manifest=b"reports:\n  moves: {type: text}\n"), "inputs.judge_manifest"
    ) == [
        "judge/program.yaml reports moves as text, and the workflow gives it a unit at "
        "reports.moves, which only a number takes."
    ]
    assert at(judged(primitives, manifest=b"reports: [1]\n"), "inputs.judge_manifest")


def test_manifests_that_disagree_on_a_columns_type_are_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = JUDGED_TASK.replace("  judge_manifest: judge/program.yaml\n", "").replace(
        "scoring:",
        "repeat:\n  - inputs: [judge_manifest]\n    tuples:\n"
        "      - [judge/program.yaml]\n      - [judge/text.yaml]\nscoring:",
    )
    files = {"workflow.yaml": JUDGED.encode()}
    found = check(
        primitives,
        task,
        JUDGED,
        files,
        JUDGED_PATHS | {"judge/text.yaml"},
        manifests={
            "judge/program.yaml": JUDGE_MANIFEST,
            "judge/text.yaml": b"reports:\n  moves: {type: text}\n",
        },
    )

    assert (
        "judge/program.yaml reports moves as a number and judge/text.yaml as text, but step "
        "game writes moves as one output of every run. Give it one type in every file."
    ) in [problem["message"] for problem in found.problems]
    assert found.task is None


def test_manifests_that_disagree_on_a_report_the_workflow_does_not_read_are_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = JUDGED_TASK.replace("  judge_manifest: judge/program.yaml\n", "").replace(
        "scoring:",
        "repeat:\n  - inputs: [judge_manifest]\n    tuples:\n"
        "      - [judge/program.yaml]\n      - [judge/text.yaml]\nscoring:",
    )
    found = check(
        primitives,
        task,
        JUDGED,
        {"workflow.yaml": JUDGED.encode()},
        JUDGED_PATHS | {"judge/text.yaml"},
        manifests={
            "judge/program.yaml": JUDGE_MANIFEST + b"  note: {}\n",
            "judge/text.yaml": JUDGE_MANIFEST + b"  note: {type: text}\n",
        },
    )

    assert [problem["message"] for problem in found.problems] == [
        "judge/program.yaml reports note as a number and judge/text.yaml as text, but step "
        "game writes note as one output of every run. Give it one type in every file."
    ]


def test_a_manifest_cannot_be_the_entrants(primitives: dict[str, PrimitiveDeclaration]) -> None:
    found = judged(primitives, JUDGED_TASK.replace("judge/program.yaml", "entrant"))

    assert at(found, "inputs.judge_manifest") == [
        "judge_manifest is a program's manifest, which the save reads to check what reads its "
        "reports, so the task must give it, not the entrant."
    ]


def test_an_entrant_judge_with_a_manifest_is_refused_where_it_cannot_be_marked(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = judged(primitives, JUDGED_TASK.replace("  judge: judge/\n", "  judge: entrant\n"))

    assert at(found, "inputs.judge") == [
        "judge is the entrant's, and reaches the program judge of the step game, which is given "
        "a manifest. unicon/interact@v2 cannot mark what such a program reports as the "
        "entrant's own, so the task must give judge itself."
    ]


def test_the_judge_needs_a_longer_time_limit_than_its_peer(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = judged(primitives, JUDGED_TASK.replace("judge_time_limit: 3", "judge_time_limit: 1"))

    assert at(found, "inputs.judge_time_limit") == [
        "In the step game, judge is given a manifest, so it judges, and needs a longer time "
        "limit than entrant: 1 is not more than 1. Each program's wall clock is twice its time "
        "limit and a second, and entrant's should run out first."
    ]


# 9.5: scoring checked at the save


@pytest.mark.parametrize(
    ("old", "new", "path", "words"),
    [
        (
            "st1_tests: {input: [tests/3.in]}",
            "st1_tests: {input: [tests/9.in]}",
            "scoring.groups.st1_tests",
            "holds no run",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "st1_tests: {input: tests/3.in, answer: tests/4.ans}",
            "scoring.groups.st1_tests",
            "holds no run",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "st1_tests: {language: cpp}",
            "scoring.groups.st1_tests.language",
            "filled by the entrant",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "st1_tests: {time_limit: 2}",
            "scoring.groups.st1_tests.time_limit",
            "not repeated",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "st1_tests: {}",
            "scoring.groups.st1_tests",
            "at least one repeated input",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "all: {input: [tests/3.in]}",
            "scoring.groups.all",
            "every run",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "seconds: {input: [tests/3.in]}",
            "scoring.groups.seconds",
            "fact",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "samples: {input: [tests/3.in]}",
            "scoring.groups.samples",
            "also a path",
        ),
        (
            "st1_tests: {input: [tests/3.in]}",
            "total: {input: [tests/3.in]}",
            "scoring.values.total",
            "already a group",
        ),
        ("{value: st1}", "{value: st9}", "scoring.values.total.of[0].of[1]", "no value called st9"),
        ("{runs: st1_tests}", "{runs: st9}", "scoring.values.st1.over", "no group called st9"),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: pow, of: [40, 2]}",
            "scoring.values.total.of[0].block",
            "not a block",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40]}",
            "scoring.values.total.of[0].of",
            "at least 2",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: not, of: [40, 1]}",
            "scoring.values.total.of[0].of",
            "one node",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40, true]}",
            "scoring.values.total.of[0].of[1]",
            "not true or false",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40, '40 * st1']}",
            "scoring.values.total.of[0].of[1]",
            "Give a number, a block",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40, {runs: st1_tests}]}",
            "scoring.values.total.of[0].of[1]",
            "give no number",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40, {run: [st1_tests, score]}]}",
            "scoring.values.total.of[0].of[1]",
            "only inside a fold",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40, {fact: attempt}]}",
            "scoring.values.total.of[0].of[1]",
            "depends on the entrant's other submissions",
        ),
        (
            "{block: mul, of: [40, {value: st1}]}",
            "{block: mul, of: [40, {fact: age}]}",
            "scoring.values.total.of[0].of[1]",
            "no fact called age",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st2_tests, score]}",
            "scoring.values.st1.of",
            "This fold reads the runs of st1_tests",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st1_tests, verdict]}",
            "scoring.values.st1.of",
            "verdict is words",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st1_tests, log]}",
            "scoring.values.st1.of",
            "log is text",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st1_tests, input]}",
            "scoring.values.st1.of",
            "input is a file",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st1_tests, points]}",
            "scoring.values.st1.of",
            "no column called points",
        ),
        (
            "{block: count, over: {runs: sample_tests}}",
            "{block: count, over: {runs: sample_tests}, colour: red}",
            "scoring.values.sample_score.of[1].colour",
            "no input called colour",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st1_tests, score]}, colour: red",
            "scoring.values.st1",
            "min is given numbers or a group's runs, and these inputs fit no form of it",
        ),
        (
            "of: {run: [st1_tests, score]}",
            "of: {run: [st1_tests, score]}, missing: none",
            "scoring.values.st1.missing",
            "skip",
        ),
        (
            "{block: count, over: {runs: sample_tests}}",
            "{block: count}",
            "scoring.values.sample_score.of[1].over",
            "needs its input over",
        ),
        (
            "{block: count, over: {runs: sample_tests}}",
            "{block: count, over: {runs: sample_tests}, weights: {input: {tests/9.in: 2}}}",
            "scoring.values.sample_score.of[1].weights.input.tests/9.in",
            "No run has input",
        ),
        (
            "{block: count, over: {runs: sample_tests}}",
            "{block: count, over: {runs: sample_tests}, weights: {code: {a: 2}}}",
            "scoring.values.sample_score.of[1].weights.code",
            "cannot weigh",
        ),
        (
            "{block: count, over: {runs: sample_tests}}",
            "{block: count, over: {runs: sample_tests}, weights: {input: {samples/1.in: 0}}}",
            "scoring.values.sample_score.of[1].weights.input.samples/1.in",
            "more than 0",
        ),
        (
            "now: [sample_score",
            "now: [sample_scor",
            "scoring.show.now[0]",
            "names no group, value, report, file or folder",
        ),
        (
            "now: [sample_score",
            "now: [st1, sample_score",
            "scoring.show.after_close[0]",
            "already listed under now",
        ),
        ("samples/]", "samples/, samples/]", "scoring.show.now[4]", "already listed above"),
        ("samples/]", "examples/]", "scoring.show.now[3]", "no files in examples/"),
        ("samples/]", "plans/plan.json]", "scoring.show.now[3]", "serves none"),
        ("samples/]", "task.yaml]", "scoring.show.now[3]", "task.yaml names the task's hidden"),
        (
            "[st1, st2, total, all]",
            "[st1, st2, total, all, samples/2.ans]",
            "scoring.show.after_close[4]",
            "samples/2.ans is in samples/, which is shown as soon as a grading ends",
        ),
        (
            "[st1, st2, total, all]",
            "[st1, st2, total, all, samples/1/]",
            "scoring.show.after_close[4]",
            "no files in samples/1/",
        ),
        (
            "[st1, st2, total, all]",
            "[st1, st2, total, all, tests/]\n    on_release: [tests/4.ans]",
            "scoring.show.on_release[0]",
            "tests/4.ans is in tests/, which is shown at the reveal",
        ),
    ],
)
def test_each_scoring_refusal_names_its_path(
    primitives: dict[str, PrimitiveDeclaration], old: str, new: str, path: str, words: str
) -> None:
    assert old in ISLANDS
    found = islands(primitives, ISLANDS.replace(old, new, 1))

    assert any(words in message for message in at(found, path)), found.problems


def test_a_number_is_one_key_however_it_is_written(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    twice = cartpole(primitives, CARTPOLE_TASK.replace("tuples: [1, 2, 3]", "tuples: [1, 1.0, 3]"))
    pointed = ok(
        cartpole(primitives, CARTPOLE_TASK.replace("tuples: [1, 2, 3]", "tuples: [1.0, 2, 3]"))
    )
    plain = ok(cartpole(primitives, previous=pointed.runs_file()))

    assert any("same as tuple 1" in message for message in at(twice, "repeat[1].tuples[1]"))
    assert [run.id for run in plain.runs] == [run.id for run in pointed.runs]


def test_a_task_file_called_all_is_refused_beside_the_group_of_every_run(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    for path in ("all", "all/1.in"):
        found = islands(primitives, paths=ISLANDS_PATHS | {path})

        assert at(found, "scoring") == [
            "all is a path in the task, and the name of the group of every run, so show "
            "could not tell them apart. Rename it in the task's files."
        ]


def test_a_value_cannot_read_itself_through_others(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace(
        "st2: {block: min, over: {runs: st2_tests}, of: {run: [st2_tests, score]}}",
        "st2: {block: add, of: [{value: total}, 1]}",
    )

    found = islands(primitives, text)

    assert at(found, "scoring.values.st2") == [
        "st2 reads itself, through st2 -> total -> st2. A value cannot read one that reads it."
    ]
    assert at(found, "scoring.values.total")


def test_a_value_shown_now_reading_compiled_makes_the_compile_early(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace(
        "    now: [sample_score, sample_time, sample_tests, samples/]\n",
        "    now: [built]\n",
    ).replace("  values:\n", "  values:\n    built: {fact: compiled}\n")

    task = ok(islands(primitives, text))

    assert task.compile_early()
    assert task.early() == frozenset()
    assert task.runs_file().compile_early


def test_on_release_is_late_until_a_release(primitives: dict[str, PrimitiveDeclaration]) -> None:
    text = ISLANDS.replace(
        "    after_close: [st1, st2, total, all]\n",
        "    after_close: [st2, all]\n    on_release: [st1]\n",
    )

    task = ok(islands(primitives, text))

    assert task.early() == {"r1", "r2"}
    assert task.early(released=True) == {"r1", "r2", "r3"}


# show: files and once shown


def test_a_file_is_shown_at_the_earliest_time_listing_it_or_its_folder() -> None:
    show = _scoring(ISLANDS)
    later = _scoring(
        ISLANDS.replace("[st1, st2, total, all]", "[st1, st2, total, all, tests/3.in]")
    )

    assert file_time(show, "samples/1.in") == "now"
    assert file_time(show, "tests/3.in") is None
    assert file_time(later, "tests/3.in") == "after_close"
    assert file_time(later, "tests/3.ans") is None
    assert file_time(show, "samples") is None
    # A group's or a value's name is no file, whatever the task holds.
    assert file_time(show, "all") is None
    assert file_time(show, "sample_score") is None


def test_a_file_shown_sooner_than_its_folder_is_shown_sooner(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace("samples/]", "samples/, tests/3.in]").replace(
        "[st1, st2, total, all]", "[st1, st2, total, all, tests/]"
    )

    ok(islands(primitives, text))

    assert file_time(_scoring(text), "tests/3.in") == "now"
    assert file_time(_scoring(text), "tests/4.in") == "after_close"


def test_show_lists_a_report_by_its_name_but_not_one_also_a_file_of_the_task(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace("samples/]", "samples/, log]")

    ok(islands(primitives, text))
    assert at(islands(primitives, text, paths=ISLANDS_PATHS | {"log"}), "scoring.show.now[4]") == [
        "log names both a report of the workflow and a file of the task, so show cannot tell "
        "which it lists. Rename the file."
    ]
    # A report listed is a name, so taking it out of show is hiding it.
    before = shown_under(_scoring(text), ISLANDS_PATHS)
    assert before.names["log"] == "now"
    assert hidden_again(before, _scoring(ISLANDS), ISLANDS_PATHS) == [
        {
            "path": "scoring.show",
            "message": "Entrants have been shown log as soon as a grading ends, and what they "
            "have been shown cannot be hidden again. List it as soon as before.",
        }
    ]


def _scoring(text: str) -> Scoring:
    scoring = parse_task(text).scoring
    assert scoring is not None
    return scoring


def test_what_was_shown_cannot_be_hidden_or_shown_later() -> None:
    scoring = _scoring(
        ISLANDS.replace("now: [sample_score, sample_time,", "now: [sample_score,").replace(
            "after_close: [st1,", "after_close: [sample_time, st1,"
        )
    )
    before = Shown(
        {"sample_time": "now", "st9": "after_close"},
        {"samples/1.in": ("now", "samples/"), "tests/3.in": ("now", "tests/3.in")},
    )

    problems = hidden_again(before, scoring, ISLANDS_PATHS)

    assert problems == [
        {
            "path": "scoring.show.after_close",
            "message": "Entrants have been shown sample_time as soon as a grading ends, and what "
            "they have been shown cannot be hidden again. List it as soon as before.",
        },
        {
            "path": "scoring.show",
            "message": "Entrants have been shown st9 at the reveal, and what they have been "
            "shown cannot be hidden again. List it as soon as before.",
        },
        {
            "path": "scoring.show",
            "message": "Entrants have been shown tests/3.in as soon as a grading ends, and what "
            "they have been shown cannot be hidden again. List it as soon as before.",
        },
    ]
    assert hidden_again(before, scoring, ISLANDS_PATHS - {"tests/3.in"})[2:] == []


def test_a_shown_file_is_compared_by_path_whichever_entry_lists_it() -> None:
    one = _scoring(ISLANDS.replace("samples/]", "samples/1.in]"))
    before = shown_under(one, ISLANDS_PATHS)

    assert before.files == {"samples/1.in": ("now", "samples/1.in")}
    assert hidden_again(before, _scoring(ISLANDS), ISLANDS_PATHS) == []
    assert hidden_again(
        shown_under(_scoring(ISLANDS), ISLANDS_PATHS),
        _scoring(ISLANDS.replace("samples/]", "samples/1.in, samples/1.ans, samples/2.in]")),
        ISLANDS_PATHS,
    ) == [
        {
            "path": "scoring.show",
            "message": "Entrants have been shown samples/ as soon as a grading ends, and what "
            "they have been shown cannot be hidden again. List it as soon as before.",
        }
    ]


def test_what_was_shown_is_the_soonest_any_publication_showed_it() -> None:
    first = Shown({"a": "now", "b": "after_close", "c": "on_release"}, {"x": ("now", "x")})
    second = Shown({"a": "after_close", "c": "after_close"}, {"x": ("on_release", "./")})

    assert most_shown([first, second]) == Shown(
        {"a": "now", "b": "after_close", "c": "after_close"}, {"x": ("now", "x")}
    )


# What a release shows, and what a later save keeps


RELEASING = ISLANDS.replace(
    "after_close: [st1, st2, total, all]",
    "after_close: [st1, all]\n    on_release: [st2, st2_tests, tests/4.in]",
)


def test_a_release_makes_what_on_release_reads_early(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(islands(primitives, RELEASING))

    assert task.shown() == ["sample_score", "sample_time", "sample_tests", "samples/"]
    assert task.early() == {"r1", "r2"}
    assert task.early(released=True) == {"r1", "r2", "r3", "r4"}
    assert [run.early for run in task.runs_file().runs] == [True, True, False, False]


def test_the_runs_file_keeps_the_runs_each_name_shown_during_the_contest_reads(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(islands(primitives, RELEASING))

    assert task.runs_read() == {
        "sample_score": {"r1", "r2"},
        "sample_time": {"r1", "r2"},
        "sample_tests": {"r1", "r2"},
        "st2": {"r3", "r4"},
        "st2_tests": {"r3", "r4"},
    }
    assert task.runs_of("total") == frozenset({"r3", "r4"})
    assert task.runs_of("samples/") is None
    written = task.runs_file()
    assert RunsFile.from_bytes(written.to_bytes()) == written
    document = json.loads(written.to_bytes())
    del document["reads"]
    assert RunsFile.from_bytes(json.dumps(document).encode()).reads == {}


def test_a_value_listed_sooner_than_what_it_reads_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = islands(
        primitives,
        ISLANDS.replace(
            "after_close: [st1, st2, total, all]",
            "after_close: [st1, total, all]\n    on_release: [st2, st2_tests, tests/4.in]",
        ),
    )

    assert at(found, "scoring.show.after_close[1]") == [
        f"total reads {name}, which is shown once results are released, so showing total at "
        f"the reveal would give {name} away sooner. List total no sooner than {name}."
        for name in ("st2", "st2_tests")
    ]
    # A name show does not list is free to read, and so is one listed as
    # soon or sooner.
    ok(islands(primitives, RELEASING))
    ok(
        islands(
            primitives,
            ISLANDS.replace(
                "after_close: [st1, st2, total, all]",
                "after_close: [st1, all]\n    on_release: [st2, total, st2_tests]",
            ),
        )
    )


def test_an_entered_mark_under_on_release_is_not_given_away_by_a_value_shown_sooner(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    marked = ISLANDS.replace(
        "    total:\n",
        "    review: {block: entered, at_least: 0, at_most: 10}\n"
        "    reviewed: {block: add, of: [{value: sample_score}, {value: review}]}\n"
        "    total:\n",
    )
    found = islands(
        primitives,
        marked.replace(
            "after_close: [st1, st2, total, all]",
            "after_close: [st1, st2, total, all]\n    on_release: [review]",
        ).replace("now: [sample_score,", "now: [reviewed, sample_score,"),
    )

    assert at(found, "scoring.show.now[0]") == [
        "reviewed reads review, which is shown once results are released, so showing "
        "reviewed as soon as a grading ends would give review away sooner. List reviewed no "
        "sooner than review."
    ]
    task = ok(
        islands(
            primitives,
            marked.replace(
                "after_close: [st1, st2, total, all]",
                "after_close: [st1, st2, total, all]\n    on_release: [review, reviewed]",
            ),
        )
    )
    assert task.values.through("reviewed").blocks == {"entered"}


def test_a_group_shown_sooner_that_holds_runs_a_release_shows_is_noted(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(islands(primitives, RELEASING))

    assert task.release_notes() == [
        f"all, shown at the reveal, holds runs {name} reads, so entrants see those runs "
        f"before a release shows {name}."
        for name in ("st2", "st2_tests")
    ]
    assert ok(islands(primitives)).release_notes() == []


def test_what_on_release_listed_counts_as_not_shown_without_a_release() -> None:
    shown = Shown(
        {"a": "now", "b": "on_release"}, {"x": ("on_release", "x"), "y": ("after_close", "./")}
    )

    assert unreleased(shown) == Shown({"a": "now"}, {"y": ("after_close", "./")})


def test_a_name_shown_during_the_contest_may_not_read_fewer_of_the_runs_left(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    fewer = ok(
        islands(
            primitives,
            RELEASING.replace(
                "sample_tests: {input: [samples/1.in, samples/2.in]}",
                ("sample_tests: {input: [samples/1.in]}"),
            ),
        )
    )
    shown = Shown({"sample_tests": "now", "st2_tests": "on_release", "st1": "after_close"})
    reads = {
        "sample_tests": frozenset({"r1", "r2"}),
        "st2_tests": frozenset({"r3", "r4", "r9"}),
        "st1": frozenset({"r1"}),
    }

    assert fewer_runs([(shown, reads)], fewer) == [
        {
            "path": "scoring.groups.sample_tests",
            "message": "Entrants have been shown sample_tests during the contest over 2 runs "
            "the task still has, and it would now read 1 fewer. What they have been shown "
            "cannot be hidden again, so keep those runs in it.",
        }
    ]
    # A run the task no longer has is not counted, nor what was shown at
    # the reveal, nor a release taken back.
    assert fewer_runs([(unreleased(shown), reads)], ok(islands(primitives, RELEASING))) == []


# exec: a value a program of the task works out

MEAN = ISLANDS.replace(
    "  values:\n",
    "  values:\n"
    "    mean: {block: exec, program: programs/mean/, language: python, over: {runs: all}}\n",
).replace(
    "    after_close: [st1, st2, total, all]\n", "    after_close: [st1, st2, total, all, mean]\n"
)
MEAN_PATHS = ISLANDS_PATHS | {"programs/mean/mean.py", "programs/mean/program.yaml"}
MEAN_MANIFEST = b"reports:\n  mean: {better: higher, at_least: 0, at_most: 1}\n"


def mean(
    primitives: dict[str, PrimitiveDeclaration],
    task: str = MEAN,
    manifest: bytes | None = MEAN_MANIFEST,
    paths: frozenset[str] = MEAN_PATHS,
) -> RunTaskCheck:
    manifests = {} if manifest is None else {"programs/mean/program.yaml": manifest}
    return islands(primitives, task, paths=paths, manifests=manifests)


def test_an_exec_value_names_its_program_and_runs_late_unless_shown_during_the_contest(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    workflow, files = deployed("unicon/classic@v3")
    checked = check_run_workflow(
        parse_run_workflow(workflow), primitives, WorkflowFiles(frozenset(files), files)
    )
    assert manifest_files(parse_task(MEAN, runs=True), checked) == {"programs/mean/program.yaml"}

    task = ok(mean(primitives))
    assert task.programs["mean"] == ProgramValue(
        "mean", "programs/mean/", "python", "all", 2, 256, "mean", Better.HIGHER
    )
    assert task.programs["mean"].manifest == "programs/mean/program.yaml"
    assert task.early_programs() == frozenset()

    shown = MEAN.replace(
        "    now: [sample_score, sample_time, sample_tests, samples/]\n",
        "    now: [sample_score, sample_time, sample_tests, samples/, mean, all]\n",
    ).replace("total, all, mean]", "total]")
    assert ok(mean(primitives, shown)).early_programs() == {"mean"}

    released = MEAN.replace("total, all, mean]", "total, all]\n    on_release: [mean]")
    assert ok(mean(primitives, released)).early_programs() == frozenset()
    assert ok(mean(primitives, released)).early_programs(released=True) == {"mean"}

    limited = MEAN.replace("over: {runs: all}}", "over: {runs: all}, time_limit: 0.5}")
    assert ok(mean(primitives, limited)).programs["mean"].time_limit == Decimal("0.5")


@pytest.mark.parametrize(
    ("manifest", "words"),
    [
        (
            None,
            "programs/mean/ holds no program.yaml, which declares the one number the program "
            "reports.",
        ),
        (
            b"reports:\n  a: {better: higher}\n  b: {better: lower}\n",
            "programs/mean/program.yaml declares 2 reports, and a value's program reports one "
            "number, the value's.",
        ),
        (
            b"reports:\n  a: {type: text}\n",
            "programs/mean/program.yaml declares a as text, with no better. A value's program "
            "reports one number, never left out, saying which way is better.",
        ),
        (
            b"reports:\n  a: {better: higher, optional: true}\n",
            "programs/mean/program.yaml declares a as number, optional. A value's program reports "
            "one number, never left out, saying which way is better.",
        ),
        (
            b"reports:\n  a: {at_least: 0}\n",
            "programs/mean/program.yaml declares a as number, with no better. A value's program "
            "reports one number, never left out, saying which way is better.",
        ),
    ],
)
def test_an_exec_values_program_yaml_declares_one_number_with_better(
    primitives: dict[str, PrimitiveDeclaration], manifest: bytes | None, words: str
) -> None:
    assert at(mean(primitives, manifest=manifest), "scoring.values.mean.program") == [words]


def test_an_exec_values_folder_and_group_must_be_the_tasks_and_hold_runs(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    assert at(
        mean(primitives, MEAN.replace("program: programs/mean/", "program: programs/other/")),
        ("scoring.values.mean.program"),
    ) == ["programs/other/ is not a folder of the task."]
    assert at(mean(primitives, manifest=b"reports: [1]\n"), "scoring.values.mean.program")
    empty = MEAN.replace("    st1_tests:", "    none_tests: {input: [tests/9.in]}\n    st1_tests:")
    empty = empty.replace("over: {runs: all}}", "over: {runs: none_tests}}")
    assert at(mean(primitives, empty), "scoring.values.mean.over") == [
        "none_tests holds no runs, so the program would have none to read."
    ]


def test_a_program_reading_a_self_reported_column_is_self_reported(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    assert ok(mean(primitives)).self_reported_values() == frozenset()
    text = (
        TOUR_TASK.replace("inputs:\n  best: 1200\n", "")
        .replace("inputs: [input, route]", "inputs: [input, route, best]")
        .replace("entrant]", "entrant, entrant]")
        .replace(
            "  values:\n",
            "  values:\n    mean: {block: exec, program: programs/mean/, language: python, "
            "over: {runs: all}}\n",
        )
    )
    task = ok(
        tour(
            primitives,
            text,
            paths=TOUR_PATHS | {"programs/mean/mean.py", "programs/mean/program.yaml"},
            manifests={"programs/mean/program.yaml": MEAN_MANIFEST},
        )
    )
    assert task.self_reported_values() == {"mean"}


MEAN_BLOCK = "{block: exec, program: programs/mean/, language: python, over: {runs: all}}"


@pytest.mark.parametrize(
    ("block", "path", "words"),
    [
        (
            "{block: exec, language: python, over: {runs: all}}",
            "scoring.values.mean.program",
            "exec needs its input program.",
        ),
        (
            "{block: exec, program: programs/mean/, over: {runs: all}}",
            "scoring.values.mean.language",
            "exec needs its input language.",
        ),
        (
            "{block: exec, program: 5, language: python, over: {runs: all}}",
            "scoring.values.mean.program",
            None,
        ),
        (
            "{block: exec, program: programs/mean/, language: python, over: all}",
            "scoring.values.mean.over",
            None,
        ),
        (
            "{block: exec, program: programs/mean/, language: python, over: {runs: nope}}",
            "scoring.values.mean.over",
            None,
        ),
    ],
)
def test_a_malformed_exec_block_is_one_problem_never_a_crash(
    primitives: dict[str, PrimitiveDeclaration], block: str, path: str, words: str | None
) -> None:
    found = mean(primitives, MEAN.replace(MEAN_BLOCK, block))

    assert found.task is None
    [problem] = found.problems
    assert problem["path"] == path
    if words is not None:
        assert problem["message"] == words


# Checks


CHECKS = """\
checks:
  - submit: {code: checks/good}
    expect: {verdict: accepted, total: 100}
  - submit: {code: checks/empty/, language: python}
    expect: {verdict: wrong_answer}
  - submit: {code: checks/slow}
    expect: {st1: 1, st2: 0.5}
"""
CHECK_PATHS = ISLANDS_PATHS | {"checks/good/main.cpp", "checks/empty/main.py", "checks/slow/a.cpp"}


def test_a_check_gives_what_the_entrant_does_and_expects_a_verdict_or_values(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = ok(islands(primitives, ISLANDS + CHECKS, paths=CHECK_PATHS))

    good, empty, slow = task.checks
    assert good.index == 1 and good.part("code", None) == "checks/good/"
    # A language left out takes its form's default, as the entrant's would.
    assert good.part("language", None) == "cpp"
    assert good.verdict == "accepted" and dict(good.values) == {"total": Fraction(100)}
    assert (empty.part("language", None), empty.verdict, dict(empty.values)) == (
        "python",
        "wrong_answer",
        {},
    )
    assert slow.verdict is None and dict(slow.values) == {"st1": 1, "st2": Fraction(1, 2)}


@pytest.mark.parametrize(
    ("check", "path", "words"),
    [
        (
            "{submit: {code: checks/good, time_limit: 3}, expect: {verdict: accepted}}",
            "checks[0].submit.time_limit",
            "The entrant gives no time_limit, so a check cannot. A check gives what the "
            "entrant does: code and language.",
        ),
        (
            "{submit: {language: cpp}, expect: {verdict: accepted}}",
            "checks[0].submit",
            "This check gives no code, which the entrant must give. Give it.",
        ),
        (
            "{submit: {code: checks/none}, expect: {verdict: accepted}}",
            "checks[0].submit.code",
            "There is no folder checks/none/ in the task, or it holds no file.",
        ),
        (
            "{submit: {code: checks/good, language: java}, expect: {verdict: accepted}}",
            "checks[0].submit.language",
            "This input takes one of cpp and python.",
        ),
        (
            "{submit: {code: checks/good}, expect: {verdict: fine}}",
            "checks[0].expect.verdict",
            None,
        ),
        (
            "{submit: {code: checks/good}, expect: {st3: 1}}",
            "checks[0].expect.st3",
            "There is no value st3 in the task's scoring, so a check cannot expect it. Its "
            "values are sample_score, sample_time, st1, st2 and total, and verdict names the "
            "verdict every run must get.",
        ),
        (
            "{submit: {code: checks/good}, expect: {total: lots}}",
            "checks[0].expect.total",
            "Give the number total must be.",
        ),
    ],
)
def test_a_check_that_does_not_fit_the_task_is_refused_at_its_path(
    primitives: dict[str, PrimitiveDeclaration], check: str, path: str, words: str | None
) -> None:
    found = islands(primitives, f"{ISLANDS}checks:\n  - {check}\n", paths=CHECK_PATHS)

    messages = at(found, path)
    assert len(messages) == 1
    if words is not None:
        assert messages == [words]


def test_a_check_needs_something_to_expect() -> None:
    with pytest.raises(InvalidDefinition) as raised:
        parse_task(
            f"{ISLANDS}checks:\n  - {{submit: {{code: checks/good}}, expect: {{}}}}\n", runs=True
        )

    assert any(problem["path"] == "checks[0].expect" for problem in raised.value.extra["errors"])


def test_a_file_only_a_check_names_is_never_shown() -> None:
    definition = parse_task(
        (ISLANDS + CHECKS).replace("    now: [sample_score,", "    now: [checks/, sample_score,"),
        runs=True,
    )

    assert check_only(definition, "checks/good/main.cpp")
    now = datetime(2026, 10, 23, tzinfo=UTC)
    assert not shown_now(definition, "checks/good/main.cpp", reveal=now, now=now)
    assert shown_now(definition, "samples/1.in", reveal=None, now=now)
    assert check_only(definition, "checks/slow/a.cpp")
    assert not check_only(definition, "samples/1.in")
    # A file the task gives a setting or a tuple is the task's as well.
    shared = parse_task((ISLANDS + CHECKS).replace("checks/empty/", "samples/"), runs=True)
    assert not check_only(shared, "samples/1.in")


def test_a_self_reported_value_newly_shown_asks_again(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = (
        TOUR_TASK.replace("inputs:\n  best: 1200\n", "")
        .replace("inputs: [input, route]", "inputs: [input, route, best]")
        .replace("entrant]", "entrant, entrant]")
        .replace(
            "  values:\n",
            "  values:\n    claimed: {block: sum, over: {runs: all}, of: {run: [all, best]}}\n",
        )
    )
    hidden = ok(tour(primitives, text))
    shown = ok(tour(primitives, text.replace("now: [total,", "now: [claimed, total,")))

    assert self_reported_question(hidden, hidden.runs_file()) is None
    assert self_reported_question(shown, shown.runs_file()) is None
    asked = self_reported_question(shown, hidden.runs_file())
    assert asked is not None and asked.startswith("claimed reads best,")
    assert "new or newly shown" in asked
    # A runs file written before what is shown was kept reads as showing
    # none, so its next save asks once.
    document = json.loads(shown.runs_file().to_bytes())
    del document["self_reported_shown"]
    older = RunsFile.from_bytes(json.dumps(document).encode())
    assert self_reported_question(shown, older) is not None
