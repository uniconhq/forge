"""The compiler: a task's tests read from its folders, its groups matched to
them, and its plan compiled from `task.yaml`, the workflow and the primitives
its steps use. The plan keeps the runner's contract; a per-test step is one
batch or one entry per test; every value is written in or refused at its
path in `task.yaml`; secrets reach only the ports marked for them; sealed
steps are found and held back; credit names a number fit to be one; a plan
that does not fit the run ceiling or a machine is refused; and a
publication says what changed how the task grades.
"""

import json
from collections.abc import Collection, Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from forge.domain import exact_json
from forge.domain.contracts import violation
from forge.domain.definitions import parse_task, starter_task
from forge.domain.grading import PLATFORM_MACHINE, Machine
from forge.domain.plans import (
    PLAN_PATH,
    Compiled,
    Plan,
    Snapshot,
    check_workflow,
    compile_plan,
    grading_changes,
    group_problems,
    is_reserved,
    natural,
    read_tests,
    spelled,
)
from forge.domain.plans import (
    TestCase as Case,
)
from forge.domain.plans import (
    test_yaml_paths as yaml_paths,
)
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.publications import read_note, write_note
from forge.domain.scoring import fold, written
from forge.domain.showing import NOTHING_SEALED, Sealed
from forge.domain.workflow_definition import WorkflowDefinition, parse_workflow
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.testing import CLASSIC, PLACEHOLDER_DIGEST, PRIMITIVES

HARNESS = "ghcr.io/uniconhq/harness@sha256:" + "1" * 64
CLASSIC_WORKFLOW = parse_workflow(CLASSIC)


def declared(name: str, rest: str) -> PrimitiveDeclaration:
    return parse_primitive(f"image: ghcr.io/acme/{name}@{PLACEHOLDER_DIGEST}\n" + rest)


LIMITS = "limits: {time_ms: 1000, cpu_ms: 1000, memory_mb: 64, pids: 8, output_mb: 1, gpus: 0}\n"

ACME = {
    "acme/score@v1": declared(
        "score",
        "batch: true\n"
        + LIMITS
        + """\
inputs:
  actual: {type: file, runs: false}
  expected: {type: file, runs: false}
outputs:
  fraction: {type: number, optional: true}
  steps: number
  outcome: outcome
""",
    ),
    "acme/notebook@v1": declared(
        "notebook",
        LIMITS
        + """\
inputs:
  notebook: {type: file, runs: true}
  data: {type: folder, runs: false}
  key: {type: text, secret: true, optional: true}
  note: {type: text, optional: true}
outputs:
  predictions: file
  accuracy: number
  outcome: outcome
""",
    ),
    "acme/each@v1": declared(
        "each",
        LIMITS
        + """\
inputs:
  actual: {type: file, runs: false}
  expected: {type: file, runs: false}
outputs:
  outcome: outcome
""",
    ),
    "acme/gpu@v1": declared(
        "gpu",
        LIMITS
        + """\
limits_from:
  gpus: {input: gpus}
inputs:
  gpus: number
outputs:
  outcome: outcome
""",
    ),
}
PRIMITIVES_READ: dict[str, PrimitiveDeclaration] = {
    **{f"unicon/{name}@v2": parse_primitive(text) for name, text in PRIMITIVES.items()},
    **ACME,
}

CLASSIC_HEAD = """\
inputs:
  submission: {type: file, contestant: true}
  language: {type: enum, options: [c, cpp, java, python], contestant: true}
"""
COMPILE = """\
  - id: compile
    use: unicon/compile@v2
    with:
      source: ${{ inputs.submission }}
      language: ${{ inputs.language }}
"""
CHECK = """\
  - id: check
    use: unicon/diff-check@v2
    per_test: true
    with:
      actual: ${{ steps.run.output }}
      expected: ${{ test.answer }}
"""

TUNABLE = parse_workflow(
    CLASSIC_HEAD
    + """\
  n: {type: number, contestant: true}
  verbose: boolean
  scale: number
  memory_limit: number
test:
  input: file
  answer: file
  seconds: number
  name: text
steps:
"""
    + COMPILE
    + """\
  - id: run
    use: unicon/sandbox-run@v2
    per_test: true
    with:
      binary: ${{ steps.compile.binary }}
      input: ${{ test.input }}
      time_limit: ${{ test.seconds }}
      memory_limit: ${{ inputs.memory_limit }}
      args: >-
        --n ${{ inputs.n }} --scale ${{ inputs.scale }} --v ${{ inputs.verbose }}
        --test ${{ test.name }} {raw}
"""
    + CHECK
)
"""Classic with a contestant number, a task boolean and number, and a number
and a text field per test, written into the run's arguments."""

CHECKED_TEXT = (
    CLASSIC_HEAD
    + """\
  time_limit: number
  memory_limit: number
  better: {type: enum, options: [higher, lower]}
test:
  input: file
  answer: file
steps:
"""
    + COMPILE
    + """\
  - id: run
    use: unicon/sandbox-run@v2
    per_test: true
    with:
      binary: ${{ steps.compile.binary }}
      input: ${{ test.input }}
      time_limit: ${{ inputs.time_limit }}
      memory_limit: ${{ inputs.memory_limit }}
  - id: score
    use: acme/score@v1
    per_test: true
    with:
      actual: ${{ steps.run.output }}
      expected: ${{ test.answer }}
report:
  fraction:
    {from: "${{ steps.score.fraction }}", fold: mean, better: higher, at_least: 0, at_most: 1}
  miss: {from: "${{ steps.score.fraction }}", fold: mean, better: lower, at_least: 0, at_most: 1}
  steps_taken: {from: "${{ steps.score.steps }}", fold: sum, better: lower, at_least: 0}
  raw: {from: "${{ steps.score.steps }}", fold: sum, at_least: 0}
  reward: {from: "${{ steps.score.steps }}", fold: sum, better: higher}
  chosen: {from: "${{ steps.score.steps }}", fold: sum, better: "${{ inputs.better }}", at_least: 0}
  log: ${{ steps.compile.compile_log }}
"""
)
"""Classic scored by a checker reporting a bounded fraction, and numbers with
every combination of direction and bound."""
CHECKED = parse_workflow(CHECKED_TEXT)

NOTEBOOK = parse_workflow(
    """\
inputs:
  notebook: {type: file, contestant: true}
  data: folder
  key: {type: text, optional: true}
test:
  answer: file
steps:
  - id: predict
    use: acme/notebook@v1
    with:
      notebook: ${{ inputs.notebook }}
      data: ${{ inputs.data }}
      key: ${{ inputs.key }}
  - id: score
    use: acme/score@v1
    per_test: true
    with:
      actual: ${{ steps.predict.predictions }}
      expected: ${{ test.answer }}
report:
  accuracy: ${{ steps.predict.accuracy }}
  fraction:
    {from: "${{ steps.score.fraction }}", fold: mean, better: higher, at_least: 0, at_most: 1}
"""
)
"""The contestant's notebook run once over a data folder the task names, and
each test scored from its predictions."""

OUTPUT_ONLY = parse_workflow(
    """\
inputs:
  answers: {type: file, contestant: true, per_test: true}
test:
  input: {type: file, public: true}
  answer: file
steps:
  - id: check
    use: acme/each@v1
    per_test: true
    with:
      actual: ${{ inputs.answers }}
      expected: ${{ test.answer }}
"""
)
"""An output-only task: one file per test, checked by a primitive that takes
no batch, each test's input served to contestants."""

CLASSIC_TASK = """\
name: Sum
workflow: unicon/classic@v2
inputs:
  submission: {label: Your solution}
  language: {options: [python]}
  time_limit: 2
  memory_limit: 256
test_groups:
  main: {each: 100}
"""


def classic_tests(*ids: str) -> set[str]:
    """The files of the tests `ids`, each an input and an answer."""
    return {f"tests/{test}/{entry}" for test in ids for entry in ("input", "answer")}


def task_with(
    text: str = CLASSIC_TASK,
    *,
    workflow: str = "unicon/classic@v2",
    inputs: str | None = None,
    groups: str | None = None,
    credit: str | None = None,
) -> str:
    """`text` with another workflow line, inputs block, groups or credit."""
    lines = text.replace("unicon/classic@v2", workflow)
    if inputs is not None:
        head, _, rest = lines.partition("inputs:\n")
        _, _, tail = rest.partition("test_groups:\n")
        lines = f"{head}inputs:\n{inputs}test_groups:\n{tail}"
    if groups is not None:
        head, _, _ = lines.partition("test_groups:\n")
        lines = f"{head}test_groups:\n{groups}"
    if credit is not None:
        lines += f"credit: {credit}\n"
    return lines


def cases_of(
    workflow: WorkflowDefinition, paths: Collection[str], yamls: Mapping[str, bytes]
) -> list[Case]:
    tests, problems = read_tests(paths, workflow.test, yamls)
    assert problems == []
    return tests


def compiled(
    text: str,
    paths: Collection[str],
    workflow: WorkflowDefinition = CLASSIC_WORKFLOW,
    *,
    yamls: Mapping[str, bytes] | None = None,
    secrets: Collection[str] = frozenset(),
    machine: Machine = PLATFORM_MACHINE,
) -> Compiled:
    """The task compiled after every check before it has passed."""
    task = parse_task(text)
    assert check_workflow(workflow, PRIMITIVES_READ) == []
    tests = cases_of(workflow, paths, yamls or {})
    assert group_problems(task, tests, paths) == []
    return compile_plan(
        task,
        workflow,
        PRIMITIVES_READ,
        paths,
        tests,
        secrets=secrets,
        machine=machine,
        harness_image=HARNESS,
    )


def refused(
    text: str,
    paths: Collection[str],
    workflow: WorkflowDefinition = CLASSIC_WORKFLOW,
    **options: Any,
) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        compiled(text, paths, workflow, **options)
    return raised.value.errors


def document(plan: Plan) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(plan.to_bytes())
    return found


def step(plan: Plan, step_id: str) -> dict[str, Any]:
    (found,) = [entry for entry in document(plan)["steps"] if entry["id"] == step_id]
    assert isinstance(found, dict)
    return found


# The tests, from their folders


def test_two_groups_may_each_have_a_test_1() -> None:
    tests = cases_of(CLASSIC_WORKFLOW, classic_tests("samples/1", "main/1"), {})

    assert [test.id for test in tests] == ["main/1", "samples/1"]
    assert [(test.group, test.name) for test in tests] == [("main", "1"), ("samples", "1")]
    assert tests[1].entries == {
        "answer": "tests/samples/1/answer",
        "input": "tests/samples/1/input",
    }


def test_tests_are_in_natural_order_within_groups_in_name_order() -> None:
    paths = classic_tests("main/10", "main/2", "main/1", "b/x10", "b/x9", "a/1")

    assert [test.id for test in cases_of(CLASSIC_WORKFLOW, paths, {})] == [
        "a/1",
        "b/x9",
        "b/x10",
        "main/1",
        "main/2",
        "main/10",
    ]
    assert sorted(["10", "2", "1"], key=natural) == ["1", "2", "10"]


def test_an_entry_may_carry_an_ending_after_the_fields_name() -> None:
    tests = cases_of(CLASSIC_WORKFLOW, {"tests/main/1/input.txt", "tests/main/1/answer.out"}, {})

    assert tests[0].entries == {
        "input": "tests/main/1/input.txt",
        "answer": "tests/main/1/answer.out",
    }


def test_dot_files_are_left_out() -> None:
    paths = classic_tests("main/1") | {
        "tests/.gitkeep",
        "tests/main/.hidden/input",
        "tests/main/1/.x",
    }

    assert [test.id for test in cases_of(CLASSIC_WORKFLOW, paths, {})] == ["main/1"]


def read_problems(
    paths: Collection[str],
    workflow: WorkflowDefinition = CLASSIC_WORKFLOW,
    yamls: Mapping[str, bytes] | None = None,
) -> list[Problem]:
    _, problems = read_tests(paths, workflow.test, yamls or {})
    return problems


def test_an_entry_for_no_field_is_refused_naming_the_test() -> None:
    assert read_problems(classic_tests("main/1") | {"tests/main/1/extra"}) == [
        {
            "path": "tests/main/1/extra",
            "message": "The test main/1 has an entry for no field: extra.",
        }
    ]


def test_a_missing_field_is_refused_naming_the_test_and_the_field() -> None:
    assert read_problems({"tests/main/1/input"}) == [
        {"path": "tests/main/1/", "message": "The test main/1 has no answer."}
    ]


def test_a_field_given_twice_is_refused() -> None:
    assert read_problems(classic_tests("main/1") | {"tests/main/1/input.txt"}) == [
        {"path": "tests/main/1/", "message": "The test main/1 gives input twice: input, input.txt."}
    ]


def test_a_file_outside_a_test_folder_is_refused() -> None:
    problems = read_problems(classic_tests("main/1") | {"tests/README.md", "tests/main/notes.txt"})

    assert [problem["path"] for problem in problems] == ["tests/README.md", "tests/main/notes.txt"]
    assert all("holds only folders" in problem["message"] for problem in problems)


def test_a_test_whose_name_is_not_a_name_is_refused() -> None:
    assert read_problems(classic_tests("main/a.b")) == [
        {"path": "tests/main/a.b/", "message": "'a.b' is not a name: letters, digits, _ and -."}
    ]


FOLDERS = parse_workflow(
    """\
test:
  env: folder
  answer: file
steps:
  - id: check
    use: acme/each@v1
    per_test: true
    with: {actual: "${{ test.answer }}", expected: "${{ test.answer }}"}
"""
)


def test_a_folder_field_is_a_folder_named_the_field() -> None:
    paths = {"tests/main/1/env/a.txt", "tests/main/1/env/b/c.txt", "tests/main/1/answer"}

    (test,) = cases_of(FOLDERS, paths, {})

    assert test.entries == {"env": "tests/main/1/env/", "answer": "tests/main/1/answer"}


def test_a_file_where_a_folder_field_is_wanted_is_refused_and_the_other_way() -> None:
    assert read_problems({"tests/main/1/env", "tests/main/1/answer"}, FOLDERS) == [
        {"path": "tests/main/1/env", "message": "env is a folder field."}
    ]
    assert read_problems({"tests/main/1/env/a", "tests/main/1/answer/b"}, FOLDERS) == [
        {"path": "tests/main/1/answer/", "message": "answer is a file field."}
    ]


SCALARS = parse_workflow(
    """\
test:
  answer: file
  seconds: number
  name: text
  fast: boolean
  kind: {type: enum, options: [small, large]}
steps:
  - id: check
    use: acme/each@v1
    per_test: true
    with: {actual: "${{ test.answer }}", expected: "${{ test.answer }}"}
"""
)
ONE_SCALAR_TEST = {"tests/main/1/answer", "tests/main/1/test.yaml"}
GOOD_YAML = b"seconds: 2.5\nname: one\nfast: true\nkind: small\n"


def test_a_test_yaml_gives_every_scalar_field_its_value() -> None:
    (test,) = cases_of(SCALARS, ONE_SCALAR_TEST, {"tests/main/1/test.yaml": GOOD_YAML})

    assert test.scalars == {"seconds": 2.5, "name": "one", "fast": True, "kind": "small"}
    assert test.entries == {"answer": "tests/main/1/answer"}
    assert yaml_paths(ONE_SCALAR_TEST | {"tests/test.yaml", "tests/main/test.yaml"}) == [
        "tests/main/1/test.yaml"
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (b"seconds: 2.5\nname: one\nfast: true\n", "kind is not given."),
        (GOOD_YAML + b"colour: red\n", "colour is not a field of the workflow's tests."),
        (GOOD_YAML.replace(b"2.5", b"fast"), "seconds: Must be a number."),
        (GOOD_YAML.replace(b"2.5", b".nan"), "seconds: Must be a number."),
        (GOOD_YAML.replace(b"name: one", b"name: 1"), "name: Must be text."),
        (GOOD_YAML.replace(b"fast: true", b"fast: 1"), "fast: Must be true or false."),
        (GOOD_YAML.replace(b"small", b"huge"), "kind: Must be one of small, large."),
        (b"[1, 2]\n", "must be a mapping"),
        (GOOD_YAML + b"seconds: 3\n", "given twice"),
    ],
)
def test_a_test_yaml_that_breaks_the_fields_is_refused_at_it(text: bytes, message: str) -> None:
    problems = read_problems(ONE_SCALAR_TEST, SCALARS, {"tests/main/1/test.yaml": text})

    assert [problem["path"] for problem in problems] == ["tests/main/1/test.yaml"]
    assert message in problems[0]["message"]


def test_a_test_without_its_test_yaml_is_refused() -> None:
    assert read_problems({"tests/main/1/answer"}, SCALARS) == [
        {
            "path": "tests/main/1/",
            "message": "The test needs a test.yaml giving seconds, name, fast, kind.",
        }
    ]


def test_a_test_yaml_is_refused_when_the_workflow_has_no_scalar_field() -> None:
    paths = classic_tests("main/1") | {"tests/main/1/test.yaml"}

    assert read_problems(paths, yamls={"tests/main/1/test.yaml": b"x: 1\n"}) == [
        {
            "path": "tests/main/1/test.yaml",
            "message": "The workflow's tests have no text, number, true-or-false or enum field, "
            "so a test has no test.yaml.",
        }
    ]


def test_a_scalar_field_given_as_a_file_is_an_entry_for_no_field() -> None:
    problems = read_problems(
        ONE_SCALAR_TEST | {"tests/main/1/seconds"}, SCALARS, {"tests/main/1/test.yaml": GOOD_YAML}
    )

    assert problems == [
        {
            "path": "tests/main/1/seconds",
            "message": "The test main/1 has an entry for no field: seconds.",
        }
    ]


def groups_of(groups: str, paths: Collection[str]) -> list[Problem]:
    task = parse_task(task_with(groups=groups))
    return group_problems(task, cases_of(CLASSIC_WORKFLOW, paths, {}), paths)


def test_every_group_folder_is_a_group_and_every_group_a_folder() -> None:
    assert groups_of("  main: {each: 1}\n", classic_tests("main/1")) == []
    assert groups_of("  main: {each: 1}\n", classic_tests("main/1", "extra/1")) == [
        {
            "path": "tests/extra/",
            "message": "extra is a folder of tests/ but not a group in test_groups: list it, or "
            "move its tests.",
        }
    ]
    assert groups_of("  main: {each: 1}\n  large: {each: 1}\n", classic_tests("main/1")) == [
        {
            "path": "test_groups.large",
            "message": "There is no folder tests/large/ with a test in it.",
        }
    ]


def test_a_task_without_tests_is_refused_at_its_groups() -> None:
    task = parse_task(CLASSIC_TASK)

    assert group_problems(task, [], {"task.yaml"}) == [
        {
            "path": "test_groups",
            "message": "The task has no tests/ folder: add tests/<group>/<test>/.",
        }
    ]


def test_every_test_weight_names_a_test_of_its_group() -> None:
    groups = "  main: {each: 1, test_weights: {'2': 3, '9': 2}}\n  samples: {}\n"
    paths = classic_tests("main/1", "main/2", "samples/9")

    assert groups_of(groups, paths) == [
        {"path": "test_groups.main.test_weights.9", "message": "main/9 is not a test of main."}
    ]


# Compiling


TWO_GROUPS = "  main: {each: 100}\n  samples: {}\n"


def test_the_classic_task_compiles_to_a_plan_the_runner_takes() -> None:
    text = task_with(groups=TWO_GROUPS)
    result = compiled(text, classic_tests("main/1", "main/2", "samples/1"))
    plan = result.plan
    written = document(plan)

    assert violation(written, "plan") is None
    assert written["schema_version"] == 5
    assert written["harness_image"] == HARNESS
    assert written["tests"] == ["main/1", "main/2", "samples/1"]
    assert written["contestant"] == {
        "submission": {"type": "file"},
        "language": {"type": "enum", "options": ["c", "cpp", "java", "python"]},
    }
    assert [entry["id"] for entry in written["steps"]] == ["compile", "run", "check"]
    assert written["report"] == {
        "time_ms": {"step": "run", "output": "time_ms", "at_least": 0},
        "memory_kb": {"step": "run", "output": "memory_kb", "at_least": 0},
        "log": {"step": "compile", "output": "compile_log"},
    }
    assert (result.sealed, result.notes, result.held) == ((), (), NOTHING_SEALED)


def test_a_once_step_takes_the_contestants_file_into_a_folder_port() -> None:
    compile_ = step(compiled(CLASSIC_TASK, classic_tests("main/1")).plan, "compile")

    assert compile_ == {
        "id": "compile",
        "primitive": "unicon/compile@v2",
        "image": f"ghcr.io/uniconhq/primitive-compile@{PLACEHOLDER_DIGEST}",
        "network": False,
        "limits": {
            "time_ms": 60000,
            "cpu_ms": 60000,
            "memory_mb": 1024,
            "pids": 128,
            "output_mb": 64,
            "gpus": 0,
        },
        "outputs": {"binary": "file", "compile_log": "text", "outcome": "outcome"},
        "folders": ["source"],
        "inputs": {
            "source": {"submission": "submission"},
            "language": {"submission": "language"},
        },
    }


def test_a_batching_primitive_is_one_step_with_an_item_per_test_and_its_limits_raised() -> None:
    text = task_with(groups=TWO_GROUPS)
    plan = compiled(text, classic_tests("main/1", "main/2", "samples/1")).plan

    run = step(plan, "run")
    assert [item["test"] for item in run["batch"]] == ["main/1", "main/2", "samples/1"]
    assert run["batch"][0]["inputs"] == {
        "binary": {"step": "compile", "output": "binary"},
        "input": {"task": "tests/main/1/input"},
        "time_limit": {"value": 2},
        "memory_limit": {"value": 256},
    }
    # time_limit 2 raises time_ms and cpu_ms to 2 * 2000 + 3000 for each of
    # the three items, summed; memory_limit 256 raises memory_mb to 512.
    assert run["limits"] == {
        "time_ms": 21000,
        "cpu_ms": 21000,
        "memory_mb": 512,
        "pids": 128,
        "output_mb": 64,
        "gpus": 0,
    }
    check = step(plan, "check")
    assert check["batch"][2]["inputs"] == {
        "actual": {"step": "run", "output": "output"},
        "expected": {"task": "tests/samples/1/answer"},
    }
    assert check["limits"]["time_ms"] == 3 * 2000
    assert check["outputs"] == {"outcome": "outcome"}
    assert "folders" not in check


def test_a_limit_is_raised_and_rounded_up() -> None:
    low = task_with(inputs="  time_limit: 0.5\n  memory_limit: 1.2\n")
    run = step(compiled(low, classic_tests("main/1")).plan, "run")
    assert (run["limits"]["time_ms"], run["limits"]["memory_mb"]) == (4000, 258)

    odd = task_with(inputs="  time_limit: 1.0001\n  memory_limit: 256\n")
    run = step(compiled(odd, classic_tests("main/1")).plan, "run")
    assert run["limits"]["time_ms"] == 5001


def test_a_non_batching_per_test_step_is_one_entry_per_test() -> None:
    text = task_with(
        workflow="acme/output-only@v1",
        inputs="  answers: {max_size: 1MB}\n",
        groups="  main: {each: 1}\n",
    )
    plan = compiled(text, classic_tests("main/1", "main/2"), OUTPUT_ONLY).plan
    written = document(plan)

    assert violation(written, "plan") is None
    assert written["contestant"] == {"answers": {"type": "file", "per_test": True}}
    assert [(entry["id"], entry["test"]) for entry in written["steps"]] == [
        ("check", "main/1"),
        ("check", "main/2"),
    ]
    assert written["steps"][1]["inputs"] == {
        "actual": {"submission": "answers"},
        "expected": {"task": "tests/main/2/answer"},
    }
    assert written["steps"][1]["limits"]["time_ms"] == 1000


def test_a_plan_is_the_same_bytes_every_time_and_reads_back() -> None:
    first = compiled(CLASSIC_TASK, classic_tests("main/1")).plan
    second = compiled(CLASSIC_TASK, classic_tests("main/1")).plan

    assert first.to_bytes() == second.to_bytes()
    assert first.to_bytes().endswith(b"}\n")
    assert Plan.from_bytes(first.to_bytes()) == first


def test_a_plan_names_every_task_path_its_values_name() -> None:
    plan = compiled(CLASSIC_TASK, classic_tests("main/1", "main/2")).plan

    assert plan.task_paths() == (
        "tests/main/1/answer",
        "tests/main/1/input",
        "tests/main/2/answer",
        "tests/main/2/input",
    )


TUNABLE_TASK = """\
name: Tunable
workflow: acme/tunable@v1
inputs:
  verbose: true
  scale: 2.50
  memory_limit: 256
test_groups:
  main: {each: 1}
"""
TUNABLE_PATHS = classic_tests("main/1", "main/2") | {
    "tests/main/1/test.yaml",
    "tests/main/2/test.yaml",
}
TUNABLE_YAMLS = {
    "tests/main/1/test.yaml": b"seconds: 1\nname: one\n",
    "tests/main/2/test.yaml": b"seconds: 2.5\nname: two words\n",
}


def test_scalars_are_written_in_and_a_contestants_number_becomes_a_template() -> None:
    plan = compiled(TUNABLE_TASK, TUNABLE_PATHS, TUNABLE, yamls=TUNABLE_YAMLS).plan

    assert violation(document(plan), "plan") is None
    first, second = step(plan, "run")["batch"]
    assert first["inputs"]["args"] == {
        "template": "--n {0} --scale 2.5 --v true --test one {{raw}}",
        "parts": [{"submission": "n"}],
    }
    assert second["inputs"]["args"]["template"] == (
        "--n {0} --scale 2.5 --v true --test two words {{raw}}"
    )
    assert (first["inputs"]["time_limit"], second["inputs"]["time_limit"]) == (
        {"value": 1},
        {"value": 2.5},
    )
    # The first test's limit raises nothing past the primitive's own 5000;
    # the second's raises it to 2.5 * 2000 + 3000.
    assert step(plan, "run")["limits"]["time_ms"] == 5000 + 8000


def test_a_string_with_only_the_tasks_and_the_tests_scalars_is_a_plain_value() -> None:
    workflow = parse_workflow(
        CLASSIC.replace(
            b"memory_limit: ${{ inputs.memory_limit }}",
            b"memory_limit: ${{ inputs.memory_limit }}\n"
            + b'      args: "-t ${{ inputs.time_limit }} {x}"',
        )
    )
    plan = compiled(
        CLASSIC_TASK.replace("time_limit: 2", "time_limit: 2.50"), classic_tests("main/1"), workflow
    ).plan

    assert step(plan, "run")["batch"][0]["inputs"]["args"] == {"value": "-t 2.5 {x}"}


def test_a_contestant_scalar_given_whole_to_a_text_port_is_a_template_of_itself() -> None:
    workflow = parse_workflow(
        CLASSIC.replace(
            b"memory_limit: ${{ inputs.memory_limit }}",
            b"memory_limit: ${{ inputs.memory_limit }}\n      args: ${{ inputs.language }}",
        )
    )
    plan = compiled(CLASSIC_TASK, classic_tests("main/1"), workflow).plan

    assert step(plan, "run")["batch"][0]["inputs"]["args"] == {
        "template": "{0}",
        "parts": [{"submission": "language"}],
    }


@pytest.mark.parametrize(
    ("value", "spelling"),
    [
        (True, "true"),
        (False, "false"),
        (2, "2"),
        (2.0, "2"),
        (2.50, "2.5"),
        (0.1, "0.1"),
        (1e-7, "0.0000001"),
        (1e21, "1000000000000000000000"),
        ("cpp", "cpp"),
    ],
)
def test_a_scalar_has_one_spelling_as_text(value: object, spelling: str) -> None:
    assert spelled(value) == spelling


CLASSIC_INPUTS = "  submission: {label: Your solution}\n  language: {options: [python]}\n"


@pytest.mark.parametrize(
    ("inputs", "path", "message"),
    [
        (
            CLASSIC_INPUTS + "  memory_limit: 256\n",
            "inputs.time_limit",
            "Give this input a value: a number.",
        ),
        (
            CLASSIC_INPUTS + "  time_limit: 2\n  memory_limit: 256\n  colour: red\n",
            "inputs.colour",
            "The workflow unicon/classic@v2 has no input colour.",
        ),
        (
            CLASSIC_INPUTS + "  time_limit: two\n  memory_limit: 256\n",
            "inputs.time_limit",
            "Must be a number.",
        ),
        (
            CLASSIC_INPUTS + "  time_limit: true\n  memory_limit: 256\n",
            "inputs.time_limit",
            "Must be a number.",
        ),
        (
            "  submission: main.py\n  time_limit: 2\n  memory_limit: 256\n",
            "inputs.submission",
            "The contestant gives this one",
        ),
        (
            "  language: {options: [rust]}\n  time_limit: 2\n  memory_limit: 256\n",
            "inputs.language.options",
            "rust is not an option of the workflow's: c, cpp, java, python.",
        ),
        (
            "  language: {options: [python], default: c}\n  time_limit: 2\n  memory_limit: 256\n",
            "inputs.language.default",
            "Must be one of python.",
        ),
        (
            "  submission: {max_size: 3GB}\n  time_limit: 2\n  memory_limit: 256\n",
            "inputs.submission.max_size",
            "Must be at most 2GB",
        ),
        (
            "  submission: {min: 1}\n  time_limit: 2\n  memory_limit: 256\n",
            "inputs.submission.min",
            "A file input does not take min.",
        ),
    ],
)
def test_a_value_the_task_gives_wrongly_is_refused_at_its_line(
    inputs: str, path: str, message: str
) -> None:
    problems = refused(task_with(inputs=inputs), classic_tests("main/1"))

    assert [problem["path"] for problem in problems] == [path]
    assert message in problems[0]["message"]


def test_a_contestant_input_with_no_details_may_be_left_out() -> None:
    text = task_with(inputs="  time_limit: 2\n  memory_limit: 256\n")

    assert compiled(text, classic_tests("main/1")).plan.contestant.keys() == {
        "submission",
        "language",
    }


CHECKED_TASK = """\
name: Checked
workflow: acme/checked@v1
inputs:
  time_limit: 2
  memory_limit: 256
  better: higher
test_groups:
  main: {each: 1}
"""


def test_an_enum_value_is_one_of_the_workflows_options() -> None:
    problems = refused(
        CHECKED_TASK.replace("better: higher", "better: sideways"), classic_tests("main/1"), CHECKED
    )

    assert problems == [{"path": "inputs.better", "message": "Must be one of higher, lower."}]


NOTEBOOK_TASK = """\
name: Digits
workflow: acme/notebook@v1
inputs:
  data: data/mnist-test/
test_groups:
  main: {each: 1, show: after_close}
"""
NOTEBOOK_PATHS = {"data/mnist-test/images.bin", "tests/main/1/answer", "tests/main/2/answer"}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("data/empty/", "There is no file under data/empty/ in the task."),
        ("data/mnist-test", "Names a folder, so it ends with /, such as data/."),
        ("../data/", "Must not have empty, . or .. parts."),
        ("/data/", "Must be a path from the top of the task repo, with forward slashes."),
    ],
)
def test_a_folder_value_names_a_folder_with_a_file_in_the_task(data: str, message: str) -> None:
    text = NOTEBOOK_TASK.replace("data/mnist-test/", data)

    assert refused(text, NOTEBOOK_PATHS, NOTEBOOK) == [{"path": "inputs.data", "message": message}]


def test_a_task_folder_into_a_folder_port_is_listed_in_folders() -> None:
    plan = compiled(NOTEBOOK_TASK, NOTEBOOK_PATHS, NOTEBOOK).plan
    predict = step(plan, "predict")

    assert predict["folders"] == ["data"]
    assert predict["inputs"] == {
        "notebook": {"submission": "notebook"},
        "data": {"task": "data/mnist-test/"},
    }
    assert violation(document(plan), "plan") is None
    assert plan.task_paths() == ("data/mnist-test/", "tests/main/1/answer", "tests/main/2/answer")


def test_an_optional_input_left_out_gives_its_port_nothing() -> None:
    predict = step(compiled(NOTEBOOK_TASK, NOTEBOOK_PATHS, NOTEBOOK).plan, "predict")

    assert "key" not in predict["inputs"]


# Secrets


SECRET_TASK = NOTEBOOK_TASK.replace(
    "data/mnist-test/\n", "data/mnist-test/\n  key: {secret: openai}\n"
)


def test_a_secret_the_org_does_not_hold_is_refused() -> None:
    assert refused(SECRET_TASK, NOTEBOOK_PATHS, NOTEBOOK) == [
        {"path": "inputs.key", "message": "The org holds no secret named openai."}
    ]


def test_a_secret_into_a_marked_text_port_is_carried_by_name() -> None:
    plan = compiled(SECRET_TASK, NOTEBOOK_PATHS, NOTEBOOK, secrets={"openai"}).plan

    assert step(plan, "predict")["inputs"]["key"] == {"secret": "openai"}
    assert violation(document(plan), "plan") is None


def test_a_secret_into_an_unmarked_port_is_refused() -> None:
    workflow = parse_workflow(_notebook_with("note: ${{ inputs.key }}"))

    assert refused(SECRET_TASK, NOTEBOOK_PATHS, workflow, secrets={"openai"}) == [
        {
            "path": "inputs.key",
            "message": "key is a secret, and steps[0] gives it to note, which can hand the "
            "secret to the contestant's program.",
        }
    ]


def test_a_secret_written_into_text_is_refused() -> None:
    workflow = parse_workflow(_notebook_with('note: "Bearer ${{ inputs.key }}"'))

    assert refused(SECRET_TASK, NOTEBOOK_PATHS, workflow, secrets={"openai"}) == [
        {
            "path": "inputs.key",
            "message": "key is a secret, and steps[0] writes it into text; give the secret to "
            "its own port.",
        }
    ]


def _notebook_with(line: str) -> bytes:
    """The notebook workflow with `key` wired by `line` in place of its own."""
    text = (
        "inputs:\n"
        "  notebook: {type: file, contestant: true}\n"
        "  data: folder\n"
        "  key: text\n"
        "test:\n  answer: file\n"
        "steps:\n"
        "  - id: predict\n"
        "    use: acme/notebook@v1\n"
        "    with:\n"
        "      notebook: ${{ inputs.notebook }}\n"
        "      data: ${{ inputs.data }}\n"
        f"      {line}\n"
    )
    return text.encode()


def test_a_secret_is_text_naming_a_secret() -> None:
    text = SECRET_TASK.replace("{secret: openai}", "{secret: openai, also: 1}")

    assert refused(text, NOTEBOOK_PATHS, NOTEBOOK, secrets={"openai"}) == [
        {
            "path": "inputs.key",
            "message": "Must be text, or {secret: <name>} naming a secret the org holds.",
        }
    ]


# Sealed steps (T5)


def test_a_step_running_contestant_code_over_data_the_contestant_is_not_served_is_sealed() -> None:
    result = compiled(NOTEBOOK_TASK, NOTEBOOK_PATHS, NOTEBOOK)

    assert result.sealed == ("predict", "score")
    assert result.held == Sealed(
        steps=frozenset({"predict"}), values=frozenset({"accuracy", "fraction"})
    )
    predict, score, credit = result.notes
    assert predict == (
        "Step predict is sealed: it runs the contestant's code over data/mnist-test/, which the "
        "contestant is not served, so what it reports is shown at the reveal."
    )
    assert score.startswith("Step score is sealed")
    assert credit == "fraction is reported, but credit is not set: an accepted test earns 1."


def test_a_task_with_a_sealed_step_shows_every_group_after_close() -> None:
    text = NOTEBOOK_TASK.replace(
        "  main: {each: 1, show: after_close}\n", "  main: {each: 1}\n  samples: {show: verdict}\n"
    )
    paths = NOTEBOOK_PATHS | {"tests/samples/1/answer"}

    assert refused(text, paths, NOTEBOOK) == [
        {
            "path": "test_groups.main.show",
            "message": "main is shown always, but step predict gives the contestant's code "
            "data/mnist-test/; show it after_close, or serve the data under public/.",
        },
        {
            "path": "test_groups.samples.show",
            "message": "samples is shown verdict, but step predict gives the contestant's code "
            "data/mnist-test/; show it after_close, or serve the data under public/.",
        },
    ]


def test_data_served_under_public_seals_nothing() -> None:
    text = NOTEBOOK_TASK.replace("data/mnist-test/", "public/mnist/").replace(
        ", show: after_close", ""
    )
    result = compiled(text, {"public/mnist/images.bin", "tests/main/1/answer"}, NOTEBOOK)

    assert (result.sealed, result.held) == ((), NOTHING_SEALED)
    assert not any(note.startswith("Step") for note in result.notes)


def test_classic_is_not_sealed_since_each_run_reads_only_its_own_tests_input() -> None:
    result = compiled(CLASSIC_TASK, classic_tests("main/1", "main/2"))

    assert result.sealed == ()


UNSEALED = parse_workflow(
    """inputs:
  answers: {type: file, contestant: true}
  expected: file
test:
  answer: file
steps:
  - id: check
    use: acme/each@v1
    with:
      actual: ${{ inputs.answers }}
      expected: ${{ inputs.expected }}
"""
)
"""A contestant's file checked once against a file of the task's, by a
primitive that runs neither."""
UNSEALED_TASK = """\
name: T
workflow: acme/unsealed@v1
inputs:
  expected: secret/expected.txt
test_groups:
  main: {each: 1}
"""
UNSEALED_PATHS = {"secret/expected.txt", "tests/main/1/answer"}


def test_a_step_that_runs_no_contestant_code_is_not_sealed_by_hidden_data() -> None:
    result = compiled(UNSEALED_TASK, UNSEALED_PATHS, UNSEALED)

    assert result.sealed == ()
    assert step(result.plan, "check")["inputs"]["expected"] == {"task": "secret/expected.txt"}


@pytest.mark.parametrize(
    ("expected", "message"),
    [
        ("secret/missing.txt", "There is no file secret/missing.txt in the task."),
        ("secret/", "Names one file, so it does not end with /."),
        ("secret//expected.txt", "Must not have empty, . or .. parts."),
        (
            "secret\\expected.txt",
            "Must be a path from the top of the task repo, with forward slashes.",
        ),
        ("3", "Must be a path in the task repo, such as data/ or checker/checker.cpp."),
    ],
)
def test_a_file_value_names_a_file_in_the_task(expected: str, message: str) -> None:
    text = UNSEALED_TASK.replace("secret/expected.txt", expected)

    assert refused(text, UNSEALED_PATHS, UNSEALED) == [
        {"path": "inputs.expected", "message": message}
    ]


# Credit (T3, T4)


@pytest.mark.parametrize(
    "credit", ["fraction", "{relative: steps_taken}", "{relative: chosen}", "{relative: fraction}"]
)
def test_credit_names_a_number_fit_to_be_one(credit: str) -> None:
    text = task_with(CHECKED_TASK, workflow="acme/checked@v1", credit=credit)

    compiled(text, classic_tests("main/1"), CHECKED)


@pytest.mark.parametrize(
    ("credit", "path", "message"),
    [
        (
            "steps_taken",
            "credit",
            "steps_taken is not a credit: its workflow does not bound it to 0 to 1.",
        ),
        ("raw", "credit", "raw is not a credit: its workflow does not bound it to 0 to 1."),
        ("miss", "credit", "miss is lower is better, so it is not a credit."),
        ("log", "credit", "log is not a number the workflow reports per test."),
        ("ghost", "credit", "ghost is not a number the workflow reports per test."),
        (
            "{relative: raw}",
            "credit.relative",
            "raw has no direction: its workflow declares no better.",
        ),
        (
            "{relative: reward}",
            "credit.relative",
            "reward is not bounded below by 0: its workflow declares no at_least of 0 or more.",
        ),
        (
            "{relative: log}",
            "credit.relative",
            "log is not a number the workflow reports per test, with a direction.",
        ),
    ],
)
def test_credit_naming_a_number_unfit_to_be_one_is_refused(
    credit: str, path: str, message: str
) -> None:
    text = task_with(CHECKED_TASK, workflow="acme/checked@v1", credit=credit)

    assert refused(text, classic_tests("main/1"), CHECKED) == [{"path": path, "message": message}]


def test_a_bounded_number_credit_does_not_name_is_reported_not_refused() -> None:
    plain = compiled(CHECKED_TASK, classic_tests("main/1"), CHECKED)
    assert plain.notes == (
        "fraction is reported, but credit is not set: an accepted test earns 1.",
        "miss is reported, but credit is not set: an accepted test earns 1.",
    )

    credited = compiled(
        task_with(CHECKED_TASK, workflow="acme/checked@v1", credit="fraction"),
        classic_tests("main/1"),
        CHECKED,
    )
    assert credited.notes == ("miss is reported, but credit names another value.",)


def test_a_compiled_report_carries_each_numbers_bounds() -> None:
    report = document(compiled(CHECKED_TASK, classic_tests("main/1"), CHECKED).plan)["report"]

    assert report["fraction"] == {
        "step": "score",
        "output": "fraction",
        "at_least": 0,
        "at_most": 1,
    }
    assert report["reward"] == {"step": "score", "output": "steps"}
    assert report["log"] == {"step": "compile", "output": "compile_log"}
    assert step(compiled(CHECKED_TASK, classic_tests("main/1"), CHECKED).plan, "score")[
        "outputs"
    ] == {"fraction?": "number", "steps": "number", "outcome": "outcome"}


THIRTY_DIGITS = "-12345678901234.5678901234567891"
"""A bound of 30 significant digits, more than a float holds."""


def test_a_bound_of_thirty_significant_digits_survives_from_the_file_to_a_score() -> None:
    workflow = parse_workflow(
        CHECKED_TEXT.replace(
            "fold: sum, better: higher}", f"fold: sum, better: higher, at_least: {THIRTY_DIGITS}}}"
        )
    )
    built = compiled(CHECKED_TASK, classic_tests("main/1"), workflow)

    written_plan = exact_json.loads(built.plan.to_bytes())
    assert written_plan["report"]["reward"]["at_least"] == Decimal(THIRTY_DIGITS)
    assert Plan.from_bytes(built.plan.to_bytes()).report["reward"].at_least == Decimal(
        THIRTY_DIGITS
    )
    noted = read_note(write_note(False, (), measures=built.measures)).measures["reward"]
    # A test without the value counts as the bound, to the last digit.
    assert fold(noted, [None]) == Fraction(THIRTY_DIGITS)
    assert written(fold(noted, [None, Fraction(1)]) or Fraction(0)) == (
        "-12345678901233.5678901234567891"
    )


# Fit


def test_a_time_limit_pushing_the_run_past_the_ceiling_is_refused_at_the_input() -> None:
    text = task_with(inputs=CLASSIC_INPUTS + "  time_limit: 700\n  memory_limit: 256\n")

    assert refused(text, classic_tests("main/1")) == [
        {
            "path": "inputs.time_limit",
            "message": "1 test at time_limit 700 gives the run 27 minutes; a run may take 25.",
        }
    ]


def test_a_batch_sums_its_items_against_the_ceiling() -> None:
    sixty = [f"main/{number}" for number in range(1, 61)]
    text = task_with(inputs=CLASSIC_INPUTS + "  time_limit: 10\n  memory_limit: 256\n")

    assert refused(text, classic_tests(*sixty)) == [
        {
            "path": "inputs.time_limit",
            "message": "60 tests at time_limit 10 give the run 28 minutes; a run may take 25.",
        }
    ]
    fits = task_with(inputs=CLASSIC_INPUTS + "  time_limit: 5\n  memory_limit: 256\n")
    compiled(fits, classic_tests(*sixty))


ACME["acme/timed@v1"] = declared(
    "timed",
    "batch: true\n"
    + LIMITS
    + """\
limits_from:
  time_ms: {input: seconds, scale: 1000}
inputs:
  seconds: number
  expected: {type: file, runs: false}
outputs:
  outcome: outcome
""",
)
PRIMITIVES_READ["acme/timed@v1"] = ACME["acme/timed@v1"]


def timed(seconds: str) -> WorkflowDefinition:
    """A workflow of one batch step whose time is `seconds` per test, given
    as `seconds` writes it.
    """
    return parse_workflow(
        f"""\
inputs:
  seconds: number
test:
  answer: file
  seconds: number
steps:
  - id: wait
    use: acme/timed@v1
    per_test: true
    with:
      seconds: {seconds}
      expected: ${{{{ test.answer }}}}
"""
    )


TIMED_TASK = (
    "name: T\nworkflow: acme/waiting@v1\ninputs:\n  seconds: {seconds}\n"
    "test_groups:\n  main: {{each: 1}}\n"
)


def timed_tests(seconds: Mapping[str, int]) -> tuple[set[str], dict[str, bytes]]:
    """The files of tests giving each its `seconds` in its `test.yaml`."""
    paths = {f"tests/{test}/{entry}" for test in seconds for entry in ("answer", "test.yaml")}
    yamls = {
        f"tests/{test}/test.yaml": f"seconds: {value}\n".encode() for test, value in seconds.items()
    }
    return paths, yamls


HUNDRED = {f"main/{number}": 1 for number in range(1, 101)}


def test_a_run_too_long_is_blamed_on_the_task_input_it_was_raised_from() -> None:
    paths, yamls = timed_tests(HUNDRED)
    workflow = timed("${{ inputs.seconds }}")

    assert refused(TIMED_TASK.format(seconds=15), paths, workflow, yamls=yamls) == [
        {
            "path": "inputs.seconds",
            "message": "100 tests at seconds 15 give the run 27 minutes; a run may take 25.",
        }
    ]


def test_a_run_too_long_is_blamed_on_the_test_that_gives_the_most() -> None:
    ten = {**{f"main/{number}": 2 for number in range(1, 11)}, "main/7": 1500}
    paths, yamls = timed_tests(ten)
    workflow = timed("${{ test.seconds }}")

    assert refused(TIMED_TASK.format(seconds=1), paths, workflow, yamls=yamls) == [
        {
            "path": "tests/main/7/test.yaml",
            "message": "10 tests at seconds up to 1500 (main/7) give the run 27 minutes; a run "
            "may take 25.",
        }
    ]
    paths, yamls = timed_tests({"main/1": 1500})
    assert refused(TIMED_TASK.format(seconds=1), paths, workflow, yamls=yamls) == [
        {
            "path": "tests/main/1/test.yaml",
            "message": "main/1's seconds 1500 gives the run 27 minutes; a run may take 25.",
        }
    ]


def test_a_run_too_long_is_blamed_on_the_raised_limit_that_adds_the_most_time() -> None:
    both = parse_workflow(
        """\
inputs:
  seconds: number
test:
  answer: file
  seconds: number
steps:
  - id: first
    use: acme/timed@v1
    per_test: true
    with: {seconds: "${{ inputs.seconds }}", expected: "${{ test.answer }}"}
  - id: second
    use: acme/timed@v1
    per_test: true
    with: {seconds: "${{ test.seconds }}", expected: "${{ test.answer }}"}
"""
    )
    paths, yamls = timed_tests(dict.fromkeys(HUNDRED, 2))
    [by_input] = refused(TIMED_TASK.format(seconds=13), paths, both, yamls=yamls)
    paths, yamls = timed_tests(dict.fromkeys(HUNDRED, 13))
    [by_test] = refused(TIMED_TASK.format(seconds=2), paths, both, yamls=yamls)

    assert by_input == {
        "path": "inputs.seconds",
        "message": "100 tests at seconds 13 give the run 27 minutes; a run may take 25.",
    }
    assert by_test == {
        "path": "tests/main/1/test.yaml",
        "message": "100 tests at seconds up to 13 (main/1) give the run 27 minutes; a run may "
        "take 25.",
    }


def test_a_run_too_long_from_a_number_the_workflow_writes_is_refused_at_the_workflow() -> None:
    paths, yamls = timed_tests(HUNDRED)

    assert refused(TIMED_TASK.format(seconds=1), paths, timed("15"), yamls=yamls) == [
        {
            "path": "workflow",
            "message": "100 tests at seconds 15, as acme/waiting@v1 writes it, give the run 27 "
            "minutes; a run may take 25.",
        }
    ]


def test_a_run_too_long_with_no_limit_raised_is_blamed_on_the_number_of_tests() -> None:
    many = [f"main/{number}" for number in range(1, 92)]
    text = task_with(
        workflow="acme/output-only@v1",
        inputs="  answers: {max_size: 1MB}\n",
        groups="  main: {each: 1}\n",
    )

    assert refused(text, classic_tests(*many), OUTPUT_ONLY) == [
        {
            "path": "workflow",
            "message": "91 tests through the steps of acme/output-only@v1 give the run 26 "
            "minutes; a run may take 25.",
        }
    ]
    compiled(text, classic_tests(*many[:90]), OUTPUT_ONLY)


def test_memory_beyond_the_platforms_machine_is_refused_at_the_input() -> None:
    text = task_with(inputs=CLASSIC_INPUTS + "  time_limit: 2\n  memory_limit: 20000\n")

    assert refused(text, classic_tests("main/1")) == [
        {
            "path": "inputs.memory_limit",
            "message": "memory_limit 20000 gives step run 20256 MB of memory; no machine this "
            "task may run on has more than 16384.",
        }
    ]
    assert Machine(memory_mb=16384, gpus=0, network=False) == PLATFORM_MACHINE


GPU = parse_workflow(
    """\
inputs:
  gpus: number
test:
  answer: file
steps:
  - {id: train, use: acme/gpu@v1, with: {gpus: "${{ inputs.gpus }}"}}
"""
)
GPU_TASK = "name: T\nworkflow: acme/gpu@v1\ninputs:\n  gpus: 1\ntest_groups:\n  main: {each: 1}\n"


def test_a_gpu_is_refused_while_no_machine_has_one() -> None:
    assert refused(GPU_TASK, {"tests/main/1/answer"}, GPU) == [
        {
            "path": "inputs.gpus",
            "message": "gpus 1 gives step train 1 GPUs; no machine this task may run on has more "
            "than 0.",
        }
    ]
    plan = compiled(GPU_TASK, {"tests/main/1/answer"}, GPU, machine=Machine(16384, 1, False)).plan
    assert step(plan, "train")["limits"]["gpus"] == 1
    compiled(GPU_TASK.replace("gpus: 1", "gpus: 0"), {"tests/main/1/answer"}, GPU)


ACME["acme/online@v1"] = declared(
    "online",
    "network: true\n" + LIMITS + "outputs:\n  outcome: outcome\n",
)
PRIMITIVES_READ["acme/online@v1"] = ACME["acme/online@v1"]
ONLINE = parse_workflow("test:\n  answer: file\nsteps:\n  - {id: fetch, use: acme/online@v1}\n")
ONLINE_TASK = "name: T\nworkflow: acme/online@v1\ntest_groups:\n  main: {each: 1}\n"


def test_a_step_reaching_the_network_is_refused_while_no_machine_gives_it() -> None:
    assert refused(ONLINE_TASK, {"tests/main/1/answer"}, ONLINE) == [
        {
            "path": "workflow",
            "message": "Step fetch uses acme/online@v1, which reaches the network, and no "
            "machine this task may run on gives a step the network.",
        }
    ]
    online = Machine(16384, 0, True)
    plan = compiled(ONLINE_TASK, {"tests/main/1/answer"}, ONLINE, machine=online).plan
    assert step(plan, "fetch")["network"] is True


# The runner's contract


def test_a_plan_the_runners_contract_refuses_is_refused_at_the_workflow() -> None:
    task = parse_task(CLASSIC_TASK)
    paths = classic_tests("main/1")
    tests = cases_of(CLASSIC_WORKFLOW, paths, {})

    with pytest.raises(InvalidDefinition) as raised:
        compile_plan(
            task,
            CLASSIC_WORKFLOW,
            PRIMITIVES_READ,
            paths,
            tests,
            secrets=frozenset(),
            machine=PLATFORM_MACHINE,
            harness_image="harness@sha256:" + "1" * 64,
        )

    (problem,) = raised.value.errors
    assert problem["path"] == "workflow"
    assert problem["message"].startswith(
        "The plan this task compiles to with unicon/classic@v2 breaks the runner's contract at "
        "harness_image: "
    )


def test_a_test_id_longer_than_the_contract_allows_is_refused_at_its_folder() -> None:
    group, test = "g" * 200, "t" * 55
    paths = classic_tests(f"{group}/{test}", f"{group}/{test[:54]}")

    tests, problems = read_tests(paths, CLASSIC_WORKFLOW.test, {})

    assert [case.id for case in tests] == [f"{group}/{test[:54]}"]
    assert problems == [
        {
            "path": f"tests/{group}/{test}/",
            "message": "A test's id, <group>/<test>, is at most 255 characters; this one has 256.",
        }
    ]


# The starter


def test_the_starter_task_compiles_against_the_seeded_classic() -> None:
    files = starter_task("Sum")
    task = parse_task(files["task.yaml"])
    paths = set(files)

    assert check_workflow(CLASSIC_WORKFLOW, PRIMITIVES_READ) == []
    tests, problems = read_tests(paths, CLASSIC_WORKFLOW.test, {})
    assert problems == [] and group_problems(task, tests, paths) == []
    result = compile_plan(
        task,
        CLASSIC_WORKFLOW,
        PRIMITIVES_READ,
        paths,
        tests,
        secrets=frozenset(),
        machine=PLATFORM_MACHINE,
        harness_image=HARNESS,
    )

    assert result.plan.tests == ("main/1",)
    assert violation(document(result.plan), "plan") is None


# What changed how a task grades


def snapshot(plan: bytes = b"plan", **data: str) -> Snapshot:
    return Snapshot(
        plans={PLAN_PATH: plan}, data={key.replace("__", "/"): value for key, value in data.items()}
    )


def test_a_first_publication_changes_nothing() -> None:
    assert grading_changes(None, snapshot()) == ()


def test_the_same_plan_and_data_change_nothing() -> None:
    assert (
        grading_changes(snapshot(tests__main__1__input="a"), snapshot(tests__main__1__input="a"))
        == ()
    )


def test_a_changed_plan_is_named() -> None:
    assert grading_changes(snapshot(), snapshot(b"other")) == ("plans/plan.json changed",)


def test_a_plan_whose_numbers_are_only_spelled_otherwise_changes_nothing() -> None:
    before = snapshot(b'{"limit": 2.5, "at_least": 1000.0}\n')
    after = snapshot(b'{\n  "at_least": 1E+3,\n  "limit": 2.50\n}\n')

    assert grading_changes(before, after) == ()
    assert grading_changes(before, snapshot(b'{"limit": 2.6, "at_least": 1000.0}\n')) == (
        "plans/plan.json changed",
    )


def test_a_data_file_added_removed_or_changed_is_named() -> None:
    before = snapshot(tests__main__1__input="a", tests__main__1__answer="b")
    after = snapshot(tests__main__1__input="c", tests__main__2__input="d")

    assert grading_changes(before, after) == (
        "tests/main/1/answer removed",
        "tests/main/1/input changed",
        "tests/main/2/input added",
    )


def test_only_the_plans_folder_is_reserved() -> None:
    assert is_reserved("plans/plan.json")
    assert is_reserved("plans")
    assert not is_reserved("plansx/plan.json")
    assert not is_reserved("tests/plans/1/input")
