"""A workflow as its `workflow.yaml` defines it (TASK-FORMAT.md section 1.3):
the inputs it declares, which of them the contestant gives, the fields every
test has, the steps it runs in order, once or per test, and the values a run
reports, with what each number means. The file has no name and no version:
the repo is the name and the tag the version. A reference to a workflow or a
primitive is `owner/name@version`, and is the one way a task or a step names
one.

What this module checks needs nothing but the file: ids and names unique and
well formed, each declaration's keys fitting its type, every once step
before every per-test step, and the report's own rules (W1, W3 to W5). What
needs the primitives its steps use, every port, type and reference, is the
compiler's (`forge.domain.plans.check_workflow`).

A value in a step's `with` or in the report may be or hold a reference,
`${{ inputs.<id> }}`, `${{ test.<field> }}` or `${{ steps.<id>.<output> }}`;
`references` finds them in a string.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PlainValidator, model_validator

from forge.domain.errors import InvalidName
from forge.domain.names import validate_name
from forge.domain.types import VALUE_TYPES, Type, declared, retired_type, retired_type_message
from forge.domain.yaml_models import (
    ANY,
    Model,
    Number,
    Problems,
    Retired,
    load_mapping,
    validate,
)

WORKFLOW_FILE = "workflow.yaml"
"""The file at the root of a workflow that defines it."""

VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_MAX = 40
HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
REPORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED_NAMES = ("outcome", "points", "penalty")

_REF_EXAMPLE = "such as unicon/classic@v2"


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


Ref = Annotated[WorkflowRef, PlainValidator(_ref)]
"""A reference written as `owner/name@version`."""

Flag = Annotated[bool, Field(strict=True)]


def _value_type(value: object) -> Type:
    try:
        found = Type(value) if isinstance(value, str) else None
    except ValueError:
        found = None
    if found is None or found not in VALUE_TYPES:
        raise ValueError(f"Must be one of {', '.join(VALUE_TYPES)}.")
    return found


ValueType = Annotated[Type, PlainValidator(_value_type)]


def _options(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise ValueError("Must be a list of at least one option.")
    found: list[str] = []
    for option in value:
        if not isinstance(option, str) or not option.strip() or "\n" in option:
            raise ValueError("Each option is one line of text.")
        if option in found:
            raise ValueError(f"{option!r} is given more than once.")
        found.append(option)
    return tuple(found)


Options = Annotated[tuple[str, ...], PlainValidator(_options)]
"""A non-empty list of distinct one-line options."""


def _handle(value: object) -> str:
    if not isinstance(value, str) or not HANDLE.match(value):
        raise ValueError(
            f"{value!r} must be lower case letters, digits, hyphens and underscores, "
            "starting with a letter or a digit, at most 40 characters."
        )
    return value


class WorkflowInput(Model):
    """One input: its type; `contestant` when the submission fills it;
    `options` on an enum; `per_test` on a contestant file given once per
    test; `optional` on an input a task may leave out.
    """

    type: ValueType
    contestant: Flag = False
    options: Options | None = None
    per_test: Flag = False
    optional: Flag = False

    @model_validator(mode="after")
    def _check(self) -> WorkflowInput:
        problems = Problems()
        if self.type is Type.ENUM and self.options is None:
            problems.add(("options",), "An enum lists its options.")
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), "Applies only to an enum.")
        if self.per_test and not (self.contestant and self.type is Type.FILE):
            problems.add(("per_test",), "Applies only to a contestant's file input.")
        if self.optional and self.contestant:
            problems.add(
                ("optional",), "A contestant's input is never optional; the task gives defaults."
            )
        problems.raise_any()
        return self


class TestField(Model):
    """One field every test has: its type, `options` on an enum, and
    `public` on a file or folder the platform serves to contestants.
    """

    type: ValueType
    options: Options | None = None
    public: Flag = False

    @model_validator(mode="after")
    def _check(self) -> TestField:
        problems = Problems()
        if self.type is Type.ENUM and self.options is None:
            problems.add(("options",), "An enum lists its options.")
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), "Applies only to an enum.")
        if self.public and self.type not in (Type.FILE, Type.FOLDER):
            problems.add(("public",), "Applies only to a file or a folder field.")
        problems.raise_any()
        return self


class WorkflowStep(Model):
    """One step: the primitive it uses, whether it runs once or once per
    test, and what each of the primitive's ports is given.
    """

    id: Annotated[str, PlainValidator(_handle)]
    use: Ref
    per_test: Flag = False
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class Fold(StrEnum):
    SUM = "sum"
    MEAN = "mean"
    MAX = "max"


class Meaning(Model):
    """A reported value with what it means: `from` the step output it reads,
    how it folds over tests, which way is better, and its bounds.
    """

    from_: str = Field(alias="from")
    fold: Fold | None = None
    better: str | None = None
    at_least: Number | None = None
    at_most: Number | None = None


Declared = Annotated[WorkflowInput, BeforeValidator(declared)]
Field_ = Annotated[TestField, BeforeValidator(declared)]


def _report_entry(value: object) -> str | Meaning:
    """A report entry: a reference, or a mapping read as a `Meaning`, so a
    problem inside one is reported at its own YAML path.
    """
    if isinstance(value, dict):
        return Meaning.model_validate(value)
    if not isinstance(value, str):
        raise ValueError(
            "Must be ${{ steps.<id>.<output> }}, or a mapping of from, fold, better, at_least "
            "and at_most."
        )
    return value


_ReportEntry = Annotated[str | Meaning, PlainValidator(_report_entry)]


class WorkflowDefinition(Model):
    """A `workflow.yaml`. `inputs` and `report` default to none; a workflow
    has at least one test field and one step.
    """

    inputs: dict[str, Declared] = Field(default_factory=dict)
    test: dict[str, Field_] = Field(min_length=1)
    steps: tuple[WorkflowStep, ...] = Field(min_length=1)
    report: dict[str, _ReportEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> WorkflowDefinition:
        problems = Problems()
        for key in self.inputs:
            if not HANDLE.match(key):
                problems.add(("inputs", key), _NOT_A_HANDLE)
        for key in self.test:
            if not HANDLE.match(key):
                problems.add(("test", key), _NOT_A_HANDLE)
            elif key == "test":
                problems.add(
                    ("test", key), "No field is named test, since test.yaml would match it."
                )
        problems.duplicates([step.id for step in self.steps], ("steps",))
        seen_per_test = False
        for index, step in enumerate(self.steps):
            if step.per_test:
                seen_per_test = True
            elif seen_per_test:
                problems.add(
                    ("steps", index, "per_test"),
                    "A step that runs once comes before every step that runs per test: it "
                    "can read nothing of them, so move it up.",
                )
        for name, entry in self.report.items():
            for path, message in self._report_problems(name, entry):
                problems.add(("report", name, *path), message)
        problems.raise_any()
        return self

    def _report_problems(
        self, name: str, entry: str | Meaning
    ) -> list[tuple[tuple[str, ...], str]]:
        found: list[tuple[tuple[str, ...], str]] = []
        if not REPORT_NAME.match(name):
            found.append(
                ((), "A reported name is lower case letters, digits and _, starting with a letter.")
            )
        if name in RESERVED_NAMES:
            found.append(
                (
                    (),
                    f"{name} is the platform's own name for what a board ranks; report it as "
                    f"another.",
                )
            )
        if isinstance(entry, Meaning):
            if entry.better is not None and entry.better not in ("higher", "lower"):
                problem = self._better_problem(entry.better)
                if problem is not None:
                    found.append((("better",), problem))
            if (
                entry.at_least is not None
                and entry.at_most is not None
                and entry.at_least > entry.at_most
            ):
                found.append((("at_most",), "Must be at least at_least."))
        return found

    def _better_problem(self, better: str) -> str | None:
        found = whole_reference(better)
        if found is None or found.kind != "inputs":
            return "Must be higher, lower, or ${{ inputs.<id> }} naming a task's enum input."
        declared_input = self.inputs.get(found.name)
        if declared_input is None:
            return f"{found.name} is not an input of the workflow."
        if declared_input.contestant or declared_input.type is not Type.ENUM:
            return f"{found.name} must be an enum input the task gives, not the contestant."
        if not set(declared_input.options or ()) <= {"higher", "lower"}:
            return f"The options of {found.name} must be higher and lower."
        return None


_NOT_A_HANDLE = (
    "Must be lower case letters, digits, hyphens and underscores, starting with a letter "
    "or a digit, at most 40 characters."
)


@dataclass(frozen=True, slots=True)
class Reference:
    """One `${{ ... }}`: `inputs.<id>`, `test.<field>` or
    `steps.<id>.<output>`.
    """

    kind: Literal["inputs", "test", "steps"]
    name: str
    output: str | None = None

    def __str__(self) -> str:
        tail = f".{self.output}" if self.output is not None else ""
        return f"${{{{ {self.kind}.{self.name}{tail} }}}}"


_WHOLE = re.compile(r"^\s*\$\{\{\s*([^{}]*?)\s*\}\}\s*$")
_ANY = re.compile(r"\$\{\{\s*([^{}]*?)\s*\}\}")


class BadReference(ValueError):
    """A `${{ }}` that is not one of the three a workflow makes."""


def parse_reference(expression: str) -> Reference:
    """The reference inside `${{ ... }}`. `BadReference` saying what a
    reference may be.
    """
    parts = expression.split(".")
    match parts:
        case ["inputs", name] if name:
            return Reference("inputs", name)
        case ["test", field] if field:
            return Reference("test", field)
        case ["steps", step, output] if step and output:
            return Reference("steps", step, output)
    raise BadReference(
        f"${{{{ {expression} }}}} is not a reference a workflow makes: inputs.<id>, "
        "test.<field> or steps.<id>.<output>."
    )


def whole_reference(text: str) -> Reference | None:
    """The reference `text` is exactly, or none when it is anything else.
    `BadReference` for a `${{ }}` that is not a reference.
    """
    found = _WHOLE.match(text)
    return parse_reference(found.group(1)) if found is not None else None


def references(text: str) -> list[tuple[re.Match[str], Reference]]:
    """Every reference written into `text`, with where it is. `BadReference`
    for a `${{ }}` that is not one, or a `${{` that is never closed.
    """
    found = [(match, parse_reference(match.group(1))) for match in _ANY.finditer(text)]
    if text.count("${{") != len(found):
        raise BadReference("A ${{ is not closed with }}, or holds a brace of its own.")
    return found


def _list_of_inputs(value: object) -> bool:
    return isinstance(value, list)


RETIRED = (
    Retired(("name",), "A workflow has no name line: its repo is its name. Remove it."),
    Retired(("version",), "A workflow has no version line: its tag is its version. Remove it."),
    Retired(("copied_from",), "A workflow says nothing of where it was copied from. Remove it."),
    Retired(
        ("outputs",),
        "`outputs` is now `report`: a mapping of name to ${{ steps.<id>.<output> }}, or to "
        "{from, fold, better, at_least, at_most}.",
    ),
    Retired(
        ("inputs",),
        "`inputs` is now a mapping of id to declaration, such as `submission: {type: file, "
        "contestant: true}`.",
        _list_of_inputs,
    ),
    Retired(("inputs", ANY), retired_type_message, retired_type),
    Retired(("test", ANY), retired_type_message, retired_type),
    Retired(("steps", ANY, "foreach"), "`foreach` is now `per_test: true`, over the task's tests."),
)


def parse_workflow(text: bytes | str) -> WorkflowDefinition:
    """The `workflow.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(WorkflowDefinition, WORKFLOW_FILE, load_mapping(WORKFLOW_FILE, text), RETIRED)


def starter_workflow(owner: str, name: str) -> dict[str, bytes]:
    """The files a new workflow is created with: a `workflow.yaml` with the
    inputs, test fields, steps and report of `unicon/classic@v2`, so it is
    valid from its first commit and a person starts from a workflow that
    grades and changes what they need.
    """
    workflow = f"""# {owner}/{name}, a workflow. It starts as unicon/classic@v2: compile
# the contestant's file once, run it on every test under the task's
# limits, and compare each run's output with that test's answer. The
# format is TASK-FORMAT.md section 1.3.
inputs:
  submission: {{type: file, contestant: true}}
  language: {{type: enum, options: [c, cpp, java, python], contestant: true}}
  time_limit: number
  memory_limit: number

test:
  input: file
  answer: file

steps:
  - id: compile
    use: unicon/compile@v2
    with:
      source: ${{{{ inputs.submission }}}}
      language: ${{{{ inputs.language }}}}

  - id: run
    use: unicon/sandbox-run@v2
    per_test: true
    with:
      binary: ${{{{ steps.compile.binary }}}}
      input: ${{{{ test.input }}}}
      time_limit: ${{{{ inputs.time_limit }}}}
      memory_limit: ${{{{ inputs.memory_limit }}}}

  - id: check
    use: unicon/diff-check@v2
    per_test: true
    with:
      actual: ${{{{ steps.run.output }}}}
      expected: ${{{{ test.answer }}}}

report:
  time_ms: {{from: "${{{{ steps.run.time_ms }}}}", fold: max, better: lower, at_least: 0}}
  memory_kb: {{from: "${{{{ steps.run.memory_kb }}}}", fold: max, better: lower, at_least: 0}}
  log: ${{{{ steps.compile.compile_log }}}}
"""
    return {WORKFLOW_FILE: workflow.encode()}


__all__ = [
    "RESERVED_NAMES",
    "WORKFLOW_FILE",
    "BadReference",
    "Fold",
    "Meaning",
    "Reference",
    "TestField",
    "WorkflowDefinition",
    "WorkflowInput",
    "WorkflowRef",
    "WorkflowStep",
    "parse_reference",
    "parse_workflow",
    "parse_workflow_ref",
    "references",
    "starter_workflow",
    "whole_reference",
]
