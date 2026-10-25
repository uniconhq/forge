"""A workflow as its `workflow.yaml` defines it (TASK-FORMAT.md section 1.3):
the inputs it declares, which of them the contestant gives, the fields every
test has, the steps it runs in order, once or per test, and the values a run
reports, with what each number means. The file has no name and no version:
the repo is the name and the tag the version. A reference to a workflow or a
primitive is `owner/name@version`, and is the one way a task or a step names
one.

A file with no `test` block is a run workflow instead (SIMPLER-CORE.md 7,
`RunWorkflow`): it describes one run, so it has no test fields and nothing
runs per test, no input says who gives it, and its `reports` add only a
title and a unit, since what a number means is the primitive's or the
program's that made it. It reads its own files, `${{ workflow.<path> }}`, a
trailing `/` naming a folder, and an `interact` step's outputs per program,
`${{ steps.<id>.<program>.<output> }}`. Testing for `test` and not for
`reports` tells the two apart, since a run workflow that reports only its
verdict has no `reports` either. `read_workflow` reads either.

What this module checks needs nothing but the file: ids and names unique and
well formed, each declaration's keys fitting its type, every once step
before every per-test step, and the report's own rules (W1, W3 to W5). What
needs the primitives its steps use, every port, type and reference, is the
compiler's (`forge.domain.plans.check_workflow`).

A value in a step's `with` or in the report may be or hold a reference,
`${{ inputs.<id> }}`, `${{ test.<field> }}`, `${{ steps.<id>.<output> }}`,
or in a run workflow `${{ workflow.<path> }}` and
`${{ steps.<id>.<program>.<output> }}`; `references` finds them in a string.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PlainValidator, model_validator

from forge.domain.errors import InvalidName
from forge.domain.names import validate_name
from forge.domain.types import VALUE_TYPES, Type, declared, retired_type, retired_type_message
from forge.domain.yaml_models import (
    ANY,
    Line,
    Model,
    Number,
    Problems,
    Retired,
    already_listed,
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
REPORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED_NAMES = ("outcome", "points", "penalty")
RUN_NAMES = ("verdict", "passed")
"""What Unicon gives every run itself: its verdict, and `passed`, 1 when the
verdict is `accepted`, else 0 (SIMPLER-CORE.md 7.3). A run workflow names no
input or report with one, nor with one of `RESERVED_NAMES`; today's format
leaves them free."""
MAX_USES = 32
"""The most distinct `use:` references one definition holds; each costs a
read at every check."""

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


class WorkflowInput(Model):
    """One input: its type; `contestant` when the submission fills it;
    `options` on an enum; `per_test` on a contestant file given once per
    test; `optional` on an input a task may leave out; and, for the task's
    form only, `title`, `help` and `unit`, which name it to the organiser
    ("Time limit", "seconds") and change no plan. Only a `number` takes a
    `unit`, since the form reads it after the title ("Time limit
    (seconds)"): making or checking a version refuses it on any other type,
    naming the input (`plans.stray_units`), and a version made before that
    reads with it dropped (`shown_unit`), since a version cannot be mended
    and a unit changes no plan.
    """

    type: ValueType
    contestant: Flag = False
    options: Options | None = None
    per_test: Flag = False
    optional: Flag = False
    title: Line | None = None
    help: Line | None = None
    unit: Line | None = None

    @property
    def shown_unit(self) -> str | None:
        """Its unit, on a number only: a stray one on another type, which an
        older version may hold, is dropped.
        """
        return self.unit if self.type is Type.NUMBER else None

    @model_validator(mode="after")
    def _check(self) -> WorkflowInput:
        problems = Problems()
        if self.type is Type.ENUM and self.options is None:
            problems.add(("options",), _LIST_OPTIONS)
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), _ONLY_ENUM)
        if self.per_test and not (self.contestant and self.type is Type.FILE):
            problems.add(("per_test",), "This applies only to a file the entrant gives.")
        if self.optional and self.contestant:
            problems.add(
                ("optional",),
                "An input the entrant gives cannot be optional. The task can give it a "
                "default instead.",
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
            problems.add(("options",), _LIST_OPTIONS)
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), _ONLY_ENUM)
        if self.public and self.type not in (Type.FILE, Type.FOLDER):
            problems.add(("public",), "Only a file or a folder can be public.")
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
    the `title` and `unit` a page names it by ("Run time", "ms"), how it
    folds over tests, which way is better, and its bounds. The title and
    the unit change no plan. Only a number takes a unit: making or checking
    a version refuses one on text, naming the entry
    (`plans.stray_report_units`), and a version made before reads with it
    dropped.
    """

    from_: str = Field(alias="from")
    title: Line | None = None
    unit: Line | None = None
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
            "Give the step output this reports, as ${{ steps.<id>.<output> }}, or that "
            "output with what it means: from, title, unit, fold, better, at_least and at_most."
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
                    ("test", key), "A test field cannot be called test. Choose another name."
                )
        problems.duplicates([step.id for step in self.steps], ("steps",))
        if len({str(step.use) for step in self.steps}) > MAX_USES:
            problems.add(
                ("steps",),
                f"A workflow can use at most {MAX_USES} different primitive versions. Use fewer.",
            )
        seen_per_test = False
        for index, step in enumerate(self.steps):
            if step.per_test:
                seen_per_test = True
            elif seen_per_test:
                problems.add(
                    ("steps", index, "per_test"),
                    "A step that runs once must come before every step that runs for each "
                    "test, since it cannot read anything they give. Move it up.",
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
                (
                    (),
                    "A reported value's name may use only lower case letters, digits and "
                    "underscores (_), starting with a letter.",
                )
            )
        if name in RESERVED_NAMES:
            found.append(((), reserved_message(name)))
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
                found.append((("at_most",), "The upper bound must be at least the lower bound."))
        return found

    def _better_problem(self, better: str) -> str | None:
        try:
            found = whole_reference(better)
        except BadReference as bad:
            return str(bad)
        if found is None or found.kind != "inputs":
            return (
                "Choose higher or lower, or name an enum input the task gives, as "
                "${{ inputs.<id> }}."
            )
        declared_input = self.inputs.get(found.name)
        if declared_input is None:
            return f"The workflow has no input called {found.name}."
        if declared_input.contestant or declared_input.type is not Type.ENUM:
            return f"{found.name} must be an enum input that the task gives, not the entrant."
        if declared_input.optional:
            return (
                f"{found.name} is optional, so it cannot say which way is better. Choose an "
                "input that is not optional."
            )
        if not set(declared_input.options or ()) <= {"higher", "lower"}:
            return f"The options of {found.name} must be higher and lower, and nothing else."
        return None


_NOT_A_HANDLE = (
    "Use at most 40 lower case letters, digits, hyphens (-) and underscores (_), starting "
    "with a letter or a digit."
)
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
    """One `${{ ... }}`: `inputs.<id>`, `test.<field>`, `steps.<id>.<output>`,
    or `workflow.<path>`, the path its `name`. An `interact` step's output
    for one program, `steps.<id>.<program>.<output>`, has the output
    `<program>.<output>`, the name the primitive gives it.
    """

    kind: Literal["inputs", "test", "steps", "workflow"]
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
    head, dot, path = expression.partition(".")
    if head == "workflow" and dot:
        problem = path_problem(path)
        if problem is not None:
            raise BadReference(f"Unicon cannot read ${{{{ {expression} }}}}: {problem}")
        return Reference("workflow", path)
    parts = expression.split(".")
    match parts:
        case ["inputs", name] if name:
            return Reference("inputs", name)
        case ["test", field] if field:
            return Reference("test", field)
        case ["steps", step, output] if step and output:
            return Reference("steps", step, output)
        case ["steps", step, program, output] if step and program and output:
            return Reference("steps", step, f"{program}.{output}")
    raise BadReference(
        f"Unicon cannot read ${{{{ {expression} }}}}. Write ${{{{ inputs.<id> }}}}, "
        "${{ steps.<id>.<output> }}, ${{ steps.<id>.<program>.<output> }}, "
        "${{ workflow.<path> }} or ${{ test.<field> }}."
    )


def path_problem(path: str) -> str | None:
    """What is wrong with `path` as a file of a workflow, or a folder when
    it ends in `/`: none when it is a relative path of named parts, no part
    `.` or `..`, none starting or ending in a space, and no backslash or
    control character.
    """
    parts = path.removesuffix("/").split("/")
    if (
        any(part in ("", ".", "..") or part != part.strip() for part in parts)
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        return (
            "give a file of the workflow by its path from the workflow's top, such as "
            "diff/program.yaml, or a folder with a / at its end, such as diff/."
        )
    return None


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
        "Inputs are now listed by name, each with its type, such as `submission: {type: file, "
        "contestant: true}`.",
        _list_of_inputs,
    ),
    Retired(("inputs", ANY), retired_type_message, retired_type),
)
RETIRED = (
    *_FILE_KEYS,
    Retired(
        ("outputs",),
        "This is now called report: each value the workflow reports, named, with the step "
        "output it reads, such as ${{ steps.<id>.<output> }}. Move these there.",
    ),
    *_OLD_INPUTS,
    Retired(("test", ANY), retired_type_message, retired_type),
    Retired(
        ("steps", ANY, "foreach"),
        "This is now set by running the step for each test. Turn that on, and remove this.",
    ),
)


def parse_workflow(text: bytes | str) -> WorkflowDefinition:
    """The `workflow.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(WorkflowDefinition, WORKFLOW_FILE, load_mapping(WORKFLOW_FILE, text), RETIRED)


# The run workflow (SIMPLER-CORE.md 7)


class RunInput(Model):
    """One input of a run workflow (SIMPLER-CORE.md 7.1): its type, `options`
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
    """One step of a run workflow: the primitive it uses and what each of
    its ports is given. A `named_files` port takes a mapping of names to
    files, a `programs` port a mapping of program names to each program's
    ports, and a `links` port a list of `"<from> -> <to>"`.
    """

    id: Annotated[str, PlainValidator(_handle)]
    use: Ref
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class RunReport(Model):
    """A report with the words a page names it by: `from` the step output it
    reads, its `title`, and its `unit`, on a number only. Which way is
    better and its bounds are the primitive's or the program's that made
    the number, never the report's.
    """

    from_: str = Field(alias="from")
    title: Line | None = None
    unit: Line | None = None


RunDeclared = Annotated[RunInput, BeforeValidator(declared)]


def _run_report(value: object) -> str | RunReport:
    if isinstance(value, dict):
        return RunReport.model_validate(value)
    if not isinstance(value, str):
        raise ValueError(
            "Give the step output this reports, as ${{ steps.<id>.<output> }}, or that "
            "output with the words a page names it by: from, title and unit."
        )
    return value


_RunReportEntry = Annotated[str | RunReport, PlainValidator(_run_report)]

SELF_REPORTED = "self_reported"
"""The port a primitive is told on that the program it runs given a manifest
runs the entrant's code, which Unicon sets from the task, never a workflow."""
SELF_REPORTED_GIVEN = (
    "Unicon gives this itself, from which inputs the task makes the entrant's, once the "
    "organiser confirms the reports as self-reported. Remove it."
)


class RunWorkflow(Model):
    """A run workflow's `workflow.yaml`: one run's inputs, its steps, at
    least one, and the reports it gives beside the `verdict` and `passed`
    every run has. Report names and input names are one set of columns.
    """

    inputs: dict[str, RunDeclared] = Field(default_factory=dict)
    steps: tuple[RunStep, ...] = Field(min_length=1)
    reports: dict[str, _RunReportEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> RunWorkflow:
        problems = Problems()
        for key in self.inputs:
            if not REPORT_NAME.match(key):
                problems.add(
                    ("inputs", key),
                    "An input's name may use only lower case letters, digits and underscores "
                    "(_), starting with a letter, since a formula names it as a column of the "
                    "run.",
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
            if SELF_REPORTED in step.with_:
                problems.add(("steps", index, "with", SELF_REPORTED), SELF_REPORTED_GIVEN)
        for name in self.reports:
            at = ("reports", name)
            if not REPORT_NAME.match(name):
                problems.add(
                    at,
                    "A report's name may use only lower case letters, digits and underscores "
                    "(_), starting with a letter.",
                )
            elif name in RESERVED_NAMES or name in RUN_NAMES:
                problems.add(at, reserved_message(name))
            elif name in self.inputs:
                problems.add(
                    at,
                    f"{name} is an input of the workflow, and a report and an input are columns "
                    "of the same run. Report this under another name.",
                )
        problems.raise_any()
        return self


_MEANT_BY_MAKER = (
    "What a number means comes from the primitive.yaml or program.yaml that made it, and a "
    "report adds only a title and a unit. Remove this."
)
RUN_RETIRED = (
    *_FILE_KEYS,
    Retired(
        ("outputs",),
        "This is now called reports: each value a run reports, named, with the step output "
        "it reads, such as ${{ steps.<id>.<output> }}. Move these there.",
    ),
    Retired(
        ("report",),
        "A workflow without a test block, which describes one run, names this reports, and a "
        "report there gives only from, title and unit. Rename it reports.",
    ),
    *_OLD_INPUTS,
    Retired(
        ("inputs", ANY, "contestant"),
        "The task decides who gives an input, in its own inputs. Remove this.",
    ),
    Retired(
        ("inputs", ANY, "per_test"),
        "A workflow without a test block describes one run, and the task says which inputs "
        "repeat. Remove this.",
    ),
    Retired(
        ("steps", ANY, "per_test"),
        "A workflow without a test block describes one run, so every step runs once in it, "
        "and the task says what repeats. Remove this.",
    ),
    Retired(("steps", ANY, "foreach"), "The task says what repeats. Remove this."),
    Retired(
        ("reports", ANY, "fold"),
        "How runs add up is the task's scoring, not the workflow's. Remove this.",
    ),
    Retired(("reports", ANY, "better"), _MEANT_BY_MAKER),
    Retired(("reports", ANY, "at_least"), _MEANT_BY_MAKER),
    Retired(("reports", ANY, "at_most"), _MEANT_BY_MAKER),
)


def is_run_workflow(document: Mapping[str, object]) -> bool:
    """Whether a `workflow.yaml`'s document is a run workflow: one with no
    `test` block.
    """
    return "test" not in document


def parse_run_workflow(text: bytes | str) -> RunWorkflow:
    """The run workflow in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(RunWorkflow, WORKFLOW_FILE, load_mapping(WORKFLOW_FILE, text), RUN_RETIRED)


def read_workflow(text: bytes | str) -> WorkflowDefinition | RunWorkflow:
    """The `workflow.yaml` in `text` in its own format: a `RunWorkflow` when
    it has no `test` block, a `WorkflowDefinition` otherwise. Raises
    `InvalidDefinition` as each format's parse does.
    """
    document = load_mapping(WORKFLOW_FILE, text)
    if is_run_workflow(document):
        return validate(RunWorkflow, WORKFLOW_FILE, document, RUN_RETIRED)
    return validate(WorkflowDefinition, WORKFLOW_FILE, document, RETIRED)


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
  submission:
    type: file
    contestant: true
    title: Solution
    help: The source file the contestant submits.
  language:
    type: enum
    options: [c, cpp, java, python]
    contestant: true
    title: Language
    help: The languages a contestant may write their solution in.
  time_limit:
    type: number
    title: Time limit
    unit: seconds
    help: The CPU time the program may use on each test.
  memory_limit:
    type: number
    title: Memory limit
    unit: MB
    help: The memory the program may use on each test.

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
  time_ms:
    from: ${{{{ steps.run.time_ms }}}}
    title: Run time
    unit: ms
    fold: max
    better: lower
    at_least: 0
  memory_kb:
    from: ${{{{ steps.run.memory_kb }}}}
    title: Memory used
    unit: KB
    fold: max
    better: lower
    at_least: 0
  log:
    from: ${{{{ steps.compile.compile_log }}}}
    title: Compile log
"""
    return {WORKFLOW_FILE: workflow.encode()}


__all__ = [
    "RESERVED_NAMES",
    "RUN_NAMES",
    "SELF_REPORTED",
    "SELF_REPORTED_GIVEN",
    "WORKFLOW_FILE",
    "BadReference",
    "Fold",
    "Meaning",
    "Reference",
    "RunInput",
    "RunReport",
    "RunStep",
    "RunWorkflow",
    "TestField",
    "WorkflowDefinition",
    "WorkflowInput",
    "WorkflowRef",
    "WorkflowStep",
    "is_run_workflow",
    "parse_reference",
    "parse_run_workflow",
    "parse_workflow",
    "parse_workflow_ref",
    "path_problem",
    "read_workflow",
    "references",
    "reserved_message",
    "starter_workflow",
    "validate_version",
    "version_number",
    "whole_reference",
]
