"""A workflow as its `workflow.yaml` defines it (TASK-FORMAT.md section 6.3):
its name, its version, the inputs it declares with their types, the steps it
runs in order and the outputs that fill the verdict. A workflow does not say
which inputs come from the contestant and which from the setter; a task
does. A reference to a workflow or a primitive is `owner/name@version`, and
is the one way a task or a step names one.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, PlainValidator, model_validator

from forge.domain.errors import InvalidName
from forge.domain.names import validate_name
from forge.domain.yaml_models import Handle, Model, Problems, load_mapping, validate


class InputType(StrEnum):
    """The type of one input, the same on the contestant's side and the
    setter's (TASK-FORMAT.md section 2).
    """

    CODE = "code"
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    FILE = "file"
    FILES = "file[]"
    DATASET = "dataset"
    JUPYTER = "jupyter"


WORKFLOW_FILE = "workflow.yaml"
"""The file at the root of a workflow that defines it."""

VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_MAX = 40

_REF_EXAMPLE = "such as unicon/classic@v1"


@dataclass(frozen=True, slots=True)
class WorkflowRef:
    """A workflow or a primitive at one version: `owner/name@version`. The
    owner and the name follow the name rules; the version is a tag, letters,
    digits, dots, hyphens and underscores.
    """

    owner: str
    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}@{self.version}"


def _validate_version(version: str) -> str:
    if not VERSION.match(version) or len(version) > VERSION_MAX:
        raise InvalidName(
            f"{version!r} is not a version: letters, digits, dots, hyphens and underscores, "
            f"at most {VERSION_MAX} characters"
        )
    return version


def _owner_and_name(text: str) -> tuple[str, str]:
    owner, slash, name = text.partition("/")
    if not slash:
        raise InvalidName(f"{text!r} must be an owner and a name, such as unicon/classic")
    return validate_name(owner), validate_name(name)


def parse_workflow_ref(text: str) -> WorkflowRef:
    """`owner/name@version` as a `WorkflowRef`. Raises `InvalidName` naming
    what is wrong.
    """
    head, at, version = text.partition("@")
    if not at or not version:
        raise InvalidName(f"{text!r} must end in @ and a version, {_REF_EXAMPLE}")
    owner, name = _owner_and_name(head)
    return WorkflowRef(owner, name, _validate_version(version))


def _ref(value: object) -> WorkflowRef:
    if isinstance(value, WorkflowRef):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Must be a reference, {_REF_EXAMPLE}.")
    try:
        return parse_workflow_ref(value.strip())
    except InvalidName as error:
        raise ValueError(f"{error.detail}.") from None


def _workflow_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be owner/name, such as unicon/classic.")
    try:
        owner, name = _owner_and_name(value.strip())
    except InvalidName as error:
        raise ValueError(f"{error.detail}.") from None
    return f"{owner}/{name}"


def _version(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be text, such as v1; a number such as 1.0 is written in quotes.")
    try:
        return _validate_version(value.strip())
    except InvalidName as error:
        raise ValueError(f"{error.detail}.") from None


Ref = Annotated[WorkflowRef, PlainValidator(_ref)]
"""A reference written as `owner/name@version`."""

OUTPUT_SLOTS = ("outcome", "metrics", "tests", "summary")


class WorkflowInput(Model):
    id: Handle
    type: InputType


class WorkflowStep(Model):
    """One step: the primitive or workflow it uses, what it is given, and the
    list it runs once per item of when `foreach` is set.
    """

    id: Handle
    use: Ref
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    foreach: str | None = Field(default=None, min_length=1)


class WorkflowDefinition(Model):
    """A `workflow.yaml`. `name` is `owner/name`; `copied_from` is set on a
    copy of another workflow and names the source at the version copied.
    """

    name: Annotated[str, PlainValidator(_workflow_name)]
    version: Annotated[str, PlainValidator(_version)]
    copied_from: Ref | None = None
    inputs: tuple[WorkflowInput, ...] = ()
    steps: tuple[WorkflowStep, ...] = Field(min_length=1)
    outputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> WorkflowDefinition:
        problems = Problems()
        problems.duplicates([entry.id for entry in self.inputs], ("inputs",))
        problems.duplicates([step.id for step in self.steps], ("steps",))
        slots = ", ".join(OUTPUT_SLOTS)
        for key in self.outputs:
            if key not in OUTPUT_SLOTS:
                problems.add(("outputs", key), f"An output fills one of {slots}.")
        problems.raise_any()
        return self

    @property
    def owner(self) -> str:
        return self.name.partition("/")[0]

    @property
    def ref(self) -> WorkflowRef:
        owner, _, name = self.name.partition("/")
        return WorkflowRef(owner, name, self.version)

    def input_types(self) -> dict[str, InputType]:
        return {entry.id: entry.type for entry in self.inputs}


def parse_workflow(text: bytes | str) -> WorkflowDefinition:
    """The `workflow.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(WorkflowDefinition, WORKFLOW_FILE, load_mapping(WORKFLOW_FILE, text))


def starter_workflow(owner: str, name: str) -> dict[str, bytes]:
    """The files a new workflow is created with: a `workflow.yaml` named
    `owner/name` at version `v1`, with the inputs, steps and outputs of
    `unicon/classic@v1`, so it is valid from its first commit and a person
    starts from a workflow that grades and changes what they need.
    """
    workflow = f"""# {owner}/{name}, a workflow. It starts as the steps of unicon/classic@v1:
# compile the submission once, run the binary on every testcase under the
# task's limits, and diff each run's output against that testcase's
# answer. The format is TASK-FORMAT.md section 6.3.
name: {owner}/{name}
version: v1

inputs:
  - id: submission
    type: code
  - id: testcases
    type: file[]
  - id: time_limit
    type: number
  - id: memory_limit
    type: number

steps:
  - id: compile
    use: unicon/compile@v1
    with:
      source: ${{{{ inputs.submission }}}}
      language: ${{{{ inputs.submission.language }}}}

  - id: run
    use: unicon/sandbox-run@v1
    foreach: ${{{{ inputs.testcases }}}}
    with:
      binary: ${{{{ steps.compile.binary }}}}
      input: ${{{{ item.input }}}}
      time_limit: ${{{{ inputs.time_limit }}}}
      memory_limit: ${{{{ inputs.memory_limit }}}}

  - id: check
    use: unicon/diff-check@v1
    foreach: ${{{{ inputs.testcases }}}}
    with:
      actual: ${{{{ steps.run.output }}}}
      expected: ${{{{ item.answer }}}}

outputs:
  outcome: ${{{{ steps.check.outcome }}}}
  metrics:
    points: ${{{{ steps.check.points }}}}
  tests:
    time_ms: ${{{{ steps.run.time_ms }}}}
    memory_kb: ${{{{ steps.run.memory_kb }}}}
  summary: ${{{{ steps.compile.compile_log }}}}
"""
    return {WORKFLOW_FILE: workflow.encode()}
