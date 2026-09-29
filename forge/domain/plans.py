"""The plan a save compiles for each stage of a task, and what a publication
compares to say whether it changed how the task grades.

A plan is what grading reads (the runner's `plan.schema.json`, version 1):
the harness image by digest, the stage, and a flat list of steps, each a
primitive with its inputs. It is written into the task repo as
`plans/<stage>.json` in the commit a publication tags. This compiler takes
one step per workflow step, in order: `primitive` is the step's `use` as
written, `inputs` its `with` mapping with every `${{ inputs.<id> }}` that
names a setter input replaced by that input's value, and `for_each` its
`foreach`. Every other `${{ }}` is left as written for the harness, and a
step that uses another workflow is carried as it is. Before it compiles, it
checks that every input the workflow declares is given by exactly one side
of the task, with the type the workflow declares.

`HARNESS_IMAGE` is the harness image of runner release v0.1.0, from that
release's `images.json`, and `HARNESS_DIGEST` its digest, which every plan
pins; both move together when the runner cuts a release.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from forge.domain.definitions import ResolvedStage, TaskDefinition
from forge.domain.workflow_definition import WorkflowDefinition
from forge.domain.yaml_models import InvalidDefinition, Model, Problem

HARNESS_IMAGE = (
    "ghcr.io/uniconhq/harness"
    "@sha256:887501fdc2b042692d434904008b5f1f7a6222673dbc5a2c2e31c9c3587c1e0a"
)
HARNESS_DIGEST = HARNESS_IMAGE.partition("@")[2]

SCHEMA_VERSION: Literal[1] = 1
STEP_ID = r"^[a-z0-9][a-z0-9_-]*$"
PRIMITIVE = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"
DIGEST = r"^sha256:[0-9a-f]{64}$"


class PlanStep(Model):
    id: Annotated[str, Field(pattern=STEP_ID)]
    primitive: Annotated[str, Field(pattern=PRIMITIVE)]
    inputs: dict[str, Any]
    for_each: Annotated[str, Field(min_length=1)] | None = None
    limits: dict[str, Any] | None = None


class Plan(Model):
    """One stage's plan, in the shape of the runner's plan schema version 1."""

    schema_version: Literal[1] = SCHEMA_VERSION
    image_digest: Annotated[str, Field(pattern=DIGEST)] = HARNESS_DIGEST
    stage: Annotated[str, Field(min_length=1)]
    steps: tuple[PlanStep, ...] = Field(min_length=1)

    def to_bytes(self) -> bytes:
        """The plan as the file it is committed as: JSON with sorted keys, two
        spaces of indent and a final newline, the same bytes every time.
        """
        document = self.model_dump(mode="json", exclude_none=True)
        return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Plan:
        return cls.model_validate_json(data)


PLANS_FOLDER = "plans/"


def plan_path(stage: str) -> str:
    """Where a stage's plan is committed in the task repo."""
    return f"{PLANS_FOLDER}{stage}.json"


def is_reserved(path: str) -> bool:
    """Whether a path is inside the plans folder, which only the compiler
    writes.
    """
    return path == PLANS_FOLDER.rstrip("/") or path.startswith(PLANS_FOLDER)


_INPUT_REFERENCE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}")


def _substitute(value: object, values: Mapping[str, object]) -> object:
    if isinstance(value, str):
        whole = _INPUT_REFERENCE.fullmatch(value.strip())
        if whole and whole.group(1) in values:
            return values[whole.group(1)]

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            return str(values[name]) if name in values else match.group(0)

        return _INPUT_REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _substitute(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, values) for item in value]
    return value


def _coverage(
    task: TaskDefinition, stage: ResolvedStage, workflow: WorkflowDefinition
) -> list[Problem]:
    declared = workflow.input_types()
    contestant = {entry.id: entry.type for entry in task.inputs.contestant}
    setter = {entry.id: entry.type for entry in stage.setter}
    where = f"in stage {stage.id} ({stage.workflow})"
    problems: list[Problem] = []

    def add(side: str, message: str) -> None:
        problems.append(Problem(path=f"inputs.{side}", message=message))

    for name, kind in declared.items():
        given = [
            (side, types[name])
            for side, types in (("contestant", contestant), ("setter", setter))
            if name in types
        ]
        if not given:
            add("setter", f"The workflow input {name} is given by neither side {where}.")
        elif len(given) > 1:
            add("contestant", f"The workflow input {name} is given by both sides {where}.")
        elif given[0][1] is not kind:
            side, found = given[0]
            add(side, f"The input {name} is {found}, but the workflow declares {kind} {where}.")
    for side, types in (("contestant", contestant), ("setter", setter)):
        for name in types:
            if name not in declared:
                add(side, f"The input {name} is not an input of the workflow {where}.")
    return problems


def _plan(stage: ResolvedStage, workflow: WorkflowDefinition) -> Plan:
    values: dict[str, object] = {entry.id: entry.value for entry in stage.setter}
    steps = tuple(
        PlanStep(
            id=step.id,
            primitive=str(step.use),
            inputs={key: _substitute(item, values) for key, item in step.with_.items()},
            for_each=step.foreach,
        )
        for step in workflow.steps
    )
    return Plan(stage=stage.id, steps=steps)


def compile_plans(
    task: TaskDefinition, workflows: Mapping[str, WorkflowDefinition]
) -> dict[str, Plan]:
    """One plan per stage of `task`, keyed by stage id. `workflows` holds
    every workflow `task.workflow_refs()` names, keyed by the reference as
    text. Raises `InvalidDefinition` when an input the workflow declares is
    given by neither side or by both, when an input the task gives is not
    one the workflow declares, or when the types differ.
    """
    stages = task.stages_resolved()
    problems = [
        problem
        for stage in stages
        for problem in _coverage(task, stage, workflows[str(stage.workflow)])
    ]
    if problems:
        raise InvalidDefinition("task.yaml", problems)
    return {stage.id: _plan(stage, workflows[str(stage.workflow)]) for stage in stages}


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What a publication's grading depends on: its plans by path in the task
    repo, a digest of each data file the plans name by its path, and the
    task's limits by name.
    """

    plans: Mapping[str, bytes]
    data: Mapping[str, str]
    limits: Mapping[str, object]


def _differences(
    before: Mapping[str, object], after: Mapping[str, object], prefix: str = ""
) -> list[str]:
    changes: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            changes.append(f"{prefix}{key} added")
        elif key not in after:
            changes.append(f"{prefix}{key} removed")
        elif before[key] != after[key]:
            changes.append(f"{prefix}{key} changed")
    return changes


def grading_changes(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """What changed how the task grades between the previous publication and
    this one, in words a person reads: `plans/default.json changed`,
    `data/testcases/1.in added`, `limits.submissions changed`. Empty when
    nothing did, and for a first publication, which has nothing before it.
    `grading_changed` is whether this is non-empty.
    """
    if before is None:
        return ()
    return (
        *_differences(before.plans, after.plans),
        *_differences(before.data, after.data),
        *_differences(before.limits, after.limits, "limits."),
    )
