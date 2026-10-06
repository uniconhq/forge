"""The plan a save compiles for a task, and what a publication compares to say
whether it changed how the task grades (TASK-FORMAT.md sections 1.5 and 2).

A plan is what grading reads (the runner's `plan.schema.json`, version 5):
the task's workflow with the task's values filled in, flat, so the harness
never reads a workflow or a primitive's declaration and fills in only the
contestant's values and the secrets. It names the harness image, every test
of the task, the contestant's inputs as the workflow declares them, the
steps in the order they run, each with its primitive's image by digest, its
limits, whether it may reach the network and its declared outputs, and the
workflow's report with each number's bounds. It is written into the task
repo as `plans/plan.json` in the commit a publication names.

Compiling has three parts, each refusing with every problem it finds:

- `check_workflow`, what making a workflow version checks, needing only the
  primitives its steps use: every port given and every value of the port's
  type under the two widenings, every reference to an earlier step and a
  declared output, `test.<field>` and per-test inputs only in per-test
  steps, optional outputs and inputs only into optional ports, no
  contestant input or step output into a port a limit is raised from, and
  the report's types (W2).
- `read_tests`, the task's tests from its folders `tests/<group>/<test>/`.
- `compile_plan`, the task's values bound to the workflow's inputs (check 4),
  every value written in, every limit raised and rounded up, the sealed
  steps found (check 8, T5), the plan's fit against the run ceiling and the
  machines it may run on (check 7), and the credit checked against the
  report (T3, T4).

A test's id is `<group>/<test>`, and the plan lists every test, groups in
name order and tests in natural order within each (`2` before `10`), so
reordering `test_groups` changes no plan. A per-test step over a primitive
that takes a batch is one step with an item per test, and over any other
one entry per test.
"""

import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, PlainValidator, model_validator

from forge.domain.definitions import (
    PUBLIC_FOLDER,
    TESTS_FOLDER,
    Relative,
    Show,
    TaskDefinition,
    form_problems,
)
from forge.domain.grading import BASE_WALL, STEP_OVERHEAD, WALL_CEILING, Machine
from forge.domain.primitives import BATCH_SCALED, IMAGE, LIMIT_NAMES, Port, PrimitiveDeclaration
from forge.domain.showing import NOTHING_SEALED, Sealed
from forge.domain.types import FILES, SCALARS, Type
from forge.domain.workflow_definition import (
    BadReference,
    Meaning,
    Reference,
    WorkflowDefinition,
    WorkflowInput,
    references,
    whole_reference,
)
from forge.domain.yaml_models import (
    InvalidDefinition,
    Model,
    Problem,
    is_number,
    load_mapping,
    path_text,
)

SCHEMA_VERSION: Literal[5] = 5
PLANS_FOLDER = "plans/"
PLAN_PATH = "plans/plan.json"
STEP_ID = r"^[a-z0-9][a-z0-9_-]*$"
PRIMITIVE = r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"
TEST_ID = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+$")
NAME = re.compile(r"^[A-Za-z0-9_-]+$")
TEST_FILE = "test.yaml"
SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _image(value: object) -> str:
    if not isinstance(value, str) or not IMAGE.match(value):
        raise ValueError("must be an image by digest")
    return value


Image = Annotated[str, PlainValidator(_image)]


def _scalar(value: object) -> bool:
    return isinstance(value, str | bool) or is_number(value)


def _submission(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"submission"}
        and isinstance(value["submission"], str)
    )


def _plan_value(value: object) -> dict[str, Any]:
    """One value of a step's inputs, in exactly one of the plan's shapes."""
    if not isinstance(value, dict):
        raise ValueError("a value is a mapping")
    keys = set(value)
    if keys == {"value"} and _scalar(value["value"]):
        return value
    if keys == {"task"} and isinstance(value["task"], str):
        return value
    if _submission(value):
        return value
    if keys == {"secret"} and isinstance(value["secret"], str):
        return value
    if keys == {"step", "output"} and all(isinstance(value[key], str) for key in keys):
        return value
    if (
        keys == {"template", "parts"}
        and isinstance(value["template"], str)
        and isinstance(value["parts"], list)
        and value["parts"]
        and all(_submission(part) for part in value["parts"])
    ):
        return value
    raise ValueError(f"{value!r} is not a value a plan holds")


Value = Annotated[dict[str, Any], PlainValidator(_plan_value)]
Inputs = dict[str, Value]
TestId = Annotated[str, Field(pattern=TEST_ID.pattern)]


class StepLimits(Model):
    time_ms: Annotated[int, Field(ge=1)]
    cpu_ms: Annotated[int, Field(ge=1)]
    memory_mb: Annotated[int, Field(ge=1)]
    pids: Annotated[int, Field(ge=1)]
    output_mb: Annotated[int, Field(ge=1)]
    gpus: Annotated[int, Field(ge=0)]


class BatchItem(Model):
    test: TestId
    inputs: Inputs


class PlanStep(Model):
    """One step: runs once with `inputs`, once for one test with `inputs` and
    `test`, or once for many tests with `batch`.
    """

    id: Annotated[str, Field(pattern=STEP_ID)]
    primitive: Annotated[str, Field(pattern=PRIMITIVE)]
    image: Image
    network: bool
    limits: StepLimits
    outputs: dict[str, str]
    folders: tuple[str, ...] | None = None
    inputs: Inputs | None = None
    test: TestId | None = None
    batch: tuple[BatchItem, ...] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_shape(self) -> PlanStep:
        if (self.inputs is None) == (self.batch is None):
            raise ValueError("a step has inputs or a batch, not both")
        if self.batch is not None and self.test is not None:
            raise ValueError("a batch step names its tests in the batch")
        return self


class ContestantInput(Model):
    """A contestant's input as the workflow declares it."""

    type: Type
    options: tuple[str, ...] | None = None
    per_test: Literal[True] | None = None


class ReportEntry(Model):
    step: Annotated[str, Field(pattern=STEP_ID)]
    output: Annotated[str, Field(min_length=1)]
    at_least: int | float | None = None
    at_most: int | float | None = None


class Plan(Model):
    """A task's plan, in the shape of the runner's plan schema version 5."""

    schema_version: Literal[5] = SCHEMA_VERSION
    harness_image: Image
    tests: tuple[TestId, ...] = Field(min_length=1)
    contestant: dict[str, ContestantInput] = Field(default_factory=dict)
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    report: dict[str, ReportEntry] = Field(default_factory=dict)

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

    def task_paths(self) -> tuple[str, ...]:
        """Every file or folder of the publication a value names, sorted; a
        folder ends in `/`.
        """
        found: set[str] = set()
        for step in self.steps:
            for inputs in [step.inputs] if step.inputs is not None else []:
                found.update(value["task"] for value in inputs.values() if "task" in value)
            for item in step.batch or ():
                found.update(value["task"] for value in item.inputs.values() if "task" in value)
        return tuple(sorted(found))


def is_reserved(path: str) -> bool:
    """Whether a path is inside the plans folder, which only the compiler
    writes.
    """
    return path == PLANS_FOLDER.rstrip("/") or path.startswith(PLANS_FOLDER)


def natural(text: str) -> tuple[tuple[int, int, str], ...]:
    """A key that orders text with the numbers in it compared as numbers."""
    return tuple(
        (0, int(part), part) if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


def spelled(value: object) -> str:
    """A scalar's one spelling as text: a boolean as `true` or `false`, a
    number as the shortest decimal equal to it, no exponent and no trailing
    zeros, an integer without a point; text and an enum's word as they are.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        number = Decimal(repr(value))
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.normalize(), "f")
    return str(value)


def path_problem(value: object, *, folder: bool) -> str | None:
    """What is wrong with `value` as a path in the task repo, if anything; a
    folder's path ends in `/` and a file's does not.
    """
    if not isinstance(value, str) or not value:
        return "Must be a path in the task repo, such as data/ or checker/checker.cpp."
    if value.startswith("/") or "\\" in value:
        return "Must be a path from the top of the task repo, with forward slashes."
    if any(part in ("", ".", "..") for part in value.removesuffix("/").split("/")):
        return "Must not have empty, . or .. parts."
    if folder and not value.endswith("/"):
        return "Names a folder, so it ends with /, such as data/."
    if not folder and value.endswith("/"):
        return "Names one file, so it does not end with /."
    return None


def has_path(paths: Collection[str], path: str) -> bool:
    """Whether `path` is in the state: a file by its path, a folder when some
    file is under it.
    """
    if path.endswith("/"):
        return any(found.startswith(path) for found in paths)
    return path in paths


# The tests


@dataclass(frozen=True, slots=True)
class TestCase:
    """One test: its id `<group>/<test>`, its group and name, the path of the
    entry for each file or folder field (a folder's ending in `/`), and the
    value of each scalar field from its `test.yaml`.
    """

    id: str
    group: str
    name: str
    entries: Mapping[str, str]
    scalars: Mapping[str, object]


def test_yaml_paths(paths: Collection[str]) -> list[str]:
    """Every `tests/<group>/<test>/test.yaml` in the state, which the save
    reads before it reads the tests.
    """
    return sorted(
        path
        for path in paths
        if path.startswith(TESTS_FOLDER)
        and path.count("/") == 3
        and path.rsplit("/", 1)[1] == TEST_FILE
    )


def read_tests(
    paths: Collection[str],
    fields: Mapping[str, Any],
    test_yamls: Mapping[str, bytes],
) -> tuple[list[TestCase], list[Problem]]:
    """The tests under `tests/` among `paths`, the files of the state, in the
    plan's order, each holding one entry per field the workflow's `test`
    block declares, and a problem at each folder or file that breaks the
    layout. `test_yamls` holds the content of every `test.yaml`. A file or
    folder whose name starts with a dot is left out.
    """
    problems: list[Problem] = []
    found: dict[tuple[str, str], dict[str, list[str]]] = {}
    for path in sorted(paths):
        if not path.startswith(TESTS_FOLDER):
            continue
        parts = path.removeprefix(TESTS_FOLDER).split("/")
        if any(part.startswith(".") for part in parts):
            continue
        if len(parts) < 3:
            where = "tests/" if len(parts) == 1 else f"tests/{parts[0]}/"
            problems.append(
                Problem(
                    path=path,
                    message=f"{where} holds only folders: tests/<group>/<test>/ and the "
                    "test's entries in it.",
                )
            )
            continue
        group, test, entry = parts[0], parts[1], parts[2]
        name = entry.split(".", 1)[0]
        folder = len(parts) > 3
        entries = found.setdefault((group, test), {})
        key = f"{entry}/" if folder else entry
        if key not in entries.setdefault(name, []):
            entries[name].append(key)
    tests: list[TestCase] = []
    for (group, test), entries in sorted(
        found.items(), key=lambda pair: (pair[0][0], natural(pair[0][1]))
    ):
        where = f"tests/{group}/{test}/"
        bad_name = [part for part in (group, test) if not NAME.match(part)]
        if bad_name:
            problems.append(
                Problem(
                    path=where, message=f"{bad_name[0]!r} is not a name: letters, digits, _ and -."
                )
            )
            continue
        made = _test(group, test, entries, fields, test_yamls.get(f"{where}{TEST_FILE}"), problems)
        if made is not None:
            tests.append(made)
    return tests, problems


def _test(
    group: str,
    test: str,
    entries: Mapping[str, list[str]],
    fields: Mapping[str, Any],
    test_yaml: bytes | None,
    problems: list[Problem],
) -> TestCase | None:
    where = f"tests/{group}/{test}/"
    before = len(problems)
    files: dict[str, str] = {}
    scalars: dict[str, object] = {}
    scalar_fields = {name: kind for name, kind in fields.items() if kind.type in SCALARS}
    for name, found in entries.items():
        if name == "test" and found == [TEST_FILE]:
            continue
        declared = fields.get(name)
        if declared is None or declared.type in SCALARS:
            problems.append(
                Problem(
                    path=f"{where}{found[0]}",
                    message=f"The test {group}/{test} has an entry for no field: {found[0]}.",
                )
            )
            continue
        if len(found) > 1:
            problems.append(
                Problem(
                    path=where,
                    message=f"The test {group}/{test} gives {name} twice: {', '.join(found)}.",
                )
            )
            continue
        is_folder = found[0].endswith("/")
        if is_folder != (declared.type is Type.FOLDER):
            wanted = "a folder" if declared.type is Type.FOLDER else "a file"
            problems.append(
                Problem(path=f"{where}{found[0]}", message=f"{name} is {wanted} field.")
            )
            continue
        files[name] = f"{where}{found[0]}"
    for name, declared in fields.items():
        if declared.type in FILES and name not in files and name not in entries:
            problems.append(Problem(path=where, message=f"The test {group}/{test} has no {name}."))
    has_yaml = "test" in entries and TEST_FILE in entries["test"]
    if has_yaml and not scalar_fields:
        problems.append(
            Problem(
                path=f"{where}{TEST_FILE}",
                message="The workflow's tests have no text, number, true-or-false or enum "
                "field, so a test has no test.yaml.",
            )
        )
    elif scalar_fields:
        scalars = _scalars(where, scalar_fields, test_yaml if has_yaml else None, problems)
    if len(problems) > before:
        return None
    return TestCase(f"{group}/{test}", group, test, files, scalars)


def _scalars(
    where: str, fields: Mapping[str, Any], text: bytes | None, problems: list[Problem]
) -> dict[str, object]:
    at = f"{where}{TEST_FILE}"
    if text is None:
        problems.append(
            Problem(path=where, message=f"The test needs a test.yaml giving {', '.join(fields)}.")
        )
        return {}
    try:
        document = load_mapping(TEST_FILE, text)
    except InvalidDefinition as invalid:
        problems.append(Problem(path=at, message=invalid.errors[0]["message"]))
        return {}
    found: dict[str, object] = {}
    for key in sorted(set(document) - set(fields)):
        problems.append(Problem(path=at, message=f"{key} is not a field of the workflow's tests."))
    for name, declared in fields.items():
        if name not in document:
            problems.append(Problem(path=at, message=f"{name} is not given."))
            continue
        problem = _scalar_problem(declared.type, declared.options, document[name])
        if problem is not None:
            problems.append(Problem(path=at, message=f"{name}: {problem}"))
            continue
        found[name] = document[name]
    return found


def _scalar_problem(kind: Type, options: Sequence[str] | None, value: object) -> str | None:
    match kind:
        case Type.TEXT:
            return None if isinstance(value, str) else "Must be text."
        case Type.NUMBER:
            return None if is_number(value) else "Must be a number."
        case Type.BOOLEAN:
            return None if isinstance(value, bool) else "Must be true or false."
        case Type.ENUM:
            return (
                None if value in (options or ()) else f"Must be one of {', '.join(options or ())}."
            )
    return "Must be a value of its type."


def group_problems(
    task: TaskDefinition, tests: Sequence[TestCase], paths: Collection[str]
) -> list[Problem]:
    """Check 5's groups and T2's test weights: every subfolder of `tests/` a
    key of `test_groups`, every key a subfolder holding a test, and every
    test weight naming a test of its group.
    """
    problems: list[Problem] = []
    if not any(path.startswith(TESTS_FOLDER) for path in paths):
        problems.append(
            Problem(
                path="test_groups",
                message="The task has no tests/ folder: add tests/<group>/<test>/.",
            )
        )
        return problems
    by_group: dict[str, list[str]] = {}
    for test in tests:
        by_group.setdefault(test.group, []).append(test.name)
    folders = {
        path.removeprefix(TESTS_FOLDER).split("/", 1)[0]
        for path in paths
        if path.startswith(TESTS_FOLDER) and "/" in path.removeprefix(TESTS_FOLDER)
    }
    folders = {folder for folder in folders if not folder.startswith(".")}
    test_folders: dict[str, set[str]] = {}
    for path in paths:
        parts = path.removeprefix(TESTS_FOLDER).split("/")
        if (
            path.startswith(TESTS_FOLDER)
            and len(parts) >= 3
            and not any(part.startswith(".") for part in parts)
        ):
            test_folders.setdefault(parts[0], set()).add(parts[1])
    for folder in sorted(folders - set(task.test_groups)):
        problems.append(
            Problem(
                path=f"tests/{folder}/",
                message=f"{folder} is a folder of tests/ but not a group in test_groups: list it, "
                "or move its tests.",
            )
        )
    for name, group in task.test_groups.items():
        if name not in folders:
            problems.append(
                Problem(
                    path=f"test_groups.{name}",
                    message=f"There is no folder tests/{name}/ with a test in it.",
                )
            )
            continue
        # A test folder `read_tests` refused for its layout is still a test
        # of its group here, so its problem is not said twice.
        if not by_group.get(name) and not test_folders.get(name):
            problems.append(
                Problem(path=f"test_groups.{name}", message=f"tests/{name}/ holds no test.")
            )
        for weighted in sorted((group.test_weights or {}).keys()):
            if weighted not in by_group.get(name, []) and weighted not in test_folders.get(
                name, set()
            ):
                problems.append(
                    Problem(
                        path=f"test_groups.{name}.test_weights.{weighted}",
                        message=f"{name}/{weighted} is not a test of {name}.",
                    )
                )
    return problems


# Checking a workflow against its primitives


@dataclass(frozen=True, slots=True)
class Kind:
    """What a value is, to check it against the port it is given to: its
    type, the options it may take when it is an enum, and where it came from.
    """

    type: Type
    options: frozenset[str] | None = None
    source: Literal["literal", "input", "contestant", "test", "step", "text"] = "literal"
    optional: bool = False


def check_workflow(
    workflow: WorkflowDefinition, primitives: Mapping[str, PrimitiveDeclaration]
) -> list[Problem]:
    """Every problem with the workflow as a version, each at its path in
    `workflow.yaml`. `primitives` holds the declaration of every `use:` that
    is a primitive, by the reference as text; a `use:` not in it is refused
    as not a primitive.
    """
    problems: list[Problem] = []
    outputs: dict[str, tuple[PrimitiveDeclaration, bool]] = {}
    for index, step in enumerate(workflow.steps):
        where: tuple[str | int, ...] = ("steps", index)
        declaration = primitives.get(str(step.use))
        if declaration is None:
            problems.append(
                Problem(path=path_text((*where, "use")), message=f"{step.use} is not a primitive.")
            )
            continue
        for name in sorted(set(step.with_) - set(declaration.inputs)):
            problems.append(
                Problem(
                    path=path_text((*where, "with", name)),
                    message=f"{step.use} has no input {name}.",
                )
            )
        for name, port in declaration.inputs.items():
            if not port.optional and name not in step.with_:
                problems.append(
                    Problem(
                        path=path_text((*where, "with")),
                        message=f"{step.use} needs the input {name}.",
                    )
                )
        raised = {source.input for source in declaration.limits_from.values()}
        for name, raw in step.with_.items():
            given = declaration.inputs.get(name)
            if given is None:
                continue
            try:
                _check_value(workflow, outputs, step.per_test, raw, given, name in raised)
            except _Refused as refused:
                problems.append(
                    Problem(path=path_text((*where, "with", name)), message=str(refused))
                )
        outputs[step.id] = (declaration, step.per_test)
    for name, entry in workflow.report.items():
        try:
            _check_report(entry, outputs)
        except _Refused as refused:
            problems.append(Problem(path=path_text(("report", name)), message=str(refused)))
    return problems


class _Refused(Exception):
    """One problem with a value, said in a sentence."""


def _kind_of(
    workflow: WorkflowDefinition,
    outputs: Mapping[str, tuple[PrimitiveDeclaration, bool]],
    per_test: bool,
    found: Reference,
) -> Kind:
    match found.kind:
        case "inputs":
            declared = workflow.inputs.get(found.name)
            if declared is None:
                raise _Refused(f"{found.name} is not an input of the workflow.")
            if declared.per_test and not per_test:
                raise _Refused(
                    f"{found.name} is given once per test, so only a per-test step reads it."
                )
            options = frozenset(declared.options) if declared.options else None
            source: Literal["input", "contestant"] = (
                "contestant" if declared.contestant else "input"
            )
            return Kind(declared.type, options, source, declared.optional)
        case "test":
            field_ = workflow.test.get(found.name)
            if field_ is None:
                raise _Refused(f"{found.name} is not a field of the workflow's tests.")
            if not per_test:
                raise _Refused("test.<field> is there only in a step that runs per test.")
            options = frozenset(field_.options) if field_.options else None
            return Kind(field_.type, options, "test")
    assert found.output is not None
    known = outputs.get(found.name)
    if known is None:
        raise _Refused(f"{found.name} is not a step before this one.")
    declaration, step_per_test = known
    port = declaration.outputs.get(found.output)
    if port is None:
        raise _Refused(f"The step {found.name} has no output {found.output}.")
    if step_per_test and not per_test:
        raise _Refused(
            f"The step {found.name} runs per test, and a step that runs once cannot read it."
        )
    if port.type is Type.OUTCOME:
        raise _Refused("A step's outcome is read by the harness, not given to another step.")
    options = frozenset(port.options) if port.options else None
    return Kind(port.type, options, "step", port.optional)


def _check_value(
    workflow: WorkflowDefinition,
    outputs: Mapping[str, tuple[PrimitiveDeclaration, bool]],
    per_test: bool,
    raw: object,
    port: Port,
    raises_limit: bool,
) -> None:
    kind = _raw_kind(workflow, outputs, per_test, raw)
    if raises_limit and kind.source in ("contestant", "step", "text"):
        if kind.source == "contestant":
            raise _Refused(
                "This port raises a limit, so the task must give it, not the contestant."
            )
        raise _Refused("This port raises a limit, so it must be known at the save.")
    if kind.optional and not port.optional:
        if kind.source == "step":
            raise _Refused("The output may be absent, so it feeds only an optional port.")
        raise _Refused("The input is optional, so it feeds only an optional port.")
    _fits(kind, port, raw)


def _raw_kind(
    workflow: WorkflowDefinition,
    outputs: Mapping[str, tuple[PrimitiveDeclaration, bool]],
    per_test: bool,
    raw: object,
) -> Kind:
    if isinstance(raw, bool):
        return Kind(Type.BOOLEAN)
    if is_number(raw):
        return Kind(Type.NUMBER)
    if raw is None or isinstance(raw, list | dict):
        raise _Refused(
            "Must be text, a number, true or false, or a ${{ }} reference; not a list, a "
            "mapping or nothing."
        )
    if not isinstance(raw, str):
        raise _Refused("Must be text, a number, true or false, or a ${{ }} reference.")
    try:
        whole = whole_reference(raw)
        written = references(raw) if whole is None else []
    except BadReference as bad:
        raise _Refused(str(bad)) from None
    if whole is not None:
        return _kind_of(workflow, outputs, per_test, whole)
    if not written:
        return Kind(Type.TEXT)
    for _, found in written:
        if found.kind == "steps":
            raise _Refused("A step's output is given whole, never written into text.")
        kind = _kind_of(workflow, outputs, per_test, found)
        if kind.type not in SCALARS:
            raise _Refused(
                f"{found} is a {kind.type}, and only text, a number, true or false or an enum "
                f"is written into text."
            )
        if kind.optional:
            raise _Refused(
                f"{found} is optional, so it is given whole to optional ports, never written "
                f"into text."
            )
    return Kind(Type.TEXT, source="text")


def _fits(kind: Kind, port: Port, raw: object) -> None:
    """Refuse a value of `kind` given to `port`, under the two widenings: a
    scalar fits a text port, and a file fits a folder port.
    """
    wanted = port.type
    if wanted is Type.TEXT and kind.type in SCALARS:
        return
    if wanted is Type.FOLDER and kind.type is Type.FILE:
        return
    if wanted is Type.ENUM:
        allowed = frozenset(port.options or ())
        if kind.type is Type.TEXT and kind.source == "literal":
            if raw in allowed:
                return
            raise _Refused(f"Takes one of {', '.join(port.options or ())}.")
        if kind.type is Type.ENUM and kind.options is not None:
            extra = sorted(kind.options - allowed)
            if not extra:
                return
            raise _Refused(f"Takes one of {', '.join(port.options or ())}, not {', '.join(extra)}.")
        raise _Refused(f"Takes one of {', '.join(port.options or ())}.")
    if kind.type is not wanted:
        raise _Refused(f"Takes {wanted}, not {kind.type}.")


def _check_report(
    entry: str | Meaning, outputs: Mapping[str, tuple[PrimitiveDeclaration, bool]]
) -> None:
    raw = entry if isinstance(entry, str) else entry.from_
    try:
        found = whole_reference(raw)
    except BadReference as bad:
        raise _Refused(str(bad)) from None
    if found is None or found.kind != "steps" or found.output is None:
        raise _Refused("Must be ${{ steps.<id>.<output> }}.")
    known = outputs.get(found.name)
    port = known[0].outputs.get(found.output) if known is not None else None
    if known is None or port is None:
        raise _Refused(f"steps.{found.name}.{found.output} is not an output of a step.")
    if port.type not in (Type.TEXT, Type.NUMBER):
        raise _Refused(
            f"steps.{found.name}.{found.output} is {port.type}; a report holds text or numbers."
        )
    if isinstance(entry, Meaning):
        if port.type is not Type.NUMBER and any(
            value is not None for value in (entry.fold, entry.better, entry.at_least, entry.at_most)
        ):
            raise _Refused(
                "fold, better, at_least and at_most say what a number means; this is text."
            )
        if not known[1] and (entry.fold is not None or entry.better is not None):
            raise _Refused(
                "fold and better are for a number reported per test; this step runs once."
            )


# Compiling a task


@dataclass(frozen=True, slots=True)
class Absent:
    """An optional input the task left out: the ports it feeds get nothing."""


ABSENT = Absent()


@dataclass(frozen=True, slots=True)
class Secret:
    name: str


@dataclass
class _Taint:
    """Where a value's content came from, for the sealed steps: from the
    contestant, from a task file the contestant is not served (`hidden`,
    naming it), or out of a sealed step (`sealed`, naming the nearest).
    """

    contestant: bool = False
    hidden: str | None = None
    sealed: str | None = None

    def merge(self, other: _Taint) -> None:
        self.contestant = self.contestant or other.contestant
        self.hidden = self.hidden or other.hidden
        self.sealed = self.sealed or other.sealed


@dataclass(frozen=True, slots=True)
class Compiled:
    """A compiled task: its plan, its sealed steps and what they hold back
    until the reveal, and what the save says of it beside publishing it.
    """

    plan: Plan
    sealed: tuple[str, ...]
    notes: tuple[str, ...] = ()
    held: Sealed = NOTHING_SEALED


@dataclass
class _Compiler:
    task: TaskDefinition
    workflow: WorkflowDefinition
    primitives: Mapping[str, PrimitiveDeclaration]
    paths: Collection[str]
    tests: Sequence[TestCase]
    secrets: Collection[str]
    machine: Machine
    problems: list[Problem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.values: dict[str, object] = {}
        self.taints: dict[str, _Taint] = {}
        self.sealed: dict[str, str] = {}
        """Each sealed step, with the task file it runs contestant code over,
        or that the sealed step it reads ran it over."""
        self.reads_sealed: dict[str, str] = {}
        """Each step sealed by reading a sealed step's output, with that step."""
        self.raised_by: dict[tuple[str, str], tuple[str, str]] = {}
        self.served = self._served()

    def refuse(self, path: str, message: str) -> None:
        self.problems.append(Problem(path=path, message=message))

    def _served(self) -> set[str]:
        """The test entries the workflow marks public, served to contestants."""
        return {
            path
            for test in self.tests
            for name, path in test.entries.items()
            if self.workflow.test[name].public
        }

    # check 4: the task's values

    def bind(self) -> None:
        declared = self.workflow.inputs
        for key in sorted(set(self.task.inputs) - set(declared)):
            self.refuse(f"inputs.{key}", f"The workflow {self.task.workflow} has no input {key}.")
        for name, wanted in declared.items():
            given = self.task.inputs.get(name, ABSENT if name not in self.task.inputs else None)
            at = f"inputs.{name}"
            if wanted.contestant:
                self._bind_contestant(at, name, wanted, given)
            else:
                self._bind_value(at, name, wanted, given)
        self._check_secrets()

    def _bind_contestant(self, at: str, name: str, wanted: WorkflowInput, given: object) -> None:
        if isinstance(given, Absent):
            return
        if not isinstance(given, dict):
            self.refuse(
                at,
                "The contestant gives this one: give its form details, such as {label: ...}, "
                "or leave it out.",
            )
            return
        for path, message in form_problems(given, wanted.type.value, wanted.options):
            self.refuse(path_text((at, *path)), message)

    def _bind_value(self, at: str, name: str, wanted: WorkflowInput, given: object) -> None:
        if isinstance(given, Absent):
            if wanted.optional:
                self.values[name] = ABSENT
            else:
                self.refuse(at, f"Give this input a value: a {_described(wanted.type)}.")
            return
        problem = self._value_problem(wanted, given)
        if problem is not None:
            self.refuse(at, problem)
            return
        if isinstance(given, dict):
            self.values[name] = Secret(str(given["secret"]))
        else:
            self.values[name] = given

    def _value_problem(self, wanted: WorkflowInput, given: object) -> str | None:
        match wanted.type:
            case Type.TEXT:
                if isinstance(given, dict):
                    secret = given.get("secret")
                    if (
                        set(given) != {"secret"}
                        or not isinstance(secret, str)
                        or not SECRET_NAME.match(secret)
                    ):
                        return "Must be text, or {secret: <name>} naming a secret the org holds."
                    if secret not in self.secrets:
                        return f"The org holds no secret named {secret}."
                    return None
                return None if isinstance(given, str) else "Must be text, or {secret: <name>}."
            case Type.NUMBER:
                return None if is_number(given) else "Must be a number."
            case Type.BOOLEAN:
                return None if isinstance(given, bool) else "Must be true or false."
            case Type.ENUM:
                options = wanted.options or ()
                return None if given in options else f"Must be one of {', '.join(options)}."
            case Type.FILE | Type.FOLDER:
                folder = wanted.type is Type.FOLDER
                problem = path_problem(given, folder=folder)
                if problem is not None:
                    return problem
                assert isinstance(given, str)
                if not has_path(self.paths, given):
                    return (
                        f"There is no file under {given} in the task."
                        if folder
                        else f"There is no file {given} in the task."
                    )
                return None
        return "Must be a value of its type."

    def _check_secrets(self) -> None:
        """Each input given as a secret is wired whole into ports marked
        secret, and into nothing else.
        """
        for name, value in self.values.items():
            if not isinstance(value, Secret):
                continue
            for index, step in enumerate(self.workflow.steps):
                declaration = self.primitives[str(step.use)]
                for port_name, raw in step.with_.items():
                    if not isinstance(raw, str):
                        continue
                    whole = whole_reference(raw)
                    used = [found for _, found in references(raw)] if whole is None else [whole]
                    if not any(found.kind == "inputs" and found.name == name for found in used):
                        continue
                    port = declaration.inputs[port_name]
                    if whole is None:
                        self.refuse(
                            f"inputs.{name}",
                            f"{name} is a secret, and steps[{index}] writes it into text; give the "
                            "secret to its own port.",
                        )
                    elif not port.secret or port.type is not Type.TEXT:
                        self.refuse(
                            f"inputs.{name}",
                            f"{name} is a secret, and steps[{index}] gives it to {port_name}, "
                            "which can hand the secret to the contestant's program.",
                        )

    # check 7: the steps

    def compile(self, harness_image: str) -> Plan | None:
        steps: list[PlanStep] = []
        for index, step in enumerate(self.workflow.steps):
            declaration = self.primitives[str(step.use)]
            made = self._step(index, step.id, step.use, step.per_test, step.with_, declaration)
            steps.extend(made)
        report = self._report()
        contestant = {
            name: ContestantInput(
                type=declared.type,
                options=declared.options,
                per_test=True if declared.per_test else None,
            )
            for name, declared in self.workflow.inputs.items()
            if declared.contestant
        }
        if self.problems:
            return None
        return Plan(
            harness_image=harness_image,
            tests=tuple(test.id for test in self.tests),
            contestant=contestant,
            steps=tuple(steps),
            report=report,
        )

    def _step(
        self,
        index: int,
        step_id: str,
        use: object,
        per_test: bool,
        given: Mapping[str, Any],
        declaration: PrimitiveDeclaration,
    ) -> list[PlanStep]:
        taint = _Taint()
        runs_contestant = False
        hidden_on: str | None = None
        items: list[tuple[TestCase | None, Inputs]] = []
        for test in self.tests if per_test else [None]:
            inputs: Inputs = {}
            for name, raw in given.items():
                port = declaration.inputs[name]
                value, value_taint = self._value(raw, port, test)
                if isinstance(value, Absent):
                    continue
                inputs[name] = value
                taint.merge(value_taint)
                if port.runs and value_taint.contestant:
                    runs_contestant = True
                if not port.secret and value_taint.hidden:
                    hidden_on = hidden_on or value_taint.hidden
            items.append((test, inputs))
        if runs_contestant and hidden_on is not None:
            self.sealed[step_id] = hidden_on
        elif taint.sealed is not None:
            self.sealed[step_id] = self.sealed[taint.sealed]
            self.reads_sealed[step_id] = taint.sealed
        if step_id in self.sealed:
            taint.sealed = step_id
        self.taints[step_id] = taint
        common = {
            "id": step_id,
            "primitive": str(use),
            "image": declaration.image,
            "network": declaration.network,
            "outputs": {
                f"{name}?" if port.optional else name: port.type.value
                for name, port in declaration.outputs.items()
            },
            "folders": tuple(
                sorted(
                    name
                    for name, port in declaration.inputs.items()
                    if port.type is Type.FOLDER and name in given
                )
            )
            or None,
        }
        if not per_test:
            ((_, inputs),) = items
            return [
                PlanStep(
                    **common, limits=self._limits(step_id, declaration, [inputs]), inputs=inputs
                )
            ]
        if declaration.batch:
            batch = tuple(
                BatchItem(test=test.id, inputs=inputs) for test, inputs in items if test is not None
            )
            limits = self._limits(step_id, declaration, [inputs for _, inputs in items])
            return [PlanStep(**common, limits=limits, batch=batch)]
        return [
            PlanStep(
                **common,
                limits=self._limits(step_id, declaration, [inputs]),
                inputs=inputs,
                test=test.id if test is not None else None,
            )
            for test, inputs in items
        ]

    def _value(
        self, raw: object, port: Port, test: TestCase | None
    ) -> tuple[dict[str, Any] | Absent, _Taint]:
        if isinstance(raw, bool) or is_number(raw):
            return {"value": spelled(raw) if port.type is Type.TEXT else raw}, _Taint()
        assert isinstance(raw, str)
        whole = whole_reference(raw)
        if whole is None:
            return self._written(raw, test), _Taint()
        return self._reference(whole, port, test)

    def _reference(
        self, found: Reference, port: Port, test: TestCase | None
    ) -> tuple[dict[str, Any] | Absent, _Taint]:
        match found.kind:
            case "inputs":
                declared = self.workflow.inputs[found.name]
                if declared.contestant:
                    value: dict[str, Any] = {"submission": found.name}
                    if port.type is Type.TEXT and declared.type is not Type.TEXT:
                        value = {"template": "{0}", "parts": [value]}
                    return value, _Taint(contestant=True)
                given = self.values.get(found.name)
                if isinstance(given, Absent):
                    return ABSENT, _Taint()
                if isinstance(given, Secret):
                    return {"secret": given.name}, _Taint()
                if declared.type in FILES:
                    path = str(given)
                    return {"task": path}, _Taint(hidden=self._hidden(path))
                return {"value": spelled(given) if port.type is Type.TEXT else given}, _Taint()
            case "test":
                assert test is not None
                if found.name in test.entries:
                    return {"task": test.entries[found.name]}, _Taint()
                scalar = test.scalars[found.name]
                return {"value": spelled(scalar) if port.type is Type.TEXT else scalar}, _Taint()
        taint = _Taint()
        taint.merge(self.taints.get(found.name, _Taint()))
        return {"step": found.name, "output": found.output}, taint

    def _hidden(self, path: str) -> str | None:
        """`path` when the contestant is not served it, none when they are."""
        if path.startswith(PUBLIC_FOLDER) or path in self.served:
            return None
        return path

    def _written(self, raw: str, test: TestCase | None) -> dict[str, Any]:
        """A string with references written into it: the task's and the
        test's scalars written in now, the contestant's as template parts.
        """
        text = ""
        parts: list[dict[str, str]] = []
        last = 0
        for match, found in references(raw):
            text += _escaped(raw[last : match.start()])
            last = match.end()
            declared = self.workflow.inputs.get(found.name) if found.kind == "inputs" else None
            if declared is not None and declared.contestant:
                text += f"{{{len(parts)}}}"
                parts.append({"submission": found.name})
            elif found.kind == "inputs":
                text += _escaped(spelled(self.values.get(found.name)))
            else:
                assert test is not None
                text += _escaped(spelled(test.scalars[found.name]))
        text += _escaped(raw[last:])
        if not parts:
            return {"value": text.replace("{{", "{").replace("}}", "}")}
        return {"template": text, "parts": parts}

    def _limits(
        self, step_id: str, declaration: PrimitiveDeclaration, runs: Sequence[Inputs]
    ) -> StepLimits:
        raised: list[dict[str, int]] = []
        for inputs in runs:
            limits = declaration.limits.as_mapping()
            for name, source in declaration.limits_from.items():
                given = inputs.get(source.input)
                if given is None or set(given) != {"value"} or not is_number(given["value"]):
                    continue
                value = given["value"]
                wanted = Decimal(repr(value)) * Decimal(repr(source.scale)) + Decimal(
                    repr(source.add)
                )
                ceiling = math.ceil(wanted)
                if ceiling > limits[name]:
                    limits[name] = ceiling
                    self.raised_by.setdefault((step_id, name), ("input", source.input))
            raised.append(limits)
        combined = {
            name: sum(run[name] for run in raised)
            if name in BATCH_SCALED
            else max(run[name] for run in raised)
            for name in LIMIT_NAMES
        }
        return StepLimits(**combined)

    def _report(self) -> dict[str, ReportEntry]:
        found: dict[str, ReportEntry] = {}
        for name, entry in self.workflow.report.items():
            raw = entry if isinstance(entry, str) else entry.from_
            reference = whole_reference(raw)
            assert reference is not None and reference.output is not None
            found[name] = ReportEntry(
                step=reference.name,
                output=reference.output,
                at_least=entry.at_least if isinstance(entry, Meaning) else None,
                at_most=entry.at_most if isinstance(entry, Meaning) else None,
            )
        return found


def _escaped(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _described(kind: Type) -> str:
    return {
        Type.TEXT: "text, or {secret: <name>}",
        Type.NUMBER: "number",
        Type.BOOLEAN: "true or false",
        Type.ENUM: "one of its options",
        Type.FILE: "file path in the task",
        Type.FOLDER: "folder path in the task, ending in /",
    }.get(kind, str(kind))


def compile_plan(
    task: TaskDefinition,
    workflow: WorkflowDefinition,
    primitives: Mapping[str, PrimitiveDeclaration],
    paths: Collection[str],
    tests: Sequence[TestCase],
    *,
    secrets: Collection[str],
    machine: Machine,
    harness_image: str,
) -> Compiled:
    """The task's plan. `workflow` checked as a version already
    (`check_workflow`), `primitives` holding the declaration of every
    primitive its steps use, `paths` every file of the state being saved,
    `tests` its tests in plan order, `secrets` the names of the secrets the
    task's org holds and `machine` the largest a run of it may take. Raises
    `InvalidDefinition` listing every problem, each at its path in
    `task.yaml` or at the test folder it is about.
    """
    compiler = _Compiler(task, workflow, primitives, paths, tests, secrets, machine)
    compiler.bind()
    if compiler.problems:
        raise InvalidDefinition("task.yaml", compiler.problems)
    plan = compiler.compile(harness_image)
    if plan is None:
        raise InvalidDefinition("task.yaml", compiler.problems)
    values = reported(workflow, primitives)
    problems = [*_fit(compiler, plan), *_credit(task, values), *_sealed(task, compiler)]
    if problems:
        raise InvalidDefinition("task.yaml", problems)
    once = {step.id for step in workflow.steps if not step.per_test}
    held = Sealed(
        stop=any(step in once for step in compiler.sealed),
        values=frozenset(
            name for name, entry in plan.report.items() if entry.step in compiler.sealed
        ),
    )
    return Compiled(
        plan,
        tuple(sorted(compiler.sealed)),
        (*_sealed_notes(compiler), *_credit_notes(task, values)),
        held,
    )


def _fit(compiler: _Compiler, plan: Plan) -> list[Problem]:
    """Check 7's fit: the run's whole time within the ceiling, and every
    step's memory and GPUs on the machine.
    """
    problems: list[Problem] = []
    seconds = sum(step.limits.time_ms for step in plan.steps) / 1000
    total = seconds + BASE_WALL.total_seconds() + STEP_OVERHEAD.total_seconds() * len(plan.steps)
    ceiling = WALL_CEILING.total_seconds()
    if total > ceiling:
        longest = max(plan.steps, key=lambda step: step.limits.time_ms)
        source = compiler.raised_by.get((longest.id, "time_ms"))
        name = source[1] if source is not None else None
        at = f"inputs.{name}" if name is not None and name in compiler.task.inputs else "workflow"
        said = name if name is not None else str(compiler.task.workflow)
        problems.append(
            Problem(
                path=at,
                message=f"{said} gives the run {math.ceil(total / 60)} minutes; a run may take "
                f"{int(ceiling // 60)}.",
            )
        )
    for step in plan.steps:
        for limit, most, unit in (
            ("memory_mb", compiler.machine.memory_mb, "MB of memory"),
            ("gpus", compiler.machine.gpus, "GPUs"),
        ):
            wanted = getattr(step.limits, limit)
            if wanted <= most:
                continue
            source = compiler.raised_by.get((step.id, limit))
            name = source[1] if source is not None else None
            at = (
                f"inputs.{name}"
                if name is not None and name in compiler.task.inputs
                else "workflow"
            )
            said = name if name is not None else f"step {step.id}"
            problems.append(
                Problem(
                    path=at,
                    message=f"{said} gives step {step.id} {wanted} {unit}; no machine this task "
                    f"may run on has more than {most}.",
                )
            )
    return _unique(problems)


@dataclass(frozen=True, slots=True)
class ReportedValue:
    """A reported value as the task's checks read it: per test or once, its
    port's type, and its meaning.
    """

    per_test: bool
    type: Type
    meaning: Meaning | None


def reported(
    workflow: WorkflowDefinition, primitives: Mapping[str, PrimitiveDeclaration]
) -> dict[str, ReportedValue]:
    """Every name the workflow reports, with whether it is per test, its
    type and its meaning.
    """
    steps = {step.id: step for step in workflow.steps}
    found: dict[str, ReportedValue] = {}
    for name, entry in workflow.report.items():
        raw = entry if isinstance(entry, str) else entry.from_
        reference = whole_reference(raw)
        if reference is None or reference.output is None or reference.name not in steps:
            continue
        step = steps[reference.name]
        port = primitives[str(step.use)].outputs[reference.output]
        found[name] = ReportedValue(
            step.per_test, port.type, entry if isinstance(entry, Meaning) else None
        )
    return found


def _better(meaning: Meaning, task: TaskDefinition) -> str | None:
    """A value's direction with the task's values in: `higher`, `lower`, or
    none when it declares none.
    """
    if meaning.better is None:
        return None
    found = whole_reference(meaning.better)
    if found is None:
        return meaning.better
    given = task.inputs.get(found.name)
    return given if isinstance(given, str) else None


def _credit(task: TaskDefinition, values: Mapping[str, ReportedValue]) -> list[Problem]:
    """T3: `credit` names a per-test number bounded 0 to 1 that is not lower
    is better, or, relative, one with a direction and a lower bound of at
    least 0.
    """
    if task.credit is None:
        return []
    if isinstance(task.credit, Relative):
        name = task.credit.relative
        value = values.get(name)
        meaning = value.meaning if value is not None else None
        if value is None or not _per_test_number(value) or meaning is None:
            return [
                Problem(
                    path="credit.relative",
                    message=(
                        f"{name} is not a number the workflow reports per test, with a direction."
                    ),
                )
            ]
        if _better(meaning, task) is None:
            return [
                Problem(
                    path="credit.relative",
                    message=f"{name} has no direction: its workflow declares no better.",
                )
            ]
        if meaning.at_least is None or meaning.at_least < 0:
            return [
                Problem(
                    path="credit.relative",
                    message=(
                        f"{name} is not bounded below by 0: its workflow declares no "
                        f"at_least of 0 or more."
                    ),
                )
            ]
        return []
    name = task.credit
    value = values.get(name)
    if value is None or not _per_test_number(value):
        return [
            Problem(path="credit", message=f"{name} is not a number the workflow reports per test.")
        ]
    meaning = value.meaning
    if (
        meaning is None
        or meaning.at_least is None
        or meaning.at_most is None
        or meaning.at_least < 0
        or meaning.at_most > 1
    ):
        return [
            Problem(
                path="credit",
                message=f"{name} is not a credit: its workflow does not bound it to 0 to 1.",
            )
        ]
    if _better(meaning, task) == "lower":
        return [
            Problem(path="credit", message=f"{name} is lower is better, so it is not a credit.")
        ]
    return []


def _per_test_number(value: ReportedValue) -> bool:
    return value.per_test and value.type is Type.NUMBER


def _credit_notes(task: TaskDefinition, values: Mapping[str, ReportedValue]) -> list[str]:
    """T4: a bounded per-test number credit does not name is said, not
    refused.
    """
    named = task.credit.relative if isinstance(task.credit, Relative) else task.credit
    notes = []
    for name, value in sorted(values.items()):
        meaning = value.meaning
        if not _per_test_number(value) or meaning is None or name == named:
            continue
        if (
            meaning.at_least is not None
            and meaning.at_most is not None
            and meaning.at_least >= 0
            and meaning.at_most <= 1
        ):
            if task.credit is None:
                notes.append(
                    f"{name} is reported, but credit is not set: an accepted test earns 1."
                )
            else:
                notes.append(f"{name} is reported, but credit names another value.")
    return notes


def _sealed(task: TaskDefinition, compiler: _Compiler) -> list[Problem]:
    """T5: a task whose workflow has a sealed step shows every group
    `after_close`.
    """
    if not compiler.sealed:
        return []
    step, data = sorted(compiler.sealed.items())[0]
    return [
        Problem(
            path=f"test_groups.{name}.show",
            message=f"{name} is shown {group.shown}, but step {step} gives the contestant's code "
            f"{data}; show it after_close, or serve the data under public/.",
        )
        for name, group in task.test_groups.items()
        if group.shown is not Show.AFTER_CLOSE
    ]


def _sealed_notes(compiler: _Compiler) -> list[str]:
    return [
        f"Step {step} is sealed: it reads what the sealed step "
        f"{compiler.reads_sealed[step]} wrote, so what it reports is shown at the reveal."
        if step in compiler.reads_sealed
        else f"Step {step} is sealed: it runs the contestant's code over {data}, which the "
        "contestant is not served, so what it reports is shown at the reveal."
        for step, data in sorted(compiler.sealed.items())
    ]


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
    """What a publication's grading depends on: its plan by path in the task
    repo, and a digest of each data file the plan names, by its path.
    """

    plans: Mapping[str, bytes]
    data: Mapping[str, str]


def _differences(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    changes: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            changes.append(f"{key} added")
        elif key not in after:
            changes.append(f"{key} removed")
        elif before[key] != after[key]:
            changes.append(f"{key} changed")
    return changes


def grading_changes(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """What changed how the task grades between the previous publication and
    this one, in words a person reads: `plans/plan.json changed`,
    `tests/main/1/input added`. Empty when nothing did, and for a first
    publication, which has nothing before it.
    """
    if before is None:
        return ()
    return (*_differences(before.plans, after.plans), *_differences(before.data, after.data))
