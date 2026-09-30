"""The compiler: one flat plan per stage in the runner's plan shape version 3,
every step carrying its primitive's image, entrypoint and limits, every
`foreach` expanded over the task's actual tests and folded where the
primitive batches, every value resolved, the verdict block from the
workflow's outputs, the same bytes every time; what it refuses, each at its
YAML path; only the compiler writes inside `plans/`; and what a publication
names as changing how it grades, in the note it carries. The shape a plan is
held to below is the contract's section 2, written out by hand.
"""

import json
import re
from collections.abc import Collection
from typing import Any

import pytest

from forge.domain.definitions import TaskDefinition, parse_task, starter_task
from forge.domain.plans import (
    HARNESS_IMAGE,
    Plan,
    Snapshot,
    cases_of,
    compile_plans,
    grading_changes,
    is_reserved,
    plan_path,
)
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.publications import Note, read_note, write_note
from forge.domain.workflow_definition import WorkflowDefinition, parse_workflow
from forge.domain.yaml_models import InvalidDefinition
from forge.testing import CLASSIC, PLACEHOLDER_DIGEST, PRIMITIVES

STARTER = starter_task("Sum")["task.yaml"]
TESTS = ("data/testcases/1.in", "data/testcases/1.ans", "data/testcases/2.in")
PATHS = (*TESTS, "data/testcases/2.ans", "data/testcases/10.in", "data/testcases/10.ans")


def classic() -> dict[str, WorkflowDefinition]:
    return {"unicon/classic@v1": parse_workflow(CLASSIC)}


def primitives(**extra: bytes) -> dict[str, PrimitiveDeclaration]:
    found = {f"unicon/{name}@v1": parse_primitive(text) for name, text in PRIMITIVES.items()}
    found.update({ref: parse_primitive(text) for ref, text in extra.items()})
    return found


def task(extra: bytes = b"") -> TaskDefinition:
    return parse_task(STARTER + extra)


def compiled(
    definition: TaskDefinition | None = None,
    workflows: dict[str, WorkflowDefinition] | None = None,
    paths: Collection[str] = PATHS,
    declared: dict[str, PrimitiveDeclaration] | None = None,
) -> dict[str, Plan]:
    return compile_plans(
        definition or task(),
        workflows or classic(),
        declared or primitives(),
        paths,
        harness_image=HARNESS_IMAGE,
    )


def document(plan: Plan) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(plan.to_bytes())
    return loaded


def image(name: str) -> str:
    return f"ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}"


def test_the_starter_task_compiles_to_one_flat_plan_over_classic() -> None:
    plans = compiled()
    assert list(plans) == ["default"]
    plan = document(plans["default"])

    assert (plan["schema_version"], plan["harness_image"], plan["stage"]) == (
        3,
        HARNESS_IMAGE,
        "default",
    )
    assert plan["tests"] == ["1", "2", "10"]
    compile_, run, check = plan["steps"]
    assert compile_ == {
        "id": "compile",
        "primitive": "compile@v1",
        "image": image("compile"),
        "entrypoint": ["/usr/local/bin/compile"],
        "limits": {
            "time_ms": 60000,
            "cpu_ms": 60000,
            "memory_mb": 1024,
            "pids": 128,
            "output_mb": 64,
        },
        "inputs": {
            "source": {"submission": "submission"},
            "language": {"submission": "submission", "field": "language"},
        },
    }
    assert (run["id"], run["primitive"], run["image"]) == (
        "run",
        "sandbox-run@v1",
        image("sandbox-run"),
    )
    assert [item["test"] for item in run["batch"]] == ["1", "2", "10"]
    assert run["batch"][2]["inputs"] == {
        "binary": {"step": "compile", "output": "binary"},
        "input": {"task": "data/testcases/10.in"},
        "time_limit": {"value": 2.0},
        "memory_limit": {"value": 256},
    }
    assert check["batch"][0]["inputs"] == {
        "actual": {"step": "run", "output": "output", "test": "1"},
        "expected": {"task": "data/testcases/1.ans"},
    }
    assert plan["verdict"] == {
        "outcome": {"step": "check", "output": "outcome"},
        "metrics": {"points": {"step": "check", "output": "points"}},
        "tests": {
            "time_ms": {"step": "run", "output": "time_ms"},
            "memory_kb": {"step": "run", "output": "memory_kb"},
        },
        "summary": {"step": "compile", "output": "compile_log"},
    }


def test_a_batch_takes_the_limits_of_every_test_and_limits_from_raises_them() -> None:
    slow = parse_task(STARTER.replace(b"value: 2.0", b"value: 9.5").replace(b"256", b"900"))
    [run] = [step for step in compiled(slow)["default"].steps if step.id == "run"]
    assert run.limits.model_dump() == {
        "time_ms": 3 * (9.5 * 2000 + 3000),
        "cpu_ms": 3 * (9.5 * 2000 + 3000),
        "memory_mb": 900 + 256,
        "pids": 128,
        "output_mb": 64,
    }
    [quick] = [step for step in compiled()["default"].steps if step.id == "run"]
    assert (quick.limits.time_ms, quick.limits.memory_mb) == (3 * 7000, 512)


def test_a_foreach_over_a_hundred_tests_is_a_hundred_steps_unless_the_primitive_batches() -> None:
    hundred = [f"data/testcases/{n}.{ending}" for n in range(1, 101) for ending in ("in", "ans")]
    unbatched = PRIMITIVES["diff-check"].replace(b"batch: true", b"batch: false")
    plan = compiled(paths=hundred, declared=primitives(**{"unicon/diff-check@v1": unbatched}))
    steps = plan["default"].steps
    checks = [step for step in steps if step.id == "check"]
    runs = [step for step in steps if step.id == "run"]

    assert len(checks) == 100 and len(runs) == 1
    assert [step.test for step in checks[:3]] == ["1", "2", "3"]
    assert checks[9].inputs == {
        "actual": {"step": "run", "output": "output", "test": "10"},
        "expected": {"task": "data/testcases/10.ans"},
    }
    assert checks[0].limits.time_ms == 5000
    assert len(runs[0].batch or ()) == 100


def test_a_test_list_is_ordered_by_number_and_leaves_hidden_and_unended_files_out() -> None:
    tests, problems = cases_of(
        "data/t/",
        [
            "data/t/10.in",
            "data/t/2.in",
            "data/t/2.out",
            "data/t/b1.in",
            "data/t/a10.in",
            "data/t/a9.in",
            "data/t/.gitkeep",
            "data/t/README",
            "data/t/deep/3.in",
            "data/t/2.hint",
        ],
    )
    assert problems == []
    assert [test.id for test in tests] == ["2", "10", "a9", "a10", "b1"]
    assert tests[0].files == {
        "input": "data/t/2.in",
        "answer": "data/t/2.out",
        "hint": "data/t/2.hint",
    }


def test_a_test_with_two_answers_or_a_name_no_path_takes_is_refused() -> None:
    _, problems = cases_of("t/", ["t/1.in", "t/1.ans", "t/1.out", "t/a b.in"])
    assert problems == [
        "The test 1 has two answer files in t/.",
        "'a b' in t/ is not a test name: letters, digits, dots, hyphens and underscores, "
        "starting with a letter or a digit.",
    ]


def refusals(
    definition: TaskDefinition | None = None,
    workflows: dict[str, WorkflowDefinition] | None = None,
    paths: Collection[str] = PATHS,
    declared: dict[str, PrimitiveDeclaration] | None = None,
) -> list[tuple[str, str]]:
    with pytest.raises(InvalidDefinition) as error:
        compiled(definition, workflows, paths, declared)
    return [(problem["path"], problem["message"]) for problem in error.value.errors]


def test_a_limit_is_never_raised_from_an_infinity_or_past_one() -> None:
    with pytest.raises(InvalidDefinition) as error:
        parse_task(STARTER.replace(b"value: 2.0", b"value: .inf"))
    assert [problem["message"] for problem in error.value.errors] == ["Must be a number."]
    huge = parse_task(STARTER.replace(b"value: 2.0", b"value: 1.0e+308"))
    assert refusals(huge) == [
        (
            "workflow",
            "In unicon/classic@v1, steps[1].with.time_limit: The limit time_ms raised from "
            "time_limit is too large.",
        )
    ]


def test_a_folder_with_no_tests_is_refused_at_the_setter_value() -> None:
    assert refusals(paths=["data/testcases/.gitkeep"]) == [
        (
            "inputs.setter[0].value",
            "There is no test in data/testcases/: add files such as 1.in and 1.ans.",
        )
    ]


def test_a_test_missing_a_field_a_step_reads_is_refused_naming_it() -> None:
    [(path, message)] = refusals(paths=TESTS)
    assert path == "workflow"
    assert message == (
        "In unicon/classic@v1, steps[2].with.expected: The test 2 has no answer file in "
        "data/testcases/."
    )


def probe(
    steps: str, outputs: str = "outputs:\n  outcome: ${{ steps.compile.outcome }}\n"
) -> dict[str, WorkflowDefinition]:
    text = (
        "name: unicon/classic\nversion: v1\ninputs:\n"
        "  - {id: submission, type: code}\n  - {id: testcases, type: 'file[]'}\n"
        "  - {id: time_limit, type: number}\n  - {id: memory_limit, type: number}\n"
        f"steps:\n{steps}{outputs}"
    )
    return {"unicon/classic@v1": parse_workflow(text)}


COMPILE = (
    "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
    "      source: ${{ inputs.submission }}\n      language: ${{ inputs.submission.language }}\n"
)


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            COMPILE + "      extra: 1\n",
            "steps[0].with.extra: unicon/compile@v1 has no input extra.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ inputs.submission }}\n",
            "steps[0].with: unicon/compile@v1 needs the input language.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ inputs.time_limit }}\n      language: python\n",
            "steps[0].with.source: Takes file, not number.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ inputs.submission }}\n      language: rust\n",
            "steps[0].with.language: Takes one of python, c, cpp, java.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ steps.later.binary }}\n      language: python\n",
            "steps[0].with.source: later is not a step before this one.",
        ),
        (
            COMPILE + "  - id: run\n    use: unicon/sandbox-run@v1\n"
            "    foreach: ${{ inputs.testcases }}\n    with:\n"
            "      binary: ${{ steps.compile.nothing }}\n      input: ${{ item.input }}\n"
            "      time_limit: 1\n      memory_limit: 1\n",
            "steps[1].with.binary: The step compile has no output nothing.",
        ),
        (
            COMPILE + "  - id: run\n    use: unicon/sandbox-run@v1\n    with:\n"
            "      binary: ${{ steps.compile.binary }}\n      input: ${{ item.input }}\n"
            "      time_limit: 1\n      memory_limit: 1\n",
            "steps[1].with.input: item is there only inside a step with a foreach.",
        ),
        (
            COMPILE + "  - id: run\n    use: unicon/sandbox-run@v1\n"
            "    foreach: ${{ inputs.submission }}\n    with:\n"
            "      binary: ${{ steps.compile.binary }}\n      input: ${{ item.input }}\n"
            "      time_limit: 1\n      memory_limit: 1\n",
            "steps[1].foreach: A foreach runs over a setter's file[] input, ${{ inputs.<id> }}.",
        ),
        (
            COMPILE + "  - id: run\n    use: unicon/sandbox-run@v1\n    with:\n"
            "      binary: ${{ steps.compile.binary }}\n      input: ${{ inputs.submission }}\n"
            "      time_limit: ${{ inputs.submission }}\n      memory_limit: 1\n",
            "steps[1].with.time_limit: Takes number, not file.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ inputs.submission }}\n      language: [python]\n",
            "steps[0].with.language: A list or a mapping as an input comes with feature 10.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ inputs.submission }}\n"
            "      language: ${{ inputs.submission }} at ${{ inputs.submission }}\n",
            "steps[0].with.language: Only a setter's text, number or true-or-false input is "
            "written into text, not ${{ inputs.submission }}.",
        ),
        (
            "  - id: compile\n    use: unicon/compile@v1\n    with:\n"
            "      source: ${{ secrets.token }}\n      language: python\n",
            "steps[0].with.source: ${{ secrets.token }} is not a reference a workflow makes: "
            "inputs.<id>, inputs.<id>.language, steps.<id>.<output> or item.<field>.",
        ),
    ],
    ids=[
        "undeclared-input",
        "missing-input",
        "wrong-type",
        "enum-value",
        "later-step",
        "no-such-output",
        "item-outside-foreach",
        "foreach-contestant",
        "number-from-a-file",
        "a-list",
        "written-file",
        "unknown-reference",
    ],
)
def test_a_step_that_does_not_fit_its_primitive_is_refused_at_the_workflow(
    steps: str, message: str
) -> None:
    [(path, found)] = refusals(workflows=probe(steps))
    assert path == "workflow"
    assert found == f"In unicon/classic@v1, {message}"


def test_a_language_the_compile_step_does_not_take_is_refused() -> None:
    rust = parse_task(STARTER.replace(b"language: [python]", b"language: [python, rust]"))
    [(path, message)] = refusals(rust)
    assert path == "workflow"
    assert message == (
        "In unicon/classic@v1, steps[0].with.language: Takes one of python, c, cpp, java, not rust."
    )


@pytest.mark.parametrize(
    ("outputs", "message"),
    [
        ("outputs: {}\n", "outputs: A workflow's outputs give its outcome."),
        (
            "outputs:\n  outcome: ${{ steps.compile.compile_log }}\n",
            "outputs.outcome: steps.compile.compile_log is text, not outcome.",
        ),
        (
            "outputs:\n  outcome: ${{ steps.compile.outcome }}\n"
            "  tests:\n    time_ms: ${{ steps.compile.outcome }}\n",
            "outputs.tests.time_ms: steps.compile.outcome is outcome, not number.",
        ),
        (
            "outputs:\n  outcome: literal\n",
            "outputs.outcome: Must be ${{ steps.<id>.<output> }}.",
        ),
    ],
    ids=["no-outcome", "outcome-type", "test-type", "not-a-reference"],
)
def test_outputs_that_do_not_fill_the_verdict_are_refused(outputs: str, message: str) -> None:
    [(path, found)] = refusals(workflows=probe(COMPILE, outputs))
    assert (path, found) == ("workflow", f"In unicon/classic@v1, {message}")


def test_a_per_test_row_from_a_step_that_runs_once_is_refused() -> None:
    outputs = (
        "outputs:\n  outcome: ${{ steps.compile.outcome }}\n"
        "  summary: ${{ steps.compile.compile_log }}\n"
    )
    assert compiled(workflows=probe(COMPILE, outputs))["default"].verdict.summary is not None
    times = "outputs:\n  outcome: ${{ steps.compile.outcome }}\n  tests:\n"
    times += "    time_ms: ${{ steps.nothing.time_ms }}\n"
    [(_, message)] = refusals(workflows=probe(COMPILE, times))
    assert message.endswith(
        "outputs.tests.time_ms: steps.nothing.time_ms is not an output of a step."
    )


def test_a_foreach_over_a_second_list_is_refused_until_feature_10() -> None:
    staged = parse_task(
        STARTER.replace(
            b"  setter:\n",
            b"  setter:\n    - {id: extra, type: 'file[]', value: data/extra/}\n",
        )
    )
    workflow = parse_workflow(
        CLASSIC.replace(
            b"  - id: memory_limit\n", b"  - id: extra\n    type: file[]\n  - id: memory_limit\n"
        ).replace(
            b"    foreach: ${{ inputs.testcases }}\n    with:\n      actual",
            b"    foreach: ${{ inputs.extra }}\n    with:\n      actual",
        )
    )
    [(path, message)] = refusals(
        staged, {"unicon/classic@v1": workflow}, [*PATHS, "data/extra/1.in", "data/extra/1.ans"]
    )
    assert path == "workflow"
    assert message == (
        "In unicon/classic@v1, steps[2].foreach: Every foreach of a workflow runs over one list, "
        "data/testcases/, until feature 10."
    )


def test_a_use_that_is_not_among_the_primitives_read_is_refused() -> None:
    declared = primitives()
    del declared["unicon/diff-check@v1"]
    [(path, message)] = refusals(declared=declared)
    assert (path, message) == (
        "workflow",
        "In unicon/classic@v1, steps[2].use: unicon/diff-check@v1 is not a primitive.",
    )


def test_a_limit_raised_from_a_value_not_known_at_the_save_is_refused() -> None:
    contestant = parse_task(
        STARTER.replace(
            b"    - id: time_limit\n      type: number\n      value: 2.0\n", b""
        ).replace(b"  setter:\n", b"    - {id: time_limit, type: number}\n  setter:\n")
    )
    [(path, message)] = refusals(contestant)
    assert path == "workflow"
    assert message == (
        "In unicon/classic@v1, steps[1].with.time_limit: The limit time_ms is raised from "
        "time_limit, so it is a number known at the save."
    )


STAGES = b"""
stages:
  - id: validation
    counts: false
    inputs:
      setter:
        - id: testcases
          type: file[]
          value: data/testcases/public/
  - id: test
    trigger: at_end
    show: hidden
    workflow: unicon/classic@v2
    inputs:
      setter:
        - id: time_limit
          type: number
          value: 5.0
"""


def test_a_stage_takes_its_own_tests_setter_values_and_workflow() -> None:
    staged = task(STAGES)
    assert [(str(ref), at) for ref, at in staged.workflow_refs()] == [
        ("unicon/classic@v1", "workflow"),
        ("unicon/classic@v2", "stages[1].workflow"),
    ]
    v2 = parse_workflow(CLASSIC.replace(b"version: v1", b"version: v2"))
    paths = [*PATHS, "data/testcases/public/7.in", "data/testcases/public/7.ans"]
    plans = compiled(staged, {**classic(), "unicon/classic@v2": v2}, paths)
    assert list(plans) == ["validation", "test"]
    assert plans["validation"].tests == ("7",)
    assert plans["test"].tests == ("1", "2", "10")
    [run] = [step for step in plans["test"].steps if step.id == "run"]
    assert run.batch is not None
    assert run.batch[0].inputs["time_limit"] == {"value": 5.0}


def test_an_empty_stage_list_is_refused_at_the_stage_value() -> None:
    [(path, _)] = refusals(
        task(STAGES), {**classic(), "unicon/classic@v2": classic()["unicon/classic@v1"]}
    )
    assert path == "stages[0].inputs.setter[0].value"


def test_a_setter_value_is_written_into_text_and_a_folder_given_whole_is_its_files() -> None:
    declared = primitives(
        **{
            "unicon/probe@v1": b"""\
name: unicon/probe
version: v1
image: ghcr.io/uniconhq/primitive-probe@sha256:"""
            + b"1" * 64
            + b"""
entrypoint: [/probe]
limits: {time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1}
inputs:
  sentence: {type: text}
  tests: {type: "file[]"}
  flag: {type: boolean}
  code: {type: file}
outputs:
  outcome: {type: outcome}
"""
        }
    )
    workflow = probe(
        "  - id: probe\n    use: unicon/probe@v1\n    with:\n"
        "      sentence: at most ${{inputs.time_limit}}s and ${{ inputs.memory_limit }}MB\n"
        "      tests: ${{ inputs.testcases }}\n      flag: true\n"
        "      code: ${{ inputs.submission }}\n",
        "outputs:\n  outcome: ${{ steps.probe.outcome }}\n",
    )
    [step] = compiled(workflows=workflow, declared=declared)["default"].steps
    assert step.inputs == {
        "sentence": {"value": "at most 2.0s and 256MB"},
        "tests": {
            "task": [
                "data/testcases/1.ans",
                "data/testcases/1.in",
                "data/testcases/2.ans",
                "data/testcases/2.in",
                "data/testcases/10.ans",
                "data/testcases/10.in",
            ]
        },
        "flag": {"value": True},
        "code": {"submission": "submission"},
    }
    assert compiled(workflows=workflow, declared=declared)["default"].tests == ()


def test_an_input_given_by_neither_side_is_refused() -> None:
    missing = parse_task(
        STARTER.replace(b"    - id: memory_limit\n      type: number\n      value: 256\n", b"")
    )
    [(path, message)] = refusals(missing)
    assert path == "inputs.setter"
    assert "memory_limit" in message
    assert "neither" in message
    assert "default" in message


def test_an_input_given_by_both_sides_is_refused_naming_the_stage() -> None:
    both = task(
        b"stages:\n  - id: open\n    inputs:\n      setter:\n"
        b"        - {id: submission, type: code, value: 'print(1)'}\n"
    )
    [(path, message)] = refusals(both)
    assert path == "inputs.contestant"
    assert "submission" in message
    assert "both" in message
    assert "open" in message


def test_an_input_the_workflow_does_not_declare_is_refused_on_either_side() -> None:
    extra_setter = parse_task(
        STARTER.replace(b"  setter:\n", b"  setter:\n    - {id: seed, type: number, value: 1}\n")
    )
    assert refusals(extra_setter) == [
        (
            "inputs.setter",
            "The input seed is not an input of the workflow in stage default (unicon/classic@v1).",
        )
    ]
    extra_contestant = parse_task(
        STARTER.replace(b"  setter:\n", b"    - {id: notes, type: text}\n  setter:\n")
    )
    [(path, message)] = refusals(extra_contestant)
    assert path == "inputs.contestant"
    assert "notes" in message


def test_an_input_of_another_type_than_the_workflow_declares_is_refused() -> None:
    mistyped = parse_task(
        STARTER.replace(b"type: number\n      value: 256", b"type: text\n      value: '256'")
    )
    [(path, message)] = refusals(mistyped)
    assert path == "inputs.setter"
    assert "memory_limit" in message
    assert "number" in message


IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
STEP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
LIMITS = {"time_ms", "cpu_ms", "memory_mb", "pids", "output_mb"}
VALUE_SHAPES = [
    {"value"},
    {"task"},
    {"submission"},
    {"submission", "field"},
    {"step", "output"},
    {"step", "output", "test"},
]


def _value_conforms(value: dict[str, Any], tests: list[str]) -> None:
    assert set(value) in VALUE_SHAPES, value
    if "test" in value:
        assert value["test"] in tests
    if "field" in value:
        assert value["field"] == "language"


def conforms(plan: dict[str, Any]) -> None:
    """The contract's plan shape: every key it names and no other, every
    step one of its three shapes, every value one of its six.
    """
    assert set(plan) == {"schema_version", "harness_image", "stage", "tests", "steps", "verdict"}
    assert plan["schema_version"] == 3
    assert IMAGE.match(plan["harness_image"])
    tests = plan["tests"]
    seen = set()
    for step in plan["steps"]:
        base = {"id", "primitive", "image", "entrypoint", "limits"}
        assert set(step) in (base | {"inputs"}, base | {"inputs", "test"}, base | {"batch"})
        assert STEP_ID.match(step["id"]) and IMAGE.match(step["image"])
        assert "/" not in step["primitive"] and "@" in step["primitive"]
        assert step["entrypoint"] and set(step["limits"]) == LIMITS
        assert (step["id"], step.get("test")) not in seen
        seen.add((step["id"], step.get("test")))
        for item in step.get("batch") or [{"test": step.get("test"), "inputs": step["inputs"]}]:
            for value in item["inputs"].values():
                _value_conforms(value, tests)
    assert set(plan["verdict"]) <= {"outcome", "metrics", "tests", "summary"}
    assert set(plan["verdict"]["outcome"]) == {"step", "output"}


def test_a_compiled_plan_meets_the_contract_and_is_the_same_bytes_every_time() -> None:
    first = compiled()["default"].to_bytes()
    second = compiled(paths=list(reversed(PATHS)))["default"].to_bytes()
    assert first == second
    assert first.endswith(b"}\n")
    loaded = json.loads(first)
    conforms(loaded)
    assert first == (json.dumps(loaded, sort_keys=True, indent=2) + "\n").encode()
    assert Plan.from_bytes(first) == compiled()["default"]
    assert plan_path("default") == "plans/default.json"
    assert "${{" not in first.decode()


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"harness_image": "ghcr.io/uniconhq/harness:latest"},
        {"steps": []},
        {"extra": True},
        {"verdict": {}},
    ],
)
def test_a_plan_outside_the_contract_is_refused_when_read(change: dict[str, Any]) -> None:
    loaded = json.loads(compiled()["default"].to_bytes())
    with pytest.raises(ValueError):
        Plan.from_bytes(json.dumps({**loaded, **change}).encode())


def test_a_step_value_outside_the_six_shapes_is_refused_when_read() -> None:
    loaded = json.loads(compiled()["default"].to_bytes())
    loaded["steps"][0]["inputs"]["source"] = {"submission": "submission", "extra": 1}
    with pytest.raises(ValueError):
        Plan.from_bytes(json.dumps(loaded).encode())


PLAN = compiled()["default"].to_bytes()
BEFORE = Snapshot(
    plans={"plans/default.json": PLAN},
    data={"data/testcases/1.in": "sha256:aa", "data/testcases/1.ans": "sha256:bb"},
    limits=task().limits.as_mapping(),
)


def test_a_first_publication_changes_nothing() -> None:
    assert grading_changes(None, BEFORE) == ()


def test_a_statement_only_save_changes_nothing() -> None:
    same = Snapshot(plans=dict(BEFORE.plans), data=dict(BEFORE.data), limits=dict(BEFORE.limits))
    assert grading_changes(BEFORE, same) == ()


def test_a_changed_plan_is_named() -> None:
    slower = parse_task(STARTER.replace(b"value: 2.0", b"value: 3.0"))
    plan = compiled(slower)["default"].to_bytes()
    after = Snapshot(plans={"plans/default.json": plan}, data=BEFORE.data, limits=BEFORE.limits)
    assert grading_changes(BEFORE, after) == ("plans/default.json changed",)


def test_a_new_stage_is_a_plan_added() -> None:
    after = Snapshot(
        plans={**BEFORE.plans, "plans/test.json": PLAN}, data=BEFORE.data, limits=BEFORE.limits
    )
    assert grading_changes(BEFORE, after) == ("plans/test.json added",)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({**BEFORE.data, "data/testcases/2.in": "sha256:cc"}, ("data/testcases/2.in added",)),
        ({"data/testcases/1.in": "sha256:aa"}, ("data/testcases/1.ans removed",)),
        (
            {**BEFORE.data, "data/testcases/1.in": "sha256:dd"},
            ("data/testcases/1.in changed",),
        ),
    ],
)
def test_a_data_file_added_removed_or_changed_is_named(
    data: dict[str, str], expected: tuple[str, ...]
) -> None:
    after = Snapshot(plans=BEFORE.plans, data=data, limits=BEFORE.limits)
    assert grading_changes(BEFORE, after) == expected


def test_a_changed_limit_is_named() -> None:
    fewer = parse_task(STARTER.replace(b"submissions: 50", b"submissions: 10")).limits
    after = Snapshot(plans=BEFORE.plans, data=BEFORE.data, limits=fewer.as_mapping())
    assert grading_changes(BEFORE, after) == ("limits.submissions changed",)


def test_several_changes_are_named_together() -> None:
    after = Snapshot(
        plans={},
        data={**BEFORE.data, "data/testcases/1.in": "sha256:ee"},
        limits={**BEFORE.limits, "rate": "1 per 60s"},
    )
    assert grading_changes(BEFORE, after) == (
        "plans/default.json removed",
        "data/testcases/1.in changed",
        "limits.rate changed",
    )


def test_only_the_plans_folder_is_reserved() -> None:
    assert is_reserved("plans") and is_reserved("plans/default.json")
    assert not is_reserved("plansx/a") and not is_reserved("data/plans/a")


def test_a_note_reads_back_as_written() -> None:
    changes = ("plans/default.json changed", "data/testcases/1.in added")

    assert read_note(write_note(True, changes)) == Note(True, changes)
    assert read_note(write_note(False, ())) == Note(False, ())
    assert write_note(True, ("limits.rate changed",)) == (
        "grading_changed: true\nchanges:\n- limits.rate changed\n"
    )


@pytest.mark.parametrize("text", [None, "", "[1, 2]", "grading_changed: [", "changes: x"])
def test_a_note_that_does_not_read_says_nothing_changed(text: str | None) -> None:
    assert read_note(text) == Note(False, ())
