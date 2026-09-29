"""The compiler: one plan per stage in the runner's plan shape, setter values
put in place, every workflow input given by exactly one side, the same
bytes every time, only the compiler writes inside `plans/`, and what a
publication names as changing how it grades, in the note it carries.
The schema a compiled plan is held to below is the runner's
`plan.schema.json` at release v0.1.0, written out by hand.
"""

import json
import re
from typing import Any

import pytest

from forge.domain.definitions import TaskDefinition, parse_task, starter_task
from forge.domain.plans import (
    HARNESS_DIGEST,
    HARNESS_IMAGE,
    Plan,
    Snapshot,
    compile_plans,
    grading_changes,
    is_reserved,
    plan_path,
)
from forge.domain.publications import Note, read_note, write_note
from forge.domain.workflow_definition import WorkflowDefinition, parse_workflow
from forge.domain.yaml_models import InvalidDefinition
from forge.testing import CLASSIC

STARTER = starter_task("Sum")["task.yaml"]


def classic() -> dict[str, WorkflowDefinition]:
    return {"unicon/classic@v1": parse_workflow(CLASSIC)}


def task(extra: bytes = b"") -> TaskDefinition:
    return parse_task(STARTER + extra)


def test_the_starter_task_compiles_to_one_default_plan_over_classic() -> None:
    starter = task()
    assert [str(ref) for ref, _ in starter.workflow_refs()] == ["unicon/classic@v1"]
    plans = compile_plans(starter, classic())
    assert list(plans) == ["default"]
    plan = plans["default"]
    assert (plan.schema_version, plan.image_digest, plan.stage) == (1, HARNESS_DIGEST, "default")
    assert [(step.id, step.primitive) for step in plan.steps] == [
        ("compile", "unicon/compile@v1"),
        ("run", "unicon/sandbox-run@v1"),
        ("check", "unicon/diff-check@v1"),
    ]
    compile_, run, check = plan.steps
    assert compile_.inputs == {"source": "${{ inputs.submission }}"}
    assert compile_.for_each is None
    assert run.inputs == {
        "binary": "${{ steps.compile.output }}",
        "input": "${{ item.input }}",
        "time_limit": 2.0,
        "memory_limit": 256,
    }
    assert run.for_each == check.for_each == "${{ inputs.testcases }}"
    assert check.inputs == {"actual": "${{ steps.run.stdout }}", "expected": "${{ item.answer }}"}
    assert all(step.limits is None for step in plan.steps)


def test_the_harness_digest_is_the_harness_images() -> None:
    assert HARNESS_IMAGE.startswith("ghcr.io/uniconhq/harness@")
    assert HARNESS_IMAGE.endswith(HARNESS_DIGEST)


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


def test_a_stage_overrides_a_setter_input_and_the_workflow() -> None:
    staged = task(STAGES)
    assert [(str(ref), at) for ref, at in staged.workflow_refs()] == [
        ("unicon/classic@v1", "workflow"),
        ("unicon/classic@v2", "stages[1].workflow"),
    ]
    v2 = parse_workflow(CLASSIC.replace(b"version: v1", b"version: v2"))
    plans = compile_plans(staged, {**classic(), "unicon/classic@v2": v2})
    assert list(plans) == ["validation", "test"]
    assert plans["validation"].steps[1].inputs["time_limit"] == 2.0
    assert plans["test"].steps[1].inputs["time_limit"] == 5.0
    assert plans["test"].stage == "test"


def test_every_reference_to_a_setter_input_takes_its_value() -> None:
    workflow = parse_workflow(
        b"""\
name: acme/probe
version: v1
inputs:
  - {id: submission, type: code}
  - {id: testcases, type: "file[]"}
  - {id: time_limit, type: number}
  - {id: memory_limit, type: number}
steps:
  - id: probe
    use: acme/probe@v1
    with:
      tests: ${{ inputs.testcases }}
      spaced: "${{inputs.time_limit}}"
      sentence: "at most ${{ inputs.time_limit }}s and ${{ inputs.memory_limit }}MB"
      nested:
        - limit: ${{ inputs.memory_limit }}
        - ${{ inputs.submission }}
      unknown: ${{ inputs.nothing }}
      literal: 7
"""
    )
    [plan] = compile_plans(task(), {"unicon/classic@v1": workflow}).values()
    assert plan.steps[0].inputs == {
        "tests": "data/testcases/",
        "spaced": 2.0,
        "sentence": "at most 2.0s and 256MB",
        "nested": [{"limit": 256}, "${{ inputs.submission }}"],
        "unknown": "${{ inputs.nothing }}",
        "literal": 7,
    }


def refusals(staged: TaskDefinition) -> list[tuple[str, str]]:
    with pytest.raises(InvalidDefinition) as error:
        compile_plans(staged, classic())
    return [(problem["path"], problem["message"]) for problem in error.value.errors]


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


REQUIRED = {"schema_version", "image_digest", "stage", "steps"}
ALLOWED = REQUIRED
STEP_REQUIRED = {"id", "primitive", "inputs"}
STEP_ALLOWED = STEP_REQUIRED | {"for_each", "limits"}
STEP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PRIMITIVE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"
)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def conforms(document: dict[str, Any]) -> None:
    assert REQUIRED <= set(document) <= ALLOWED
    assert document["schema_version"] == 1
    assert DIGEST.match(document["image_digest"])
    assert isinstance(document["stage"], str) and document["stage"]
    assert isinstance(document["steps"], list) and document["steps"]
    for step in document["steps"]:
        assert STEP_REQUIRED <= set(step) <= STEP_ALLOWED
        assert STEP_ID.match(step["id"])
        assert PRIMITIVE.match(step["primitive"])
        assert isinstance(step["inputs"], dict)
        if "for_each" in step:
            assert isinstance(step["for_each"], str) and step["for_each"]
    assert len({step["id"] for step in document["steps"]}) == len(document["steps"])


def test_a_compiled_plan_meets_the_schema_and_is_the_same_bytes_every_time() -> None:
    first = compile_plans(task(), classic())["default"].to_bytes()
    second = compile_plans(task(), classic())["default"].to_bytes()
    assert first == second
    assert first.endswith(b"}\n")
    document = json.loads(first)
    conforms(document)
    assert first == (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    assert Plan.from_bytes(first) == compile_plans(task(), classic())["default"]
    assert plan_path("default") == "plans/default.json"


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"image_digest": "latest"},
        {"steps": []},
        {"extra": True},
    ],
)
def test_a_plan_outside_the_schema_is_refused_when_read(change: dict[str, Any]) -> None:
    document = json.loads(compile_plans(task(), classic())["default"].to_bytes())
    with pytest.raises(ValueError):
        Plan.from_bytes(json.dumps({**document, **change}).encode())


PLAN = compile_plans(task(), classic())["default"].to_bytes()
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
    plan = compile_plans(slower, classic())["default"].to_bytes()
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
