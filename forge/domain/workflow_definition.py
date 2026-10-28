"""A workflow as its `workflow.yaml` defines it (SIMPLER-CORE.md 7,
`RunWorkflow`): the inputs one run takes, the steps it runs in order, and
the outputs it gives beside the verdict every run has. It describes one
run: the task says who gives each input and what repeats. Its `outputs`
add only a title and a unit, since what a number means is the primitive's
or the program's that made it. A number or a text output is a column of
the run; a file output is not. The file has no name and no version: the
repo is the name and the tag the version. A reference to a workflow or a
primitive is `owner/name@version`, and is the one way a task or a step
names one.

What this module checks needs nothing but the file: ids and names unique and
well formed, and each declaration's keys fitting its type. What needs the
primitives its steps use, every port, type and reference, is the run
workflow's check (`forge.domain.run_workflow`).

A value in a step's `with` or in the outputs may be or hold a reference,
`${{ inputs.<id> }}`, `${{ steps.<id>.<output> }}`,
`${{ steps.<id>.<program>.<output> }}` for one program of a programs-form
step, or `${{ workflow.<path> }}`, one of the workflow's own files, a
trailing `/` naming a folder; `references` finds them in a string.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PlainValidator, field_validator, model_validator

from forge.domain.content import path_problem
from forge.domain.errors import InvalidName
from forge.domain.names import validate_name
from forge.domain.types import VALUE_TYPES, Type, declared, retired_type, retired_type_message
from forge.domain.yaml_models import (
    ANY,
    Line,
    Model,
    Problems,
    Retired,
    already_listed,
    fail,
    load_mapping,
    name_rule,
    validate,
)

WORKFLOW_FILE = "workflow.yaml"
"""The file at the root of a workflow that defines it."""

VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_MAX = 40
NEW_VERSION = re.compile(r"^v([1-9][0-9]*)$")
"""The name a version is made under: `v` and a whole number from 1, with no
leading zero. A tag already at the forge under another name is still read
by the wider `VERSION`, and is never newer than a numbered one."""
HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
REPORT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""A report's or a run input's name, a column of the run, and a report's in a
`program.yaml`, as `exec` takes it on either form: one rule for both."""
RESERVED_NAMES = ("outcome", "points", "penalty")
RUN_NAMES = ("verdict", "passed")
"""What Unicon gives every run itself: its verdict, and `passed`, 1 when the
verdict is `accepted`, else 0 (SIMPLER-CORE.md 7.3). A workflow names no
input or output with one, nor with one of `RESERVED_NAMES`."""
MAX_USES = 32
"""The most distinct `use:` references one definition holds; each costs a
read at every check."""

_REF_EXAMPLE = "such as unicon/classic@v3"


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
            f"{version!r} cannot be used as a version. A version is at most {VERSION_MAX} "
            "letters, digits, dots, hyphens and underscores."
        )
    return version


def validate_version(version: str) -> str:
    """`version` when a version may be made under it, `v` and a whole number
    from 1; `InvalidName` saying why not.
    """
    if not NEW_VERSION.match(version) or len(version) > VERSION_MAX:
        raise InvalidName(
            f"{version!r} cannot be used as a version. Name a version v and a whole number "
            "from 1, with no leading zero, such as v1, v2 or v10."
        )
    return version


def version_number(version: str) -> int | None:
    """The number of a version named `v` and a whole number, or none for a
    tag under any other name.
    """
    found = NEW_VERSION.match(version)
    return int(found.group(1)) if found is not None else None


def _owner_and_name(text: str) -> tuple[str, str]:
    owner, slash, name = text.partition("/")
    if not slash:
        raise InvalidName(f"{text!r} must give an owner and a name, such as unicon/classic.")
    return validate_name(owner), validate_name(name)


def parse_workflow_ref(text: str) -> WorkflowRef:
    """`owner/name@version` as a `WorkflowRef`. Raises `InvalidName` naming
    what is wrong.
    """
    head, at, version = text.partition("@")
    if not at or not version:
        raise InvalidName(f"{text!r} must end in @ and a version, {_REF_EXAMPLE}.")
    owner, name = _owner_and_name(head)
    return WorkflowRef(owner, name, _validate_version(version))


def _ref(value: object) -> WorkflowRef:
    if isinstance(value, WorkflowRef):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Give an owner, a name and a version, {_REF_EXAMPLE}.")
    try:
        return parse_workflow_ref(value.strip())
    except InvalidName as error:
        raise ValueError(error.detail) from None


Ref = Annotated[WorkflowRef, PlainValidator(_ref)]
"""A reference written as `owner/name@version`."""

Flag = Annotated[bool, Field(strict=True)]


def _value_type(value: object) -> Type:
    try:
        found = Type(value) if isinstance(value, str) else None
    except ValueError:
        found = None
    if found is None or found not in VALUE_TYPES:
        raise ValueError(f"This must be one of {', '.join(VALUE_TYPES)}.")
    return found


ValueType = Annotated[Type, PlainValidator(_value_type)]


def _options(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise ValueError("Give at least one option.")
    found: list[str] = []
    for option in value:
        if not isinstance(option, str) or not option.strip() or "\n" in option:
            raise ValueError("Each option must be one line of text.")
        if option in found:
            raise ValueError(already_listed(option))
        found.append(option)
    return tuple(found)


Options = Annotated[tuple[str, ...], PlainValidator(_options)]
"""A non-empty list of distinct one-line options."""


def _handle(value: object) -> str:
    if not isinstance(value, str) or not HANDLE.match(value):
        raise ValueError(name_rule(value, 40))
    return value


_LIST_OPTIONS = "List the options to choose from."
_ONLY_ENUM = "Only an enum has options. Remove them."


def reserved_message(name: str) -> str:
    """Why a value cannot be called `name`, one of `RESERVED_NAMES` or
    `RUN_NAMES`.
    """
    if name in RUN_NAMES:
        return (
            f"{name} is a name Unicon uses itself, for what it gives every run. Report this "
            "value under another name."
        )
    return (
        f"{name} is a name Unicon uses itself, for what a leaderboard ranks. Report this "
        "value under another name."
    )


@dataclass(frozen=True, slots=True)
class Reference:
    """One `${{ ... }}`: `inputs.<id>`, `steps.<id>.<output>`, or
    `workflow.<path>`, the path its `name`. A programs-form step's output
    for one program, `steps.<id>.<program>.<output>`, names that `program`
    too.
    """

    kind: Literal["inputs", "steps", "workflow"]
    name: str
    output: str | None = None
    program: str | None = None
    # 0 reads the outer workflow. 1 onwards reads the inner workflow that pin was given.
    pin: int = 0

    @property
    def port(self) -> str | None:
        """The output's name as the step gives it: `<program>.<output>` for
        one program's output, the output's own name otherwise.
        """
        if self.program is not None:
            return f"{self.program}.{self.output}"
        return self.output

    def __str__(self) -> str:
        tail = f".{self.port}" if self.port is not None else ""
        kind = f"workflow@{self.pin}" if self.kind == "workflow" and self.pin else self.kind
        return f"${{{{ {kind}.{self.name}{tail} }}}}"


_WHOLE = re.compile(r"^\s*\$\{\{\s*([^{}]*?)\s*\}\}\s*$")
_ANY = re.compile(r"\$\{\{\s*([^{}]*?)\s*\}\}")


class BadReference(ValueError):
    """A `${{ }}` that is not one of the references a workflow makes."""


def _step_id(step: str, *, flat: bool) -> bool:
    """A written reference names a step by its id. A flat workflow's step id
    is a path, so only that reading checks `FLAT_STEP_ID`. Imported here, not
    at load, because `plan_basics` is built on this module.
    """
    if not flat:
        return True
    from forge.domain.plan_basics import FLAT_STEP_ID

    return re.fullmatch(FLAT_STEP_ID, step) is not None


def parse_reference(expression: str, *, flat: bool = False) -> Reference:
    """The reference inside `${{ ... }}`. `flat` reads a workflow expanded
    into primitive steps: a step id may be a path, and
    `workflow@<k>.<path>` names pin k. `BadReference` saying what a
    reference may be.
    """
    head, dot, path = expression.partition(".")
    if flat and head.startswith("workflow@") and dot:
        number = head.removeprefix("workflow@")
        if number.isdigit() and int(number) >= 1 and path:
            problem = path_problem(path, folder=None, of="workflow")
            if problem is not None:
                raise BadReference(f"Unicon cannot read ${{{{ {expression} }}}}. {problem}")
            return Reference("workflow", path, pin=int(number))
    if head == "workflow" and dot:
        problem = path_problem(path, folder=None, of="workflow")
        if problem is not None:
            raise BadReference(f"Unicon cannot read ${{{{ {expression} }}}}. {problem}")
        return Reference("workflow", path)
    parts = expression.split(".")
    match parts:
        case ["inputs", name] if name:
            return Reference("inputs", name)
        case ["steps", step, output] if step and output and _step_id(step, flat=flat):
            return Reference("steps", step, output)
        case ["steps", step, program, output] if (
            step and program and output and _step_id(step, flat=flat)
        ):
            return Reference("steps", step, output, program)
    raise BadReference(
        f"Unicon cannot read ${{{{ {expression} }}}}. Write ${{{{ inputs.<id> }}}}, "
        "${{ steps.<id>.<output> }}, ${{ steps.<id>.<program>.<output> }} or "
        "${{ workflow.<path> }}."
    )


def whole_reference(text: str, *, flat: bool = False) -> Reference | None:
    """The reference `text` is exactly, or none when it is anything else.
    `BadReference` for a `${{ }}` that is not a reference.
    """
    found = _WHOLE.match(text)
    return parse_reference(found.group(1), flat=flat) if found is not None else None


def references(text: str, *, flat: bool = False) -> list[tuple[re.Match[str], Reference]]:
    """Every reference written into `text`, with where it is. `BadReference`
    for a `${{ }}` that is not one, or a `${{` that is never closed.
    """
    found = [(match, parse_reference(match.group(1), flat=flat)) for match in _ANY.finditer(text)]
    if text.count("${{") != len(found):
        raise BadReference(
            "A ${{ is not closed with }}, or has a brace inside it. Close each one, and "
            "remove any brace inside."
        )
    return found


def _list_of_inputs(value: object) -> bool:
    return isinstance(value, list)


_FILE_KEYS = (
    Retired(("name",), "A workflow's name is the name it was made under. Remove this."),
    Retired(("version",), "A workflow's version is set when the version is made. Remove this."),
    Retired(("copied_from",), "A workflow does not record where it was copied from. Remove this."),
)
_OLD_INPUTS = (
    Retired(
        ("inputs",),
        "Inputs are now listed by name, each with its type, such as `code: {type: folder}`.",
        _list_of_inputs,
    ),
    Retired(("inputs", ANY), retired_type_message, retired_type),
)


class RunInput(Model):
    """One input of a workflow (SIMPLER-CORE.md 7.1): its type, `options`
    on an enum, `optional` on an input a task may leave out, and the task
    form's `title`, `help` and `unit`. Who gives it, and whether it repeats,
    are the task's.
    """

    type: ValueType
    options: Options | None = None
    optional: Flag = False
    title: Line | None = None
    help: Line | None = None
    unit: Line | None = None

    @model_validator(mode="after")
    def _check(self) -> RunInput:
        problems = Problems()
        if self.type is Type.ENUM and self.options is None:
            problems.add(("options",), _LIST_OPTIONS)
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), _ONLY_ENUM)
        problems.raise_any()
        return self


class RunStep(Model):
    """One step of a workflow: the primitive it uses and what each of
    its ports is given. A `named_files` port takes a mapping of names to
    files, a `programs` port a mapping of program names to each program's
    ports, and a `links` port a list of `"<from> -> <to>"`.
    """

    id: Annotated[str, PlainValidator(_handle)]
    use: Ref
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class RunOutput(Model):
    """An output with the words a page names it by: `from` the step output it
    reads, its `title`, and its `unit`, on a number only. Which way is
    better and its bounds are the primitive's or the program's that made
    the number, never the output's.
    """

    from_: str = Field(alias="from")
    title: Line | None = None
    unit: Line | None = None


RunDeclared = Annotated[RunInput, BeforeValidator(declared)]


def _run_output(value: object) -> str | RunOutput:
    if isinstance(value, dict):
        return RunOutput.model_validate(value)
    if not isinstance(value, str):
        raise ValueError(
            "Give the step output this gives, as ${{ steps.<id>.<output> }}, or that "
            "output with the words a page names it by: from, title and unit."
        )
    return value


_RunOutputEntry = Annotated[str | RunOutput, PlainValidator(_run_output)]

CLONE_STEP = "clone"
PIPELINE_STEPS = (CLONE_STEP, "report")
"""The names of the steps Unicon adds to a grading's pipelines beside the
workflow's own, which a run workflow's step ids may not take, since a step
of the plan is named by its id: `clone`, which checks the submission out,
and `report`, which tells the forge the pipeline has ended."""
SELF_REPORTED = "self_reported"
"""The port a primitive is told on that the program it runs given a manifest
runs the entrant's code, which Unicon sets from the task, never a workflow."""
SELF_REPORTED_GIVEN = (
    "Unicon gives this itself, from which inputs the task makes the entrant's, once the "
    "organiser confirms the reports as self-reported. Remove it."
)


class RunWorkflow(Model):
    """A `workflow.yaml`: one run's inputs, its steps, at
    least one, and the outputs it gives beside the `verdict` and `passed`
    every run has. Output names and input names are one set of columns. A
    number or a text output is a column; a file output is not, and a box
    may still wire it on.
    """

    inputs: dict[str, RunDeclared] = Field(default_factory=dict)
    steps: tuple[RunStep, ...]
    outputs: dict[str, _RunOutputEntry] = Field(default_factory=dict)

    @field_validator("steps")
    @classmethod
    def _steps(cls, steps: tuple[RunStep, ...]) -> tuple[RunStep, ...]:
        if not steps:
            fail([((), "A workflow needs at least one step. Add one from the tray.")])
        return steps

    @model_validator(mode="after")
    def _check(self) -> RunWorkflow:
        problems = Problems()
        for key in self.inputs:
            if not REPORT_NAME.fullmatch(key):
                problems.add(
                    ("inputs", key),
                    "An input's name may use at most 64 lower case letters, digits and "
                    "underscores (_), starting with a letter, since a formula names it as a "
                    "column of the run.",
                )
            elif key in RESERVED_NAMES or key in RUN_NAMES:
                problems.add(("inputs", key), reserved_message(key))
        problems.duplicates([step.id for step in self.steps], ("steps",))
        if len({str(step.use) for step in self.steps}) > MAX_USES:
            problems.add(
                ("steps",),
                f"A workflow can use at most {MAX_USES} different primitive versions. Use fewer.",
            )
        for index, step in enumerate(self.steps):
            if step.id in PIPELINE_STEPS:
                problems.add(
                    ("steps", index, "id"),
                    f"{step.id} is the name of a step Unicon adds to every grading beside the "
                    "workflow's own, so a workflow step cannot take it. Choose another id.",
                )
            if SELF_REPORTED in step.with_:
                problems.add(("steps", index, "with", SELF_REPORTED), SELF_REPORTED_GIVEN)
        for name in self.outputs:
            at = ("outputs", name)
            if not REPORT_NAME.fullmatch(name):
                problems.add(
                    at,
                    "An output's name may use at most 64 lower case letters, digits and "
                    "underscores (_), starting with a letter.",
                )
            elif name in RESERVED_NAMES or name in RUN_NAMES:
                problems.add(at, reserved_message(name))
            elif name in self.inputs:
                problems.add(
                    at,
                    f"{name} is an input of the workflow, and an output and an input are columns "
                    "of the same run. Give this under another name.",
                )
        problems.raise_any()
        return self


_MEANT_BY_MAKER = (
    "What a number means comes from the primitive.yaml or program.yaml that made it, and an "
    "output adds only a title and a unit. Remove this."
)
_RENAME_OUTPUTS = (
    "A workflow names what it gives outputs, and an output gives only from, title and unit. "
    "Rename it outputs."
)
RUN_RETIRED = (
    *_FILE_KEYS,
    Retired(("report",), _RENAME_OUTPUTS),
    Retired(("reports",), _RENAME_OUTPUTS),
    *_OLD_INPUTS,
    Retired(
        ("test",),
        "A workflow describes one run, and the task repeats it over its tests with repeat. "
        "Make each test field an input, and remove this.",
    ),
    Retired(
        ("inputs", ANY, "contestant"),
        "The task decides who gives an input, in its own inputs. Remove this.",
    ),
    Retired(
        ("inputs", ANY, "per_test"),
        "A workflow describes one run, and the task says which inputs repeat. Remove this.",
    ),
    Retired(
        ("steps", ANY, "per_test"),
        "A workflow describes one run, so every step runs once in it, and the task says what "
        "repeats. Remove this.",
    ),
    Retired(("steps", ANY, "foreach"), "The task says what repeats. Remove this."),
    Retired(
        ("outputs", ANY, "fold"),
        "How runs add up is the task's scoring, not the workflow's. Remove this.",
    ),
    Retired(("outputs", ANY, "better"), _MEANT_BY_MAKER),
    Retired(("outputs", ANY, "at_least"), _MEANT_BY_MAKER),
    Retired(("outputs", ANY, "at_most"), _MEANT_BY_MAKER),
)


def parse_run_workflow(text: bytes | str) -> RunWorkflow:
    """The workflow in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(RunWorkflow, WORKFLOW_FILE, load_mapping(WORKFLOW_FILE, text), RUN_RETIRED)


CLASSIC = Path(__file__).parent / "classic"
"""A copy of `unicon/classic@v3`'s files, as deploy seeds them."""


def classic_files() -> dict[str, bytes]:
    """`unicon/classic@v3`'s files, by their paths in the workflow."""
    return {
        path.relative_to(CLASSIC).as_posix(): path.read_bytes()
        for path in sorted(CLASSIC.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def empty_workflow(owner: str, name: str) -> dict[str, bytes]:
    """The empty draft a new workflow starts with; steps are added before versioning."""
    header = f"# {owner}/{name}, a workflow. The format is SIMPLER-CORE.md 7.\n"
    return {WORKFLOW_FILE: (header + "steps: []\n").encode()}


__all__ = [
    "RESERVED_NAMES",
    "RUN_NAMES",
    "SELF_REPORTED",
    "SELF_REPORTED_GIVEN",
    "WORKFLOW_FILE",
    "BadReference",
    "Reference",
    "RunInput",
    "RunOutput",
    "RunStep",
    "RunWorkflow",
    "WorkflowRef",
    "classic_files",
    "empty_workflow",
    "parse_reference",
    "parse_run_workflow",
    "parse_workflow_ref",
    "references",
    "reserved_message",
    "validate_version",
    "version_number",
    "whole_reference",
]
