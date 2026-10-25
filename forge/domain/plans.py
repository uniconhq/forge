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
  machines it may run on (check 7), the credit checked against the report
  (T3, T4), and the plan checked against the runner's contract, so a plan
  the harness would refuse is never published.

A limit raised past what a run or a machine allows is refused where its
number came from: the task's input, the `test.yaml` of the test that gives
the most, or the workflow when it writes the number itself; the run ceiling
is blamed on the raised limit that adds the most time, and on the number of
tests when no raised limit adds any.

A test's id is `<group>/<test>`, and the plan lists every test, groups in
name order and tests in natural order within each (`2` before `10`), so
reordering `test_groups` changes no plan. A per-test step over a primitive
that takes a batch is one step with an item per test, and over any other
one entry per test. Either runs its tests in `run_order`: those of the
groups shown during the contest first, so a hidden test's run can tell no
shown one what it read or how long it took. Showing a group earlier or
later changes the order its tests run in, and nothing else.
"""

import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field, PlainValidator, model_validator

from forge.domain import exact_json
from forge.domain.blocks.numbers import Better, exact
from forge.domain.content import path_problem
from forge.domain.contracts import violation
from forge.domain.definitions import (
    PUBLIC_FOLDER,
    TESTS_FOLDER,
    Relative,
    Show,
    TaskDefinition,
    form_problems,
)
from forge.domain.grading import WALL_CEILING, Machine, wall_seconds
from forge.domain.meanings import Measure, ValueMeaning, in_sentence, with_measures
from forge.domain.plan_basics import (
    PRIMITIVE as PRIMITIVE,
)
from forge.domain.plan_basics import (
    SECRET_NAME as SECRET_NAME,
)
from forge.domain.plan_basics import (
    STEP_ID as STEP_ID,
)
from forge.domain.plan_basics import (
    Image as Image,
)
from forge.domain.plan_basics import (
    ReportEntry as ReportEntry,
)
from forge.domain.plan_basics import (
    Snapshot as Snapshot,
)
from forge.domain.plan_basics import (
    StepLimits as StepLimits,
)
from forge.domain.port_values import (
    Kind as Kind,
)
from forge.domain.port_values import (
    Refused as Refused,
)
from forge.domain.port_values import (
    a_type as a_type,
)
from forge.domain.port_values import (
    check_written as check_written,
)
from forge.domain.port_values import (
    escaped as escaped,
)
from forge.domain.port_values import (
    fits as fits,
)
from forge.domain.port_values import (
    literal_or_references as literal_or_references,
)
from forge.domain.port_values import (
    natural as natural,
)
from forge.domain.port_values import (
    spelled as spelled,
)
from forge.domain.primitives import (
    BATCH_SCALED,
    LIMIT_NAMES,
    FromInput,
    Port,
    PrimitiveDeclaration,
)
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
TEST_ID = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+$")
TEST_ID_MAX = 255
NAME = re.compile(r"^[A-Za-z0-9_-]+$")
TEST_FILE = "test.yaml"


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
TestId = Annotated[str, Field(pattern=TEST_ID.pattern, max_length=TEST_ID_MAX)]


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
        document = self.model_dump(exclude_none=True)
        return (exact_json.dumps(document, sort_keys=True, indent=2) + "\n").encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Plan:
        """The plan in a `plans/plan.json`, its numbers read exactly. Bytes
        that are not JSON are pydantic's `ValidationError`, as any other
        plan that does not validate.
        """
        try:
            document = exact_json.loads(data)
        except ValueError:
            return cls.model_validate_json(data)
        return cls.model_validate(document)

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
                    message=f"{where} can hold only folders: one for each test group, with "
                    "one for each test inside. Move this into a test's folder.",
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
                    path=where,
                    message=f"{bad_name[0]!r} cannot be used as a name. Use only letters, digits, "
                    "hyphens (-) and underscores (_).",
                )
            )
            continue
        if len(f"{group}/{test}") > TEST_ID_MAX:
            problems.append(
                Problem(
                    path=where,
                    message=f"A test's group and name together, <group>/<test>, can be at "
                    f"most {TEST_ID_MAX} characters, and these are {len(group) + 1 + len(test)}. "
                    "Shorten them.",
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
                    message=f"The test {group}/{test} has {found[0]}, which is none of the "
                    "workflow's test fields. Rename it after one, or remove it.",
                )
            )
            continue
        if len(found) > 1:
            problems.append(
                Problem(
                    path=where,
                    message=f"The test {group}/{test} has {name} more than once: "
                    f"{', '.join(found)}. Keep one.",
                )
            )
            continue
        is_folder = found[0].endswith("/")
        if is_folder != (declared.type is Type.FOLDER):
            wanted = "a folder" if declared.type is Type.FOLDER else "a file"
            problems.append(Problem(path=f"{where}{found[0]}", message=f"{name} must be {wanted}."))
            continue
        files[name] = f"{where}{found[0]}"
    for name, declared in fields.items():
        if declared.type in FILES and name not in files and name not in entries:
            problems.append(
                Problem(path=where, message=f"The test {group}/{test} has no {name}. Add it.")
            )
    has_yaml = "test" in entries and TEST_FILE in entries["test"]
    if has_yaml and not scalar_fields:
        problems.append(
            Problem(
                path=f"{where}{TEST_FILE}",
                message="The workflow's tests have no values besides their files, so this "
                "test has nothing to give here. Remove it.",
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
            Problem(
                path=where, message=f"This test needs values for {', '.join(fields)}. Add them."
            )
        )
        return {}
    try:
        document = load_mapping(TEST_FILE, text)
    except InvalidDefinition as invalid:
        problems.append(Problem(path=at, message=invalid.errors[0]["message"]))
        return {}
    found: dict[str, object] = {}
    for key in sorted(set(document) - set(fields)):
        problems.append(
            Problem(path=at, message=f"{key} is not one of the workflow's test fields. Remove it.")
        )
    for name, declared in fields.items():
        if name not in document:
            problems.append(Problem(path=at, message=f"This test needs a value for {name}."))
            continue
        problem = _scalar_problem(declared.type, declared.options, document[name])
        if problem is not None:
            problems.append(Problem(path=at, message=f"The value for {name} {problem}."))
            continue
        found[name] = document[name]
    return found


def _scalar_problem(kind: Type, options: Sequence[str] | None, value: object) -> str | None:
    match kind:
        case Type.TEXT:
            return None if isinstance(value, str) else "must be text"
        case Type.NUMBER:
            return None if is_number(value) else "must be a number"
        case Type.BOOLEAN:
            return None if isinstance(value, bool) else "must be true or false"
        case Type.ENUM:
            return (
                None if value in (options or ()) else f"must be one of {', '.join(options or ())}"
            )
    return "is not a value of its type"


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
                message="The task has no tests yet. Add a test to each test group.",
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
                message=f"{folder} holds tests but is not one of the task's test groups. Add "
                f"a test group called {folder}, or move its tests.",
            )
        )
    for name, group in task.test_groups.items():
        if name not in folders:
            problems.append(
                Problem(
                    path=f"test_groups.{name}",
                    message=f"The test group {name} has no tests. Add a test to it.",
                )
            )
            continue
        # A test folder `read_tests` refused for its layout is still a test
        # of its group here, so its problem is not said twice.
        if not by_group.get(name) and not test_folders.get(name):
            problems.append(
                Problem(
                    path=f"test_groups.{name}",
                    message=f"The test group {name} has no tests. Add a test to it.",
                )
            )
        for weighted in sorted((group.test_weights or {}).keys()):
            if weighted not in by_group.get(name, []) and weighted not in test_folders.get(
                name, set()
            ):
                problems.append(
                    Problem(
                        path=f"test_groups.{name}.test_weights.{weighted}",
                        message=f"{name}/{weighted} is not a test in the test group {name}. "
                        "Add the test, or remove its weight.",
                    )
                )
    return problems


# Checking a workflow against its primitives


def stray_report_units(
    workflow: WorkflowDefinition, primitives: Mapping[str, PrimitiveDeclaration]
) -> list[Problem]:
    """A unit on a reported value that is not a number, refused where a
    version is made or checked, naming the value, as `stray_units` does for
    an input. A version made with one before is read with it dropped
    (`value_meanings`), and a task on it saves, since a unit changes no
    plan. `primitives` holds the declaration of every `use:` that reads; an
    entry whose output cannot be found is refused by `check_workflow`.
    """
    problems: list[Problem] = []
    for name, entry in workflow.report.items():
        if not isinstance(entry, Meaning) or entry.unit is None:
            continue
        port = _reported_port(workflow, primitives, entry.from_)
        if port is not None and port.type is not Type.NUMBER:
            problems.append(
                Problem(
                    path=path_text(("report", name, "unit")),
                    message=f"Only a number takes a unit, and {name} is {a_type(port.type)}. "
                    "Remove the unit.",
                )
            )
    return problems


def _reported_port(
    workflow: WorkflowDefinition, primitives: Mapping[str, PrimitiveDeclaration], raw: str
) -> Port | None:
    """The output port a report entry's `from` reads, or none when it names
    no output of a step whose primitive reads.
    """
    try:
        found = whole_reference(raw)
    except BadReference:
        return None
    if found is None or found.kind != "steps" or found.output is None:
        return None
    step = next((step for step in workflow.steps if step.id == found.name), None)
    declaration = primitives.get(str(step.use)) if step is not None else None
    return declaration.outputs.get(found.output) if declaration is not None else None


RESERVED_FOLDERS = {
    "tests": "its tests",
    "public": "the files every contestant is served",
    "plans": "the plan Unicon compiles",
}
"""The folders of a task the format gives a meaning of its own, each with
what it holds."""


def reserved_inputs(workflow: WorkflowDefinition) -> list[Problem]:
    """An input whose id is a folder the task format gives meaning to,
    refused where a version is made or checked, naming it: a setting's own
    files go in a folder named by its id, which would then be mixed with
    the task's tests, the files served to contestants or the plan.
    """
    return [
        Problem(
            path=path_text(("inputs", key)),
            message=f"An input cannot be called {key}: a task's {key}/ folder already holds "
            f"{RESERVED_FOLDERS[key]}. Choose another name.",
        )
        for key in workflow.inputs
        if key in RESERVED_FOLDERS
    ]


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
                Problem(
                    path=path_text((*where, "use")),
                    message=f"{step.use} is not a primitive Unicon knows. Check its owner, name "
                    "and version.",
                )
            )
            continue
        if declaration.schema_version != 5:
            problems.append(
                Problem(
                    path=path_text((*where, "use")),
                    message=f"{step.use} is written for the run workflow's format, which this "
                    "workflow is not in. Use a version written for this format.",
                )
            )
            continue
        for name in sorted(set(step.with_) - set(declaration.inputs)):
            problems.append(
                Problem(
                    path=path_text((*where, "with", name)),
                    message=f"{step.use} has no input called {name}. Remove it.",
                )
            )
        for name, port in declaration.inputs.items():
            if not port.optional and name not in step.with_:
                problems.append(
                    Problem(
                        path=path_text((*where, "with", name)),
                        message=f"{step.use} needs a value for its input {name}.",
                    )
                )
        raised = {
            source.input
            for source in declaration.limits_from.values()
            if isinstance(source, FromInput)
        }
        for name, raw in step.with_.items():
            given = declaration.inputs.get(name)
            if given is None:
                continue
            try:
                _check_value(workflow, outputs, step.per_test, raw, given, name in raised)
            except Refused as refused:
                problems.append(
                    Problem(path=path_text((*where, "with", name)), message=str(refused))
                )
        outputs[step.id] = (declaration, step.per_test)
    for name, entry in workflow.report.items():
        try:
            _check_report(entry, outputs)
        except Refused as refused:
            problems.append(Problem(path=path_text(("report", name)), message=str(refused)))
    return problems


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
                raise Refused(f"The workflow has no input called {found.name}.")
            if declared.per_test and not per_test:
                raise Refused(
                    f"{found.name} is given once for each test, so only a step that runs for "
                    "each test can read it."
                )
            options = frozenset(declared.options) if declared.options else None
            source: Literal["input", "contestant"] = (
                "contestant" if declared.contestant else "input"
            )
            return Kind(declared.type, options, source, declared.optional)
        case "test":
            field_ = workflow.test.get(found.name)
            if field_ is None:
                raise Refused(f"{found.name} is not one of the workflow's test fields.")
            if not per_test:
                raise Refused("Only a step that runs for each test can read a test's fields.")
            options = frozenset(field_.options) if field_.options else None
            return Kind(field_.type, options, "test")
        case "workflow":
            raise Refused(
                "Only a workflow without a test block, which describes one run, reads its own "
                "files. Give this file as an input or a test field instead."
            )
    assert found.output is not None
    known = outputs.get(found.name)
    if known is None:
        raise Refused(f"There is no step called {found.name} before this one.")
    declaration, step_per_test = known
    port = declaration.outputs.get(found.output)
    if port is None:
        raise Refused(f"The step {found.name} has no output called {found.output}.")
    if step_per_test and not per_test:
        raise Refused(
            f"The step {found.name} runs for each test, so a step that runs once cannot read it."
        )
    if port.type is Type.OUTCOME:
        raise Refused(
            "A step's outcome is read by Unicon itself, and cannot be given to another step."
        )
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
            raise Refused("This value raises a limit, so the task must give it, not the entrant.")
        raise Refused("This value raises a limit, so it must be known when the task is saved.")
    if kind.optional and not port.optional:
        if kind.source == "step":
            raise Refused(
                "That output may be missing, so it can be given only to an optional port."
            )
        raise Refused("That input is optional, so it can be given only to an optional port.")
    fits(kind, port, raw)


def _raw_kind(
    workflow: WorkflowDefinition,
    outputs: Mapping[str, tuple[PrimitiveDeclaration, bool]],
    per_test: bool,
    raw: object,
) -> Kind:
    found = literal_or_references(raw)
    if isinstance(found, Kind):
        return found
    whole, written = found
    if whole is not None:
        return _kind_of(workflow, outputs, per_test, whole)
    if not written:
        return Kind(Type.TEXT)
    for reference in written:
        if reference.kind == "steps":
            raise Refused("A step's output can be given only whole, not written into text.")
        check_written(reference, _kind_of(workflow, outputs, per_test, reference))
    return Kind(Type.TEXT, source="text")


def _check_report(
    entry: str | Meaning, outputs: Mapping[str, tuple[PrimitiveDeclaration, bool]]
) -> None:
    raw = entry if isinstance(entry, str) else entry.from_
    try:
        found = whole_reference(raw)
    except BadReference as bad:
        raise Refused(str(bad)) from None
    if found is None or found.kind != "steps" or found.output is None:
        raise Refused("Give the step output this reports, as ${{ steps.<id>.<output> }}.")
    known = outputs.get(found.name)
    port = known[0].outputs.get(found.output) if known is not None else None
    if known is None or port is None:
        raise Refused(f"steps.{found.name}.{found.output} is not the output of any step.")
    if port.type not in (Type.TEXT, Type.NUMBER):
        raise Refused(
            f"steps.{found.name}.{found.output} is {a_type(port.type)}, and only text or a number "
            "can be reported."
        )
    if isinstance(entry, Meaning):
        if port.type is not Type.NUMBER and any(
            value is not None for value in (entry.fold, entry.better, entry.at_least, entry.at_most)
        ):
            raise Refused(
                "fold, better, at_least and at_most apply only to a number, and this is text."
            )
        if not known[1] and (entry.fold is not None or entry.better is not None):
            raise Refused(
                "fold and better apply only to a number reported for each test, and this "
                "step runs once."
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
class _Origin:
    """Where a number that raised a limit came from: a task input by its id,
    a scalar field of one test's `test.yaml`, or a literal the workflow
    gives a port, by the port's name; with the number itself.
    """

    kind: Literal["input", "test", "literal"]
    name: str
    value: object
    test: TestCase | None = None

    @property
    def path(self) -> str:
        """Where a refusal of the limit it raised is said."""
        if self.kind == "input":
            return f"inputs.{self.name}"
        if self.kind == "test" and self.test is not None:
            return f"tests/{self.test.group}/{self.test.name}/{TEST_FILE}"
        return "workflow"


@dataclass(frozen=True, slots=True)
class _Raised:
    """One limit of one run of a step raised above its primitive's: the
    step, the plan entry's test for a step with an entry per test, the test
    the run is for, the limit and its raised value, and where the number
    came from.
    """

    step: str
    entry: str | None
    test: TestCase | None
    limit: str
    value: int
    origin: _Origin


@dataclass(frozen=True, slots=True)
class Compiled:
    """A compiled task: its plan, its sealed steps and what they hold back
    until the reveal, what the save says of it beside publishing it, what
    each per-test value means, the workflow's title of each contestant
    input that has one, which the entrant's form names it by when the task
    gives no label, and every reported value in the workflow's order with
    the title and unit a page names it by (`value_meanings`).
    """

    plan: Plan
    sealed: tuple[str, ...]
    notes: tuple[str, ...] = ()
    held: Sealed = NOTHING_SEALED
    measures: Mapping[str, Measure] = field(default_factory=dict)
    titles: Mapping[str, str] = field(default_factory=dict)
    meanings: tuple[ValueMeaning, ...] = ()


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
        self.after_sealed: dict[str, str] = {}
        """Each step sealed by coming after a sealed step, with the first
        sealed step: whether it runs at all depends on whether that one
        stopped the run (#432)."""
        self.raised: list[_Raised] = []
        """Every limit raised above its primitive's, run by run."""
        self.runs_per_test: list[str] = []
        """Each per-test step that runs the contestant's code, in order."""
        self.run_rank = {test.id: at for at, test in enumerate(run_order(self.tests, self.task))}
        """Each test's place in the order a per-test step runs them."""
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
            self.refuse(
                f"inputs.{key}",
                f"The workflow {self.task.workflow} has no input called {key}. Remove it.",
            )
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
                "Entrants give this input themselves, so the task cannot give it a value. "
                "Set how it is asked for instead, such as its label, or leave it as it is.",
            )
            return
        for path, message in form_problems(given, wanted.type.value, wanted.options):
            self.refuse(path_text((at, *path)), message)

    def _bind_value(self, at: str, name: str, wanted: WorkflowInput, given: object) -> None:
        if isinstance(given, Absent):
            if wanted.optional:
                self.values[name] = ABSENT
            else:
                self.refuse(at, f"Give this input a value: {_described(wanted.type)}.")
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
                        return "Give text, or choose a secret the org holds."
                    if secret not in self.secrets:
                        return (
                            f"The org has no secret called {secret}. Choose one it has, or add "
                            "it to the org."
                        )
                    return None
                return (
                    None
                    if isinstance(given, str)
                    else "Give text, or choose a secret the org holds."
                )
            case Type.NUMBER:
                return None if is_number(given) else "This must be a number."
            case Type.BOOLEAN:
                return None if isinstance(given, bool) else "This must be true or false."
            case Type.ENUM:
                options = wanted.options or ()
                return None if given in options else f"This must be one of {', '.join(options)}."
            case Type.FILE | Type.FOLDER:
                folder = wanted.type is Type.FOLDER
                problem = path_problem(given, folder=folder, today=True)
                if problem is not None:
                    return problem
                assert isinstance(given, str)
                if not has_path(self.paths, given):
                    return (
                        f"The task has no files in {given}. Add them, or give another folder."
                        if folder
                        else f"The task has no file {given}. Add it, or give another file."
                    )
                return None
        return "This is not a value this input takes."

    def _check_secrets(self) -> None:
        """Each input given as a secret is wired whole into ports marked
        secret, and into nothing else.
        """
        for name, value in self.values.items():
            if not isinstance(value, Secret):
                continue
            for step in self.workflow.steps:
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
                            f"{name} is a secret, and the step {step.id} writes it into "
                            "text. Give a secret whole, to a port of its own.",
                        )
                    elif not port.secret or port.type is not Type.TEXT:
                        self.refuse(
                            f"inputs.{name}",
                            f"{name} is a secret, and the step {step.id} gives it to "
                            f"{port_name}, which could pass it on to the entrant's program.",
                        )

    # check 7: the steps

    def compile(self, harness_image: str) -> Plan | None:
        steps: list[PlanStep] = []
        for index, step in enumerate(self.workflow.steps):
            declaration = self.primitives[str(step.use)]
            made = self._step(index, step.id, step.use, step.per_test, step.with_, declaration)
            steps.extend(made)
        self._seal_what_follows()
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

    def _seal_what_follows(self) -> None:
        """Seal every step after the first sealed step in the workflow's
        order (#432): it runs only when that step did not stop the run, so
        its values being there, or its own stop, would tell the contestant
        whether it stopped.
        """
        order = [step.id for step in self.workflow.steps]
        first = next((at for at, step in enumerate(order) if step in self.sealed), None)
        if first is None:
            return
        for step in order[first + 1 :]:
            if step not in self.sealed:
                self.sealed[step] = self.sealed[order[first]]
                self.after_sealed[step] = order[first]

    def _in_run_order(
        self, items: Sequence[tuple[TestCase | None, Inputs]]
    ) -> list[tuple[TestCase, Inputs]]:
        return sorted(
            ((test, inputs) for test, inputs in items if test is not None),
            key=lambda item: self.run_rank[item[0].id],
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
        if per_test and runs_contestant:
            self.runs_per_test.append(step_id)
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
            limits = self._limits(step_id, None, declaration, given, items)
            return [PlanStep(**common, limits=limits, inputs=inputs)]
        if declaration.batch:
            batch = tuple(
                BatchItem(test=test.id, inputs=inputs) for test, inputs in self._in_run_order(items)
            )
            limits = self._limits(step_id, None, declaration, given, items)
            return [PlanStep(**common, limits=limits, batch=batch)]
        return [
            PlanStep(
                **common,
                limits=self._limits(step_id, test.id, declaration, given, [(test, inputs)]),
                inputs=inputs,
                test=test.id,
            )
            for test, inputs in self._in_run_order(items)
        ]

    def _value(
        self, raw: object, port: Port, test: TestCase | None
    ) -> tuple[dict[str, Any] | Absent, _Taint]:
        if isinstance(raw, bool) or is_number(raw):
            return {"value": spelled(raw) if port.type is Type.TEXT else raw}, _Taint()
        assert isinstance(raw, str)
        whole = whole_reference(raw)
        if whole is None:
            written = self._written(raw, test)
            # A contestant's value written into text is theirs as much as one
            # given whole (TASK-FORMAT.md section 3.5): it is a template part.
            return written, _Taint(contestant="parts" in written)
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
            text += escaped(raw[last : match.start()])
            last = match.end()
            declared = self.workflow.inputs.get(found.name) if found.kind == "inputs" else None
            if declared is not None and declared.contestant:
                text += f"{{{len(parts)}}}"
                parts.append({"submission": found.name})
            elif found.kind == "inputs":
                text += escaped(spelled(self.values.get(found.name)))
            else:
                assert test is not None
                text += escaped(spelled(test.scalars[found.name]))
        text += escaped(raw[last:])
        if not parts:
            return {"value": text.replace("{{", "{").replace("}}", "}")}
        return {"template": text, "parts": parts}

    def _limits(
        self,
        step_id: str,
        entry: str | None,
        declaration: PrimitiveDeclaration,
        given: Mapping[str, Any],
        runs: Sequence[tuple[TestCase | None, Inputs]],
    ) -> StepLimits:
        """The limits of one plan entry of a step over `runs`, each raised
        from the port its primitive names and rounded up, a batch's time
        summed over its items and the rest the largest of any item. Each
        raise is recorded with where its number came from, `given` being
        what the workflow gives the step's ports.
        """
        raised: list[dict[str, int]] = []
        assert declaration.limits is not None
        least = declaration.limits.as_mapping()
        for test, inputs in runs:
            # A sum or max over a programs port is contract 6's, which
            # `check_workflow` refuses in this format.
            numbers = {
                source.input: value["value"]
                for source in declaration.limits_from.values()
                if isinstance(source, FromInput)
                and (value := inputs.get(source.input)) is not None
                and set(value) == {"value"}
                and is_number(value["value"])
            }
            limits = declaration.item_limits(numbers)
            for name, source in declaration.limits_from.items():
                if isinstance(source, FromInput) and limits[name] > least[name]:
                    number = numbers[source.input]
                    origin = _origin_of(given.get(source.input), source.input, number, test)
                    self.raised.append(_Raised(step_id, entry, test, name, limits[name], origin))
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


def _origin_of(raw: object, port: str, number: object, test: TestCase | None) -> _Origin:
    """Where the number a port was given came from, `raw` being what the
    workflow wrote for the port.
    """
    found = whole_reference(raw) if isinstance(raw, str) else None
    if found is not None and found.kind == "inputs":
        return _Origin("input", found.name, number)
    if found is not None and found.kind == "test" and test is not None:
        return _Origin("test", found.name, number, test)
    return _Origin("literal", port, number)


def _described(kind: Type) -> str:
    return {
        Type.TEXT: "text, or a secret the org holds",
        Type.NUMBER: "a number",
        Type.BOOLEAN: "true or false",
        Type.ENUM: "one of its options",
        Type.FILE: "the path of a file in the task",
        Type.FOLDER: "the path of a folder in the task, ending in /",
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
    `task.yaml`, at the `workflow` line, or at the `test.yaml` of the test
    whose number raised a limit past what is allowed.
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
    broken = violation(exact_json.loads(plan.to_bytes()), "plan")
    if broken is not None:
        raise InvalidDefinition(
            "task.yaml",
            [
                Problem(
                    path="workflow",
                    message=f"Unicon cannot grade this task with {task.workflow}: the "
                    f"grading steps it makes break one of the runner's rules ({broken}).",
                )
            ],
        )
    once = {step.id for step in workflow.steps if not step.per_test}
    held = Sealed(
        steps=frozenset(step for step in compiler.sealed if step in once),
        values=frozenset(
            name for name, entry in plan.report.items() if entry.step in compiler.sealed
        ),
    )
    measured = measures(values, task)
    return Compiled(
        plan,
        tuple(sorted(compiler.sealed)),
        (*_sealed_notes(compiler), *_order_notes(compiler), *_credit_notes(task, values)),
        held,
        measured,
        {
            name: declared.title
            for name, declared in workflow.inputs.items()
            if declared.contestant and declared.title is not None
        },
        with_measures(value_meanings(workflow, values), measured),
    )


def _fit(compiler: _Compiler, plan: Plan) -> list[Problem]:
    """Check 7's fit: the run's whole time within the ceiling, and every
    step's memory, GPUs and network on the machine, each refused where the
    number that pushed it over came from.
    """
    problems: list[Problem] = []
    total = wall_seconds(plan)
    ceiling = WALL_CEILING.total_seconds()
    if total > ceiling:
        said = (
            f"{math.ceil(total / 60)} minutes, but a grading may take at most {int(ceiling // 60)}."
        )
        problems.append(_over_time(compiler, plan, said))
    machine = compiler.machine
    for step in plan.steps:
        for limit, most, unit in (
            ("memory_mb", machine.memory_mb, "MB of memory"),
            ("gpus", machine.gpus, "GPUs"),
        ):
            wanted = getattr(step.limits, limit)
            if wanted > most:
                said = (
                    f"the step {step.id} needs {wanted} {unit}, but no machine this task may "
                    f"run on has more than {most}."
                )
                problems.append(_over_machine(compiler, step, limit, wanted, said))
        if step.network and not machine.network:
            problems.append(
                Problem(
                    path="workflow",
                    message=f"The step {step.id} uses {step.primitive}, which needs the "
                    "network, but no machine this task may run on gives a step the network.",
                )
            )
    return _unique(problems)


def _over_time(compiler: _Compiler, plan: Plan, said: str) -> Problem:
    """The run ceiling's refusal, blamed on the raised time limit that adds
    the most time to the run, or on the number of tests when none is raised.
    `said` is how long a grading takes and may take.
    """
    by_origin: dict[tuple[str, str], list[_Raised]] = {}
    for raised in compiler.raised:
        if raised.limit == "time_ms":
            by_origin.setdefault((raised.origin.kind, raised.origin.name), []).append(raised)
    if not by_origin:
        count = len(plan.tests)
        return Problem(
            path="workflow",
            message=f"{_tests(count)} through the steps of {compiler.task.workflow} "
            f"{_make(count)} a grading take {said}",
        )
    runs = max(by_origin.values(), key=lambda runs: sum(run.value for run in runs))
    tests = sorted({run.test.id for run in runs if run.test is not None}, key=natural)
    most = max(runs, key=lambda run: run.value)
    origin = most.origin
    written = f"{origin.name} at {spelled(origin.value)}"
    if origin.kind == "test":
        assert origin.test is not None
        if len(tests) == 1:
            return Problem(
                path=origin.path,
                message=f"With {written}, the test {origin.test.id} makes a grading take {said}",
            )
        return Problem(
            path=origin.path,
            message=f"With {origin.name} up to {spelled(origin.value)} (in {origin.test.id}), "
            f"{_tests(len(tests))} make a grading take {said}",
        )
    if origin.kind == "literal":
        written = f"{written}, as {compiler.task.workflow} sets it"
    if not tests:
        return Problem(path=origin.path, message=f"With {written}, a grading takes {said}")
    return Problem(
        path=origin.path,
        message=f"With {written}, {_tests(len(tests))} {_make(len(tests))} a grading take {said}",
    )


def _over_machine(
    compiler: _Compiler, step: PlanStep, limit: str, wanted: int, said: str
) -> Problem:
    """A refusal of a step's memory or GPUs beyond the machine, at where the
    number that raised it came from, or at the workflow when its primitive
    asks that much itself.
    """
    origin = next(
        (
            raised.origin
            for raised in compiler.raised
            if (raised.step, raised.entry, raised.limit, raised.value)
            == (step.id, step.test, limit, wanted)
        ),
        None,
    )
    if origin is None:
        return Problem(path="workflow", message=f"As {step.primitive} sets it, {said}")
    written = f"{origin.name} at {spelled(origin.value)}"
    if origin.kind == "test":
        assert origin.test is not None
        return Problem(
            path=origin.path, message=f"With {written} in the test {origin.test.id}, {said}"
        )
    if origin.kind == "literal":
        written = f"{written}, as {compiler.task.workflow} sets it"
    return Problem(path=origin.path, message=f"With {written}, {said}")


def _tests(count: int) -> str:
    return "1 test" if count == 1 else f"{count} tests"


def _make(count: int) -> str:
    return "makes" if count == 1 else "make"


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


def value_meanings(
    workflow: WorkflowDefinition, values: Mapping[str, ReportedValue]
) -> tuple[ValueMeaning, ...]:
    """Every value the workflow reports, in its order, with the title and
    unit a page names it by: a unit on a value that is not a number, which a
    version made before it was refused may hold, is dropped.
    """
    found: list[ValueMeaning] = []
    for name, entry in workflow.report.items():
        value = values.get(name)
        if value is None:
            continue
        if not isinstance(entry, Meaning):
            found.append(ValueMeaning(name))
            continue
        unit = entry.unit if value.type is Type.NUMBER else None
        found.append(ValueMeaning(name, entry.title, unit))
    return tuple(found)


def measures(values: Mapping[str, ReportedValue], task: TaskDefinition) -> dict[str, Measure]:
    """What each per-test number the workflow reports means for this task:
    its fold, its direction with the task's values in, and its bounds, each
    bound the exact rational of the decimal it is written as.
    """
    found: dict[str, Measure] = {}
    for name, value in sorted(values.items()):
        if not _per_test_number(value):
            continue
        meaning = value.meaning
        if meaning is None:
            found[name] = Measure()
            continue
        better = _better(meaning, task)
        found[name] = Measure(
            fold=meaning.fold,
            better=Better(better) if better in ("higher", "lower") else None,
            at_least=exact(meaning.at_least),
            at_most=exact(meaning.at_most),
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
        said = _said(name, value)
        meaning = value.meaning if value is not None else None
        if value is None or not _per_test_number(value) or meaning is None:
            return [
                Problem(
                    path="credit.relative",
                    message=(
                        f"The workflow does not report {said} as a number for each test with a "
                        "direction, higher or lower, that is better. Choose another value."
                    ),
                )
            ]
        if _better(meaning, task) is None:
            return [
                Problem(
                    path="credit.relative",
                    message=f"The workflow does not say whether a higher or a lower {said} "
                    "is better, so it cannot be measured against the best. Choose another value.",
                )
            ]
        if meaning.at_least is None or meaning.at_least < 0:
            return [
                Problem(
                    path="credit.relative",
                    message=(
                        f"The workflow does not say that {said} is never below 0. Choose "
                        "another value."
                    ),
                )
            ]
        return []
    name = task.credit
    value = values.get(name)
    said = _said(name, value)
    if value is None or not _per_test_number(value):
        return [
            Problem(
                path="credit",
                message=f"The workflow does not report {said} as a number for each test. "
                "Choose another value.",
            )
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
                message=f"The workflow does not keep {said} between 0 and 1, so it cannot be "
                "what a test earns. Choose another value.",
            )
        ]
    if _better(meaning, task) == "lower":
        return [
            Problem(
                path="credit",
                message=f"For {said}, lower is better, so it cannot be what a test earns. Choose "
                "another value.",
            )
        ]
    return []


def _per_test_number(value: ReportedValue) -> bool:
    return value.per_test and value.type is Type.NUMBER


def _said(name: str, value: ReportedValue | None) -> str:
    """A reported value as a sentence names it: by the title the workflow's
    report gives it, "run time", or by its key where it gives none (M58).
    """
    title = value.meaning.title if value is not None and value.meaning is not None else None
    return name if title is None else in_sentence(title)


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
                    f"The workflow reports {_said(name, value)}, a score from 0 to 1 for each "
                    "test, but the task does not use it: an accepted test earns 1."
                )
            else:
                notes.append(
                    f"The workflow reports {_said(name, value)}, a score from 0 to 1 for each "
                    "test, but the task counts another value instead."
                )
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
            message=f"{name} shows entrants its results {_SHOWN[group.shown]}, but the step "
            f"{step} runs the entrant's code on {data}, which entrants do not see. Show this "
            "group's results only at the reveal, or put that data in the task's public files.",
        )
        for name, group in task.test_groups.items()
        if group.shown is not Show.AFTER_CLOSE
    ]


_SHOWN = {
    Show.ALWAYS: "as soon as a grading ends",
    Show.VERDICT: "as an outcome before the reveal",
    Show.AFTER_CLOSE: "at the reveal",
}


def _sealed_notes(compiler: _Compiler) -> list[str]:
    return [
        f"The step {step} runs after {compiler.after_sealed[step]}, which uses data entrants "
        f"do not see, so whether {step} ran, and what it reports, is shown only at the reveal."
        if step in compiler.after_sealed
        else f"The step {step} reads what {compiler.reads_sealed[step]} gives, which comes from "
        "data entrants do not see, so what it reports is shown only at the reveal."
        if step in compiler.reads_sealed
        else f"The step {step} runs the entrant's code on {data}, which entrants do not see, "
        "so what it reports is shown only at the reveal."
        for step, data in sorted(compiler.sealed.items())
    ]


_RUN_RANK = {Show.ALWAYS: 0, Show.VERDICT: 1, Show.AFTER_CLOSE: 2}
"""Which tests a per-test step runs first: those entrants see everything of
during the contest, then those they see only the outcome and points of,
then those they see nothing of until the reveal."""


def run_order(tests: Sequence[TestCase], task: TaskDefinition) -> list[TestCase]:
    """`tests` in the order a per-test step runs them: those of `always`
    groups first, then `verdict`, then `after_close`, each part in plan order.
    A run can tell only the runs after it what it read or how long it took,
    through what its container keeps between items
    (`findings/batch-channels-test.md`), so no test runs before one whose
    values entrants see sooner. A `verdict` test may tell another `verdict`
    test, since entrants see only their outcomes.
    """
    return sorted(tests, key=lambda test: _rank(test.group, task))


def _rank(group: str, task: TaskDefinition) -> int:
    found = task.test_groups.get(group)
    return 0 if found is None else _RUN_RANK[found.shown]


def _order_notes(compiler: _Compiler) -> list[str]:
    """The one leak `run_order` cannot close in one step: a second per-test
    step running the contestant's code runs the tests entrants see sooner
    after the first step's tests they see later. Said, not refused, so no
    saved task stops saving.
    """
    ranks = {_RUN_RANK[group.shown] for group in compiler.task.test_groups.values()}
    if len(compiler.runs_per_test) < 2 or len(ranks) < 2:
        return []
    first, second = compiler.runs_per_test[:2]
    return [
        f"The workflow runs the entrant's code in two steps for each test, {first} and "
        f"{second}. A run in {first} of a test whose results entrants see later can tell a run "
        f"in {second} of a test whose results they see sooner how long it took. Show every "
        "group the same way to avoid it."
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


def _differences(
    before: Mapping[str, object],
    after: Mapping[str, object],
    named: Callable[[str], str],
    why: Callable[[object, object], list[str]] | None = None,
) -> list[str]:
    changes: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            changes.append(f"{named(key)} was added.")
        elif key not in after:
            changes.append(f"{named(key)} was removed.")
        elif not _same(before[key], after[key]):
            changes.append(f"{named(key)} changed.")
            if why is not None:
                changes += why(before[key], after[key])
    return changes


def _plan_named(path: str) -> str:
    return "The way the task grades"


def _file_named(path: str) -> str:
    """A data file in words a person reads: a test's entry by its field,
    its test and its group, `tests/main/1/input.txt` being the input of test
    1 in the group main; any other by its path.
    """
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] == "tests":
        _, group, test, entry = parts[:4]
        field = entry.split(".", 1)[0]
        return f"The {field} of test {test} in the group {group}"
    return f"The file {path}"


def _by_name(data: Mapping[str, str]) -> dict[str, list[str]]:
    """Each data file's digests under the name `_file_named` gives it, so a
    test entry renamed with what it holds unchanged is no change of it.
    """
    named: dict[str, list[str]] = {}
    for path in sorted(data):
        named.setdefault(_file_named(path), []).append(data[path])
    return named


def _moved_images(before: object, after: object) -> list[str]:
    """Each primitive version two plans both run whose image differs between
    them: a release moved that version's image in place, so a save picks it
    up though nothing the organiser wrote changed it.
    """
    old, new = _images(before), _images(after)
    return [
        f"A release updated {ref.partition('/')[2]} in place."
        for ref in sorted(old.keys() & new.keys())
        if old[ref] != new[ref]
    ]


def _images(plan: object) -> dict[str, str]:
    """The image each primitive version a plan's steps run is pinned to, or
    nothing for a plan that does not read.
    """
    if not isinstance(plan, bytes):
        return {}
    try:
        document = exact_json.loads(plan)
    except ValueError:
        return {}
    steps = document.get("steps") if isinstance(document, dict) else None
    return {
        step["primitive"]: step["image"]
        for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, dict)
        and isinstance(step.get("primitive"), str)
        and isinstance(step.get("image"), str)
    }


def _same(before: object, after: object) -> bool:
    """Whether two plans, or two digests, are the same: a plan by what it
    says, so a number written `2.50` in one and `2.5` in the other is no
    change, nor the order a step runs its tests in, which changes no test's
    outcome or values (`run_order`); anything that does not read as JSON by
    its bytes.
    """
    if before == after:
        return True
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        return False
    try:
        return bool(
            _in_test_order(exact_json.loads(before)) == _in_test_order(exact_json.loads(after))
        )
    except ValueError:
        return False


def _in_test_order(plan: object) -> object:
    """`plan` with each batch's items sorted by test, and the per-test entries
    of each step gathered where that step first appears and sorted by test,
    so two plans differing only in the order their steps run their tests
    read the same. The compiler writes a step's entries together, so
    gathering them merges nothing two different plans would keep apart.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return plan

    def test_of(entry: object) -> str:
        return str(entry.get("test", "")) if isinstance(entry, dict) else ""

    steps = [
        {**entry, "batch": sorted(entry["batch"], key=test_of)}
        if isinstance(entry, dict) and isinstance(entry.get("batch"), list)
        else entry
        for entry in plan["steps"]
    ]
    first: dict[str, int] = {}
    for at, entry in enumerate(steps):
        first.setdefault(str(entry.get("id")) if isinstance(entry, dict) else "", at)

    def place(entry: object) -> tuple[int, str]:
        step_id = str(entry.get("id")) if isinstance(entry, dict) else ""
        return first[step_id], test_of(entry)

    return {**plan, "steps": sorted(steps, key=place)}


def grading_changes(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """What changed how the task grades between the previous publication and
    this one, in words a person reads: `The way the task grades changed.`,
    `The input of test 1 in the group main was added.`, and after a changed plan each
    primitive version whose image a release moved, `A release updated
    sandbox-run@v2 in place.` Empty when nothing did, and for a first
    publication, which has nothing before it.
    """
    if before is None:
        return ()
    return (
        *_differences(before.plans, after.plans, _plan_named, _moved_images),
        *_differences(_by_name(before.data), _by_name(after.data), str),
    )
