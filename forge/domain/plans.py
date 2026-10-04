"""The plan a save compiles for each stage of a task, and what a publication
compares to say whether it changed how the task grades.

A plan is what grading reads (the runner's `plan.schema.json`, version 4):
flat and fully resolved, so the harness never reads a workflow or a
primitive's declaration. It names the harness image, the stage, the test
list and the steps in the order they run, each with its primitive's image by
digest and its limits, and the verdict block that says which step outputs
fill the verdict. A step's container runs its image's own entrypoint. It is
written into the task repo as `plans/<stage>.json` in the commit a
publication names.

The compiler takes the stage's workflow step by step. Each `use:` is the
primitive whose declaration the save read, as the organiser saving, at the
version named. Each `with` value becomes one value of the plan:

- a literal, or text with a setter's text, number or true-or-false input
  written into it, is `{"value": ...}`;
- `${{ inputs.<id> }}` naming a setter input is its value: a literal as
  `{"value": ...}`, a file as `{"task": path}`, a `file[]` as the list of the
  files directly in its folder, a dataset folder as every file under it;
- naming a contestant input it is `{"submission": id}`, and
  `${{ inputs.<id>.language }}` the language chosen for a code input;
- `${{ steps.<id>.<output> }}` is an earlier step's output; inside a
  `foreach`, a step of the same `foreach` is read for the same test, and from
  a step that runs once, a step that runs per test gives its output over
  every test;
- `${{ item.<field> }}` is the file of that field of the current test.

A `foreach` over a setter's `file[]` input runs the step once per test of
that folder (`cases_of`), and a primitive that declares `batch: true` takes
them all in one step. Every step with a `foreach` in one plan runs over the
same list. The compiler checks what grading the built-in workflows needs:
every input a step is given is one its primitive declares, every input it
requires is given, every output a step or the verdict reads is one its step
declares, and a value's type is the input's. The step's limits are the
declaration's, raised by `limits_from` from values known at the save, and for
a batch its time and CPU are summed over its tests.

Checking every type, a workflow used as a step, and subtask overrides are
feature 10's. Before it compiles, the compiler checks that every input the
workflow declares is given by exactly one side of the task, with the type the
workflow declares.

`HARNESS_IMAGE` is the harness image of runner release v0.3.0, from that
release's `images.json`: what `UNICON_HARNESS_IMAGE` is unless a deployment
sets it, and what every plan names.
"""

import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, PlainValidator, model_validator

from forge.domain.definitions import (
    ContestantInput,
    ResolvedStage,
    SetterInput,
    TaskDefinition,
)
from forge.domain.primitives import (
    BATCH_SCALED,
    IMAGE,
    LIMIT_NAMES,
    Port,
    PortType,
    PrimitiveDeclaration,
)
from forge.domain.workflow_definition import InputType, WorkflowDefinition, WorkflowStep
from forge.domain.yaml_models import InvalidDefinition, Model, Problem, is_number, path_text

HARNESS_IMAGE = (
    "ghcr.io/uniconhq/harness"
    "@sha256:2f73048019369c3ce58c9165e8ca2f45dbdc6815792ca2eb88c922896f7d8400"
)

SCHEMA_VERSION: Literal[4] = 4
STEP_ID = r"^[a-z0-9][a-z0-9_-]*$"
PRIMITIVE = r"^[a-z0-9][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"
TEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FIELDS = {"in": "input", "ans": "answer", "out": "answer"}
VERDICT_TESTS = ("time_ms", "memory_kb")


def _image(value: object) -> str:
    if not isinstance(value, str) or not IMAGE.match(value):
        raise ValueError("must be an image by digest")
    return value


Image = Annotated[str, PlainValidator(_image)]


def _plan_value(value: object) -> dict[str, Any]:
    """One value of a step's inputs, in exactly one of the plan's shapes."""
    if not isinstance(value, dict):
        raise ValueError("a value is a mapping")
    keys = set(value)
    if keys == {"value"} and (isinstance(value["value"], str | bool) or is_number(value["value"])):
        return value
    if keys == {"task"} and (
        isinstance(value["task"], str)
        or (isinstance(value["task"], list) and all(isinstance(p, str) for p in value["task"]))
    ):
        return value
    if (
        keys in ({"submission"}, {"submission", "field"})
        and isinstance(value["submission"], str)
        and value.get("field", "language") == "language"
    ):
        return value
    if keys in ({"step", "output"}, {"step", "output", "test"}) and all(
        isinstance(value[key], str) for key in keys
    ):
        return value
    raise ValueError(f"{value!r} is not a value a plan holds")


Value = Annotated[dict[str, Any], PlainValidator(_plan_value)]
Inputs = dict[str, Value]


class StepLimits(Model):
    time_ms: Annotated[int, Field(ge=1)]
    cpu_ms: Annotated[int, Field(ge=1)]
    memory_mb: Annotated[int, Field(ge=1)]
    pids: Annotated[int, Field(ge=1)]
    output_mb: Annotated[int, Field(ge=1)]


class BatchItem(Model):
    test: Annotated[str, Field(pattern=TEST_ID.pattern)]
    inputs: Inputs


class PlanStep(Model):
    """One step: runs once with `inputs`, once for one test with `inputs` and
    `test`, or once for many tests with `batch`.
    """

    id: Annotated[str, Field(pattern=STEP_ID)]
    primitive: Annotated[str, Field(pattern=PRIMITIVE)]
    image: Image
    limits: StepLimits
    inputs: Inputs | None = None
    test: Annotated[str, Field(pattern=TEST_ID.pattern)] | None = None
    batch: tuple[BatchItem, ...] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_shape(self) -> PlanStep:
        if (self.inputs is None) == (self.batch is None):
            raise ValueError("a step has inputs or a batch, not both")
        if self.batch is not None and self.test is not None:
            raise ValueError("a batch step names its tests in the batch")
        return self


class Reference(Model):
    step: Annotated[str, Field(pattern=STEP_ID)]
    output: Annotated[str, Field(min_length=1)]


class VerdictBlock(Model):
    """Which step outputs fill the verdict: its outcome, its named metrics,
    each test's time and memory, and its summary.
    """

    outcome: Reference
    metrics: dict[str, Reference] | None = None
    tests: dict[Literal["time_ms", "memory_kb"], Reference] | None = None
    summary: Reference | None = None


class Plan(Model):
    """One stage's plan, in the shape of the runner's plan schema version 4."""

    schema_version: Literal[4] = SCHEMA_VERSION
    harness_image: Image
    stage: Annotated[str, Field(min_length=1)]
    tests: tuple[Annotated[str, Field(pattern=TEST_ID.pattern)], ...] = ()
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    verdict: VerdictBlock

    @model_validator(mode="after")
    def _unique_steps(self) -> Plan:
        seen = [(step.id, step.test) for step in self.steps]
        if len(set(seen)) != len(seen):
            raise ValueError("a step id is given twice for one test")
        return self

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


def _natural(text: str) -> tuple[tuple[int, int, str], ...]:
    """A key that orders text with the numbers in it compared as numbers."""
    return tuple(
        (0, int(part), part) if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


@dataclass(frozen=True, slots=True)
class Case:
    """One test of a `foreach` list: its id, the stem its files share, and
    the path of each of its fields' files.
    """

    id: str
    files: Mapping[str, str]


def cases_of(folder: str, paths: Collection[str]) -> tuple[list[Case], list[str]]:
    """The tests a `file[]` folder holds among `paths`, in order, and a
    sentence for each thing wrong with them. The files directly in the folder,
    hidden ones and ones without an ending left out, are grouped by stem:
    `1.in` and `1.ans` are the test `1` with the fields `input` and `answer`.
    `.out` is an `answer` too, and any other ending `x` the field `x`.
    """
    grouped: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for path in sorted(paths):
        if not path.startswith(folder):
            continue
        name = path.removeprefix(folder)
        if "/" in name or name.startswith(".") or "." not in name:
            continue
        stem, ending = name.rsplit(".", 1)
        field = FIELDS.get(ending, ending)
        files = grouped.setdefault(stem, {})
        if field in files:
            problems.append(f"The test {stem} has two {field} files in {folder}.")
            continue
        files[field] = path
    tests = []
    for stem in sorted(grouped, key=_natural):
        if not TEST_ID.match(stem):
            problems.append(
                f"{stem!r} in {folder} is not a test name: letters, digits, dots, hyphens and "
                "underscores, starting with a letter or a digit."
            )
            continue
        tests.append(Case(id=stem, files=grouped[stem]))
    return tests, problems


def _files_directly_in(folder: str, paths: Collection[str]) -> list[str]:
    return sorted(
        (
            path
            for path in paths
            if path.startswith(folder)
            and "/" not in path.removeprefix(folder)
            and not path.removeprefix(folder).startswith(".")
        ),
        key=_natural,
    )


def _files_under(folder: str, paths: Collection[str]) -> list[str]:
    return sorted((path for path in paths if path.startswith(folder)), key=_natural)


_WHOLE = re.compile(r"^\s*\$\{\{\s*([^{}]*?)\s*\}\}\s*$")
_ANY = re.compile(r"\$\{\{\s*([^{}]*?)\s*\}\}")
_SETTER_PART = re.compile(r"^inputs\.([A-Za-z0-9_-]+)$")


@dataclass(frozen=True, slots=True)
class Kind:
    """What a resolved value is, for checking it against the input it is
    given to: its type, the values it may take when it is an enum, and
    whether it is one value per test.
    """

    type: PortType
    values: frozenset[str] | None = None
    per_test: bool = False


@dataclass(frozen=True, slots=True)
class _Compiled:
    declaration: PrimitiveDeclaration
    per_test: bool


class _Refused(Exception):
    """One problem with a value, said in a sentence."""


class _Skipped(Exception):
    """A value reads a step that failed to compile, whose problem is already
    reported, so nothing more is said about it.
    """


@dataclass
class _Stage:
    """What compiling one stage works with, and the problems it finds, each
    carried at the YAML path of the stage's workflow in `task.yaml`.
    """

    task: TaskDefinition
    stage: ResolvedStage
    workflow: WorkflowDefinition
    primitives: Mapping[str, PrimitiveDeclaration]
    paths: Collection[str]
    at: str
    setter_at: Mapping[str, str]
    problems: list[Problem]

    def __post_init__(self) -> None:
        self.setter = {entry.id: entry for entry in self.stage.setter}
        self.contestant = {entry.id: entry for entry in self.task.inputs.contestant}
        self.compiled: dict[str, _Compiled] = {}
        self.failed: set[str] = set()
        self.folder: str | None = None
        self.tests: list[Case] = []

    def refuse(self, where: str, message: str) -> None:
        self.problems.append(
            Problem(path=self.at, message=f"In {self.workflow.ref}, {where}: {message}")
        )

    def plan(self, harness_image: str) -> Plan | None:
        before = len(self.problems)
        steps: list[PlanStep] = []
        for index, step in enumerate(self.workflow.steps):
            reported = len(self.problems)
            found = self._step(index, step)
            if not found or len(self.problems) > reported:
                self.failed.add(step.id)
            steps.extend(found)
        verdict = self._verdict()
        if len(self.problems) > before or verdict is None or not steps:
            return None
        return Plan(
            harness_image=harness_image,
            stage=self.stage.id,
            tests=tuple(test.id for test in self.tests),
            steps=tuple(steps),
            verdict=verdict,
        )

    def _step(self, index: int, step: WorkflowStep) -> list[PlanStep]:
        where = f"steps[{index}]"
        declaration = self.primitives.get(str(step.use))
        if declaration is None:
            self.refuse(f"{where}.use", f"{step.use} is not a primitive.")
            return []
        unfit = False
        for name in sorted(set(step.with_) - set(declaration.inputs)):
            self.refuse(f"{where}.with.{name}", f"{step.use} has no input {name}.")
            unfit = True
        for name, port in declaration.inputs.items():
            if not port.optional and name not in step.with_:
                self.refuse(f"{where}.with", f"{step.use} needs the input {name}.")
                unfit = True
        tests = self._foreach(where, step) if step.foreach is not None else None
        self.compiled[step.id] = _Compiled(declaration, per_test=tests is not None)
        if unfit or (step.foreach is not None and tests is None):
            return []
        common = {
            "id": step.id,
            "primitive": f"{declaration.short_name}@{declaration.version}",
            "image": declaration.image,
        }
        if tests is None:
            inputs = self._inputs(where, step, declaration, None)
            limits = self._limits(where, declaration, [inputs] if inputs is not None else [])
            if inputs is None or limits is None:
                return []
            return [PlanStep(**common, limits=limits, inputs=inputs)]
        runs = [(test, self._inputs(where, step, declaration, test)) for test in tests]
        if any(inputs is None for _, inputs in runs):
            return []
        given = [inputs for _, inputs in runs if inputs is not None]
        if declaration.batch:
            limits = self._limits(where, declaration, given)
            if limits is None:
                return []
            batch = tuple(
                BatchItem(test=test.id, inputs=inputs)
                for test, inputs in zip(tests, given, strict=True)
            )
            return [PlanStep(**common, limits=limits, batch=batch)]
        found = []
        for test, inputs in zip(tests, given, strict=True):
            limits = self._limits(where, declaration, [inputs])
            if limits is None:
                return []
            found.append(PlanStep(**common, limits=limits, inputs=inputs, test=test.id))
        return found

    def _foreach(self, where: str, step: WorkflowStep) -> list[Case] | None:
        assert step.foreach is not None
        whole = _WHOLE.match(step.foreach)
        name = _SETTER_PART.match(whole.group(1)) if whole else None
        entry = self.setter.get(name.group(1)) if name else None
        if entry is None or entry.type is not InputType.FILES:
            self.refuse(
                f"{where}.foreach",
                "A foreach runs over a setter's file[] input, ${{ inputs.<id> }}.",
            )
            return None
        folder = str(entry.value)
        if self.folder is None:
            tests, problems = cases_of(folder, self.paths)
            at = self.setter_at.get(entry.id, "inputs.setter")
            for message in problems:
                self.problems.append(Problem(path=at, message=message))
            if not tests and not problems:
                self.problems.append(
                    Problem(
                        path=at,
                        message=f"There is no test in {folder}: add files such as 1.in and 1.ans.",
                    )
                )
            if problems or not tests:
                self.folder = folder
                return None
            self.folder, self.tests = folder, tests
        elif folder != self.folder:
            self.refuse(
                f"{where}.foreach",
                f"Every foreach of a workflow runs over one list, {self.folder}, until feature 10.",
            )
            return None
        return self.tests or None

    def _inputs(
        self,
        where: str,
        step: WorkflowStep,
        declaration: PrimitiveDeclaration,
        test: Case | None,
    ) -> Inputs | None:
        inputs: Inputs = {}
        failed = False
        for name, raw in step.with_.items():
            port = declaration.inputs.get(name)
            if port is None:
                continue
            try:
                value, kind = self._value(raw, test)
                _check(port, value, kind)
            except _Skipped:
                failed = True
                continue
            except _Refused as refused:
                self.refuse(f"{where}.with.{name}", str(refused))
                failed = True
                continue
            inputs[name] = value
        return None if failed else inputs

    def _value(self, raw: object, test: Case | None) -> tuple[dict[str, Any], Kind]:
        if isinstance(raw, bool):
            return {"value": raw}, Kind(PortType.BOOLEAN)
        if is_number(raw):
            return {"value": raw}, Kind(PortType.NUMBER)
        if raw is None:
            raise _Refused("Must be given a value.")
        if isinstance(raw, list | dict):
            raise _Refused("A list or a mapping as an input comes with feature 10.")
        if not isinstance(raw, str):
            raise _Refused("Must be text, a number, true or false, or a ${{ }} reference.")
        whole = _WHOLE.match(raw)
        if whole is None:
            return {"value": _ANY.sub(self._written, raw)}, Kind(PortType.TEXT)
        return self._reference(whole.group(1), test)

    def _written(self, match: re.Match[str]) -> str:
        found = _SETTER_PART.match(match.group(1))
        entry = self.setter.get(found.group(1)) if found else None
        if entry is None or entry.type not in (
            InputType.TEXT,
            InputType.CODE,
            InputType.NUMBER,
            InputType.BOOLEAN,
        ):
            raise _Refused(
                f"Only a setter's text, number or true-or-false input is written into text, "
                f"not {match.group(0)}."
            )
        return str(entry.value)

    def _reference(self, expression: str, test: Case | None) -> tuple[dict[str, Any], Kind]:
        parts = expression.split(".")
        match parts:
            case ["inputs", name]:
                return self._input(name)
            case ["inputs", name, "language"]:
                entry = self.contestant.get(name)
                if entry is None or entry.type is not InputType.CODE:
                    raise _Refused(f"{name} is not a contestant's code input.")
                if entry.language is None:
                    raise _Refused(f"The code input {name} lists no languages in task.yaml.")
                return {"submission": name, "field": "language"}, Kind(
                    PortType.ENUM, frozenset(entry.language)
                )
            case ["steps", step, output]:
                return self._output(step, output, test)
            case ["item", field]:
                if test is None:
                    raise _Refused("item is there only inside a step with a foreach.")
                path = test.files.get(field)
                if path is None:
                    raise _Refused(f"The test {test.id} has no {field} file in {self.folder}.")
                return {"task": path}, Kind(PortType.FILE)
        raise _Refused(
            f"${{{{ {expression} }}}} is not a reference a workflow makes: inputs.<id>, "
            "inputs.<id>.language, steps.<id>.<output> or item.<field>."
        )

    def _input(self, name: str) -> tuple[dict[str, Any], Kind]:
        entry = self.setter.get(name)
        if entry is not None:
            return _setter_value(entry, self.paths)
        given = self.contestant.get(name)
        if given is not None:
            return _contestant_value(given)
        raise _Refused(f"{name} is not an input the task gives.")

    def _output(self, step: str, output: str, test: Case | None) -> tuple[dict[str, Any], Kind]:
        if step in self.failed:
            raise _Skipped
        compiled = self.compiled.get(step)
        if compiled is None:
            raise _Refused(f"{step} is not a step before this one.")
        port = compiled.declaration.outputs.get(output)
        if port is None:
            raise _Refused(f"The step {step} has no output {output}.")
        values = frozenset(port.values) if port.values is not None else None
        if compiled.per_test and test is not None:
            return {"step": step, "output": output, "test": test.id}, Kind(port.type, values)
        return {"step": step, "output": output}, Kind(port.type, values, compiled.per_test)

    def _limits(
        self, where: str, declaration: PrimitiveDeclaration, runs: Sequence[Inputs]
    ) -> StepLimits | None:
        raised: list[dict[str, int]] = []
        for inputs in runs:
            limits = declaration.limits.as_mapping()
            for name, source in declaration.limits_from.items():
                given = inputs.get(source.input)
                if given is None:
                    continue
                value = given.get("value") if set(given) == {"value"} else None
                if not is_number(value):
                    self.refuse(
                        f"{where}.with.{source.input}",
                        f"The limit {name} is raised from {source.input}, so it is a number "
                        "known at the save.",
                    )
                    return None
                assert isinstance(value, int | float)
                wanted = value * source.scale + source.add
                if not math.isfinite(wanted):
                    self.refuse(
                        f"{where}.with.{source.input}",
                        f"The limit {name} raised from {source.input} is too large.",
                    )
                    return None
                limits[name] = max(limits[name], math.ceil(wanted))
            raised.append(limits)
        if not raised:
            return StepLimits(**declaration.limits.as_mapping())
        combined = {
            name: sum(run[name] for run in raised)
            if name in BATCH_SCALED
            else max(run[name] for run in raised)
            for name in LIMIT_NAMES
        }
        return StepLimits(**combined)

    def _verdict(self) -> VerdictBlock | None:
        outputs = self.workflow.outputs
        before = len(self.problems)
        if "outcome" not in outputs:
            self.refuse("outputs", "A workflow's outputs give its outcome.")
            return None
        outcome = self._slot("outputs.outcome", outputs["outcome"], PortType.OUTCOME, None)
        metrics = self._slots("outputs.metrics", outputs.get("metrics"), PortType.NUMBER, None)
        tests = self._slots("outputs.tests", outputs.get("tests"), PortType.NUMBER, True)
        if tests is not None:
            for name in sorted(set(tests) - set(VERDICT_TESTS)):
                self.refuse(f"outputs.tests.{name}", "A test's row takes time_ms and memory_kb.")
        summary = (
            self._slot("outputs.summary", outputs["summary"], PortType.TEXT, False)
            if "summary" in outputs
            else None
        )
        if len(self.problems) > before or outcome is None:
            return None
        return VerdictBlock.model_validate(
            {"outcome": outcome, "metrics": metrics, "tests": tests, "summary": summary}
        )

    def _slots(
        self, where: str, raw: object, wanted: PortType, per_test: bool | None
    ) -> dict[str, Reference] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            self.refuse(where, "Must be a mapping of names to ${{ steps.<id>.<output> }}.")
            return None
        found = {}
        for name, value in raw.items():
            reference = self._slot(f"{where}.{name}", value, wanted, per_test)
            if reference is not None:
                found[str(name)] = reference
        return found

    def _slot(
        self, where: str, raw: object, wanted: PortType, per_test: bool | None
    ) -> Reference | None:
        whole = _WHOLE.match(raw) if isinstance(raw, str) else None
        parts = whole.group(1).split(".") if whole else []
        if len(parts) != 3 or parts[0] != "steps":
            self.refuse(where, "Must be ${{ steps.<id>.<output> }}.")
            return None
        step, output = parts[1], parts[2]
        if step in self.failed:
            return None
        compiled = self.compiled.get(step)
        port = compiled.declaration.outputs.get(output) if compiled else None
        if compiled is None or port is None:
            self.refuse(where, f"steps.{step}.{output} is not an output of a step.")
            return None
        if port.type is not wanted:
            self.refuse(where, f"steps.{step}.{output} is {port.type}, not {wanted}.")
            return None
        if per_test is not None and compiled.per_test is not per_test:
            runs = "once per test" if per_test else "once"
            self.refuse(where, f"Reads a step that runs {runs}; steps.{step} does not.")
            return None
        return Reference(step=step, output=output)


def _setter_value(entry: SetterInput, paths: Collection[str]) -> tuple[dict[str, Any], Kind]:
    match entry.type:
        case InputType.CODE | InputType.TEXT:
            return {"value": entry.value}, Kind(PortType.TEXT)
        case InputType.NUMBER:
            return {"value": entry.value}, Kind(PortType.NUMBER)
        case InputType.BOOLEAN:
            return {"value": entry.value}, Kind(PortType.BOOLEAN)
        case InputType.FILE:
            return {"task": entry.value}, Kind(PortType.FILE)
        case InputType.FILES:
            return {"task": _files_directly_in(entry.value, paths)}, Kind(PortType.FILES)
        case InputType.DATASET:
            if str(entry.value).endswith("/"):
                return {"task": _files_under(entry.value, paths)}, Kind(PortType.FILES)
            return {"task": entry.value}, Kind(PortType.FILE)
    raise _Refused(f"The setter input {entry.id} cannot be given to a step.")


def _contestant_value(entry: ContestantInput) -> tuple[dict[str, Any], Kind]:
    kinds = {
        InputType.CODE: PortType.FILE,
        InputType.FILE: PortType.FILE,
        InputType.FILES: PortType.FILES,
        InputType.TEXT: PortType.TEXT,
        InputType.NUMBER: PortType.NUMBER,
        InputType.BOOLEAN: PortType.BOOLEAN,
    }
    kind = kinds.get(entry.type)
    if kind is None:
        raise _Refused(f"A {entry.type} input is not given to a step yet.")
    return {"submission": entry.id}, Kind(kind)


def _check(port: Port, value: Mapping[str, Any], kind: Kind) -> None:
    """Refuse `value`, of `kind`, given to an input declared as `port`. Text
    written in the workflow is one of an enum's values when it is written as
    one.
    """
    wanted = port.type
    if wanted is PortType.ENUM and set(value) == {"value"} and kind.type is PortType.TEXT:
        if value["value"] in (port.values or ()):
            return
        raise _Refused(f"Takes one of {', '.join(port.values or ())}.")
    if kind.per_test:
        if wanted is PortType.FILES and kind.type is PortType.FILE:
            return
        raise _Refused(
            f"Takes one {wanted}, and a step that runs once per test gives one value per test."
        )
    if wanted is PortType.ENUM:
        allowed = frozenset(port.values or ())
        if kind.type is PortType.ENUM and kind.values is not None and kind.values <= allowed:
            return
        if kind.type is PortType.ENUM and kind.values is not None:
            extra = ", ".join(sorted(kind.values - allowed))
            raise _Refused(f"Takes one of {', '.join(port.values or ())}, not {extra}.")
        raise _Refused(f"Takes one of {', '.join(port.values or ())}.")
    if kind.type is not wanted:
        raise _Refused(f"Takes {wanted}, not {kind.type}.")


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


def _workflow_at(task: TaskDefinition, stage: ResolvedStage) -> str:
    own = stage.index is not None and task.stages[stage.index].workflow is not None
    return f"stages[{stage.index}].workflow" if own else "workflow"


def _setter_at(task: TaskDefinition, stage: ResolvedStage) -> dict[str, str]:
    """The YAML path of each setter input's value as the stage sees it: the
    stage's own when it gives one, the task's otherwise.
    """
    found = {
        entry.id: path_text(("inputs", "setter", index, "value"))
        for index, entry in enumerate(task.inputs.setter)
    }
    if stage.index is not None:
        for index, entry in enumerate(task.stages[stage.index].inputs.setter):
            found[entry.id] = path_text(("stages", stage.index, "inputs", "setter", index, "value"))
    return found


def compile_plans(
    task: TaskDefinition,
    workflows: Mapping[str, WorkflowDefinition],
    primitives: Mapping[str, PrimitiveDeclaration],
    paths: Collection[str],
    *,
    harness_image: str,
) -> dict[str, Plan]:
    """One plan per stage of `task`, keyed by stage id. `workflows` holds
    every workflow `task.workflow_refs()` names and `primitives` the
    declaration of every primitive their steps use, each keyed by the
    reference as text; `paths` is every file of the state being saved. Raises
    `InvalidDefinition` listing every problem, each at the YAML path in
    `task.yaml` it is about.
    """
    stages = task.stages_resolved()
    problems = [
        problem
        for stage in stages
        for problem in _coverage(task, stage, workflows[str(stage.workflow)])
    ]
    if problems:
        raise InvalidDefinition("task.yaml", problems)
    plans: dict[str, Plan] = {}
    for stage in stages:
        compiling = _Stage(
            task=task,
            stage=stage,
            workflow=workflows[str(stage.workflow)],
            primitives=primitives,
            paths=paths,
            at=_workflow_at(task, stage),
            setter_at=_setter_at(task, stage),
            problems=problems,
        )
        plan = compiling.plan(harness_image)
        if plan is not None:
            plans[stage.id] = plan
    if problems:
        raise InvalidDefinition("task.yaml", _unique(problems))
    return plans


def _unique(problems: Sequence[Problem]) -> list[Problem]:
    seen: set[tuple[str, str]] = set()
    found = []
    for problem in problems:
        key = (problem["path"], problem["message"])
        if key not in seen:
            seen.add(key)
            found.append(problem)
    return found


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
