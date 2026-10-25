"""The plan a run task's save makes (SIMPLER-CORE.md 11.1 to 11.4, 15.4 and
15.5), plan schema version 6, and what a save compares to say what changed
in it.

A run task's plan is its run workflow bound to the task's values, run by
run, so the forge never reads a workflow, a primitive's declaration or
`task.yaml` at grade time and fills in only the entrant's values. It pins
the workflow by its commit, and every file a step is given by path, size
and, for an upload, its SHA-256. It is written into the task repo as
`plans/plan.json`, beside `plans/runs.json`, in the commit a publication
names.

Each step is placed by what it reads, directly or through earlier steps
(11.1): a step reading nothing of the entrant's runs at the save, once, or
once per run when it reads a repeated input; one reading the entrant's runs
once per grading, or once per run of each grading. A run's verdict is the
outcome of its first step that was not accepted, so every run reads every
step done once per grading. A grading is two pipelines (11.3): the early one
holds the early runs and, when any run is early, every step done once per
grading; when none is, but a value shown `now` reads `compiled`, the steps
done once per grading that can give a compile error and are not tainted,
with what they read. The late one holds the rest. Steps done at the save are
a pipeline of their own. A grading made while the task's results are
released counts what `on_release` lists as shown during the contest too,
so the plan holds that split as well, where it differs (`released`), and
each grading reads the one it was made with (`RunPlan.for_split`). Each
pipeline lists its Woodpecker steps: one per step done once, one per step
of a primitive that takes a batch holding the pipeline's runs of it in run
order, and one per run of any other step, each with `depends_on` from the
wiring and the container's limits, time summed over its runs and the rest
the most of any, memory with room for the files `exec` and `interact` copy
into their boxes, since the container holds what a step copies.

A value's `exec` program adds two steps no workflow names, each marked
with its value (`RunStep.value`): `build-<value>`, compiling its folder at
the save, and `exec-<value>`, run once per grading at the end of the
pipeline its value is shown from, after every step there, on the runs
file the forge builds from its group's runs (`RUNS_FILE`), at most
`RUN_ENTRY_KB` a run. Neither is any run's step.

Each pipeline runs in one slot of a grading machine (`Slot`), and the
steps Woodpecker runs side by side share its memory. So a pipeline is laid
out in stages, each step waiting for every step of the stage before it,
for the smallest slot that holds its largest step: a stage holds steps in
order while their memory adds up within the slot, and a step that runs no
boxes, whose container may use the whole slot, is a stage alone
(`_chained`). The pipeline's `size` label is the smallest slot that holds
the most its steps can use at once (`slot_of`).

A task's checks (SIMPLER-CORE.md 9.6) are graded in the save's pipeline:
each check copies every step graded per submission (`RunStep.check`),
named `c<check>-<step>`, given the check's files and values where the
entrant's would be, done for every run, once the save's own steps and the
check before it have finished, so the save's pipeline holds the whole
grading of each check (`_check_copies`). No grading reads a copy, and a
file only a copy is given is not one the grading depends on
(`RunPlan.task_paths`).

A step is tainted (11.4) when it runs the entrant's code and gets, directly
or through earlier steps, a file entrants are not shown during the contest
on a port not marked secret: a task file `show` does not list under `now`
or `on_release`, a run's own files among them, or a file of the workflow. A
step reading a tainted step is tainted too, and scalars never taint. The
save refuses a value or group shown during the contest that reads runs
behind a tainted step done once per grading, and notes one that reads runs
whose own steps are tainted (15.4).
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, PlainValidator

from forge.domain import exact_json
from forge.domain.checks import (
    CHECKS_ROOM,
    COPY_MOST,
    RUNS_KEPT,
    TaskCheck,
    check_paths,
    check_room,
    copy_of,
)
from forge.domain.content import named_by
from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE, VALUE_NAME, Scoring, TaskDefinition
from forge.domain.grading import WALL_CEILING, Machine, Slot, wall_of
from forge.domain.plan_basics import (
    PLAN_PATH,
    PRIMITIVE,
    STEP_ID,
    Image,
    ReportEntry,
    Snapshot,
    StepLimits,
)
from forge.domain.port_values import escaped, spelled
from forge.domain.primitives import (
    BATCH_SCALED,
    LIMIT_NAMES,
    PROGRAMS,
    FromInput,
    FromMax,
    FromSum,
    Port,
    PrimitiveDeclaration,
)
from forge.domain.run_task import RUN_ID, SHOWN_WORDS, Part, RunTask, file_time, show_names
from forge.domain.run_workflow import ResolvedStep, RunWorkflowCheck
from forge.domain.types import FILES, Type
from forge.domain.workflow_definition import (
    SELF_REPORTED,
    BadReference,
    Reference,
    RunWorkflow,
    references,
    whole_reference,
)
from forge.domain.yaml_models import Model, Path, Problem, is_number, path_text

SCHEMA_VERSION: Literal[6] = 6
StepClass = Literal["save_once", "save_per_run", "grading_once", "grading_per_run"]
PipelineKind = Literal["save", "early", "late"]
PIPELINES: tuple[PipelineKind, ...] = ("save", "early", "late")
GradingKind = Literal["early", "late"]
"""The pipelines of a grading, which a release may lay out otherwise."""
RELEASE_MOVED = (
    "What a release shows moved, so a grading made while results are released runs other runs "
    "early."
)
"""The words a save says when it moves only the released split, which
grades nothing again while the task's results are not released."""
COMPILE_USE = "unicon/compile@v3"
EXEC_USE = "unicon/exec@v1"
PROGRAM_USES = (COMPILE_USE, EXEC_USE)
"""The primitives a value's `exec` program is compiled and run with, which
a save reads when a value has one."""
RUNS_FILE = "runs.json"
"""The name a value's program reads its group's runs under."""
RUN_ENTRY_KB = 16
"""The most one run takes in a program's runs file, which bounds the file
as its box holds it."""
ONCE: tuple[StepClass, ...] = ("save_once", "grading_once")
CLASS_WORDS: dict[StepClass, str] = {
    "save_once": "once, at the save",
    "save_per_run": "once per run, at the save",
    "grading_once": "once per grading",
    "grading_per_run": "once per run of each grading",
}
_READS: dict[StepClass, tuple[bool, bool]] = {
    "save_once": (False, False),
    "save_per_run": (False, True),
    "grading_once": (True, False),
    "grading_per_run": (True, True),
}
"""What a step of each class reads: the entrant's inputs, and a repeated
input."""
SHOWN_DURING = ("now", "on_release")
"""The times of `show` that count as during the contest: a release has no
time, so it is refused as if shown `now`, though it makes no run early."""
BOXED: dict[str, tuple[str, ...]] = {
    "unicon/exec": ("program", "files", "loads"),
    "unicon/interact": ("program", "files"),
}
"""The primitives that run programs in isolate boxes, by name, with the
ports whose files each copies into a box's tmpfs, which is charged to the
container and not the box: `exec`'s own ports, and each program's of
`interact`."""
PAGE_KB = 4
PRIMITIVE_ROOM_MB = 112
"""What a boxed primitive's container keeps for the step wrapper, the
primitive itself and isolate beside its boxes' memory limits and the files
it copies into them: an item needing more of the container than its
memory, less this, is refused before any program runs (`STEP_MEMORY_KB` in
`exec` and `interact`, and their READMEs). The planner gives each
container at least the three together."""
BOX_MEMORY = "memory_limit"
"""The input, of a boxed primitive or of each of its programs, that gives a
box's memory limit in MB."""
COMPILE_ERROR = "compile_error"
COMPILED = "compiled"
_UNSHOWN = ", which entrants are not shown during the contest, so {entry} could tell them about it"
PIPELINE_WORDS: dict[PipelineKind, str] = {
    "save": "The steps done at the save",
    "early": "The steps of a grading's early pipeline",
    "late": "The steps of a grading's late pipeline",
}
SHA256 = r"^[0-9a-f]{64}$"
COMMIT = r"^[0-9a-f]{40}([0-9a-f]{24})?$"
OUTPUT = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$"
PIPELINE_STEP = r"^[a-z0-9][a-z0-9_-]*(\.[A-Za-z0-9_-]+)?$"


def _file_value(value: object) -> bool:
    return (
        isinstance(value, dict)
        and len(value) == 1
        and any(isinstance(value.get(key), str) for key in ("task", "workflow"))
    )


def _entrant_value(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("entrant"), str):
        return False
    part = value.get("part")
    return set(value) == {"entrant"} or (
        set(value) == {"entrant", "part"}
        and isinstance(part, int)
        and not isinstance(part, bool)
        and part >= 1
    )


def _output_value(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"step", "output"}
        and all(isinstance(value[key], str) for key in value)
    )


def _runs_value(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"runs"}
        and isinstance(value["runs"], list)
        and bool(value["runs"])
        and all(isinstance(each, str) and RUN_ID.fullmatch(each) for each in value["runs"])
        and len(set(value["runs"])) == len(value["runs"])
    )


def _run_value(value: object) -> dict[str, Any]:
    """One value of a step's inputs, in exactly one of the plan's shapes."""
    if not isinstance(value, dict):
        raise ValueError("a value is a mapping")
    keys = set(value)
    if keys == {"value"} and (isinstance(value["value"], str | bool) or is_number(value["value"])):
        return value
    if _file_value(value) or _entrant_value(value) or _output_value(value):
        return value
    if keys == {"secret"} and isinstance(value["secret"], str):
        return value
    if (
        keys == {"template", "parts"}
        and isinstance(value["template"], str)
        and isinstance(value["parts"], list)
        and value["parts"]
        and all(_entrant_value(part) for part in value["parts"])
    ):
        return value
    if keys == {"files"} and isinstance(value["files"], dict):
        for each in value["files"].values():
            if not (
                _file_value(each)
                or _entrant_value(each)
                or _output_value(each)
                or _runs_value(each)
            ):
                raise ValueError(f"{each!r} is not a file a plan holds")
        return value
    if keys == {"programs"} and isinstance(value["programs"], dict) and value["programs"]:
        for inputs in value["programs"].values():
            if not isinstance(inputs, dict):
                raise ValueError("a program's inputs are a mapping")
            for each in inputs.values():
                _run_value(each)
        return value
    if keys == {"links"} and isinstance(value["links"], list):
        return value
    raise ValueError(f"{value!r} is not a value a plan holds")


def _taint(value: object) -> dict[str, Any]:
    if isinstance(value, dict) and set(value) == {"beside"} and _file_value(value["beside"]):
        return value
    if isinstance(value, dict) and set(value) == {"reads"} and isinstance(value["reads"], str):
        return value
    raise ValueError(f"{value!r} is not why a step is tainted")


Value = Annotated[dict[str, Any], PlainValidator(_run_value)]
Inputs = dict[str, Value]
Taint = Annotated[dict[str, Any], PlainValidator(_taint)]
RunId = Annotated[str, Field(pattern=RUN_ID.pattern)]
StepId = Annotated[str, Field(pattern=STEP_ID)]


class FileFact(Model):
    """A file a step is given: its size in bytes and the SHA-256 of what it
    holds, an upload's own from its pointer.
    """

    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=SHA256)]


class PinnedWorkflow(Model):
    """The run workflow the task names: the reference it wrote, the forge's
    id for it, the commit its version stood at when the task was saved, and
    each of its files a step is given.
    """

    ref: str
    id: Annotated[str, Field(min_length=1)]
    commit: Annotated[str, Field(pattern=COMMIT)]
    files: dict[str, FileFact] = Field(default_factory=dict)


class EntrantEntry(Model):
    """An input the entrant gives: its type, an enum's options, the parts it
    is given in, and on a file or folder the most it may hold, in bytes.
    """

    type: Type
    options: tuple[str, ...] | None = None
    parts: Annotated[int, Field(ge=1)] | None = None
    max_size: Annotated[int, Field(ge=1)] | None = None


class RunEntry(Model):
    """A run: its id, its key with a part as `{part: <n>}`, and whether it is
    early.
    """

    id: RunId
    key: dict[str, Any]
    early: bool


class Item(Model):
    """One run of a step done per run: its inputs, and why it is tainted."""

    inputs: Inputs
    tainted: Taint | None = None


class ValueStep(Model):
    """The value a step serves the `exec` program of, never a workflow
    step's: its name, and for the program's run, the report it gives the
    value's number under."""

    name: Annotated[str, Field(pattern=VALUE_NAME.pattern)]
    report: str | None = None


class CheckStep(Model):
    """The check a step is a copy for: its place in the task's `checks`,
    from 1, and the step it copies."""

    index: Annotated[int, Field(ge=1)]
    of: StepId


class RunStep(Model):
    """One workflow step bound to the task's values: its class, its
    primitive and image, whether it may reach the network and takes the
    cache, the boxes one item uses at once, its outputs, its folder ports,
    whether its manifest program's reports are the entrant's, and its
    inputs once or an item per run, by run id, each with why it is tainted.
    A value's program is two steps of its own (`value`): compiled once at
    the save, and run once per grading after the runs it reads. A check's
    copy of a step graded per submission says which (`check`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    class_: StepClass = Field(alias="class")
    primitive: Annotated[str, Field(pattern=PRIMITIVE)]
    image: Image
    network: bool
    cache: bool
    boxes: Annotated[int, Field(ge=1)] | None = None
    outputs: dict[str, str]
    folders: tuple[str, ...] | None = None
    self_reported: Literal[True] | None = None
    tainted: Taint | None = None
    value: ValueStep | None = None
    check: CheckStep | None = None
    inputs: Inputs | None = None
    items: dict[RunId, Item] | None = None

    @property
    def once(self) -> bool:
        return self.class_ in ONCE


class PipelineStep(Model):
    """One Woodpecker step of a pipeline: its name, the workflow step it
    runs, the runs it holds, none for a step done once, the Woodpecker steps
    of the same pipeline it waits for, and its container's limits.
    """

    name: Annotated[str, Field(pattern=PIPELINE_STEP)]
    step: StepId
    runs: tuple[RunId, ...] | None = Field(default=None, min_length=1)
    depends_on: tuple[str, ...] = ()
    limits: StepLimits


class ReleasedSplit(Model):
    """The split a grading takes when it is made while the task's results
    are released (9.4): the ids of the runs early then, and the early and
    late pipelines then. The save pipeline is the same either way.
    """

    early: tuple[RunId, ...]
    pipelines: dict[GradingKind, tuple[PipelineStep, ...]] = Field(default_factory=dict)


class Confirmation(Model):
    """A column the entrant could set, with the entrant inputs that let
    them. The plan lists every one, whether or not its save asked the
    organiser to confirm it (#504), which it does only when they change.
    """

    column: str
    inputs: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class FilesRead:
    """What one Woodpecker step reads: task files and workflow files by path,
    a folder's files each by itself, entrant inputs with their part, and
    earlier steps' outputs.
    """

    task: frozenset[str] = frozenset()
    workflow: frozenset[str] = frozenset()
    entrant: frozenset[tuple[str, int | None]] = frozenset()
    outputs: frozenset[tuple[str, str]] = frozenset()


class RunPlan(Model):
    """A run task's plan, in the shape of the runner's plan schema
    version 6.
    """

    schema_version: Literal[6] = SCHEMA_VERSION
    workflow: PinnedWorkflow
    task_files: dict[str, FileFact]
    entrant: dict[str, EntrantEntry]
    runs: tuple[RunEntry, ...]
    steps: dict[StepId, RunStep]
    pipelines: dict[PipelineKind, tuple[PipelineStep, ...]]
    reports: dict[str, ReportEntry]
    notes: tuple[str, ...] = ()
    confirmations: tuple[Confirmation, ...] = ()
    released: ReleasedSplit | None = None

    def to_bytes(self) -> bytes:
        """The plan as the file it is committed as: JSON with sorted keys,
        two spaces of indent and a final newline, the same bytes every time.
        """
        document = self.model_dump(by_alias=True, exclude_none=True)
        return (exact_json.dumps(document, sort_keys=True, indent=2) + "\n").encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> RunPlan:
        """The plan in a `plans/plan.json`, its numbers read exactly; pydantic's
        `ValidationError` for one that does not read as a version 6 plan.
        """
        try:
            document = exact_json.loads(data)
        except ValueError:
            return cls.model_validate_json(data)
        return cls.model_validate(document)

    def pipeline(self, kind: PipelineKind) -> tuple[PipelineStep, ...]:
        """The Woodpecker steps of one pipeline, none when it is left out."""
        return self.pipelines.get(kind, ())

    def for_split(self, released: bool) -> RunPlan:
        """The plan as a grading made `released` or not reads it: made
        while the task's results are released, each run early as the
        released split says and its early and late pipelines; otherwise,
        or when a release moves nothing, the plan as it is.
        """
        split = self.released
        if not released or split is None:
            return self
        early = set(split.early)
        return self.model_copy(
            update={
                "runs": tuple(
                    run.model_copy(update={"early": run.id in early}) for run in self.runs
                ),
                "pipelines": {
                    **({"save": self.pipelines["save"]} if "save" in self.pipelines else {}),
                    **split.pipelines,
                },
                "released": None,
            }
        )

    def items_of(self, held: PipelineStep) -> list[tuple[str | None, Inputs]]:
        """The inputs of each run a Woodpecker step holds, in its order, or of
        the one run of a step done once, with no run.
        """
        step = self.steps[held.step]
        if held.runs is None:
            return [(None, step.inputs or {})]
        items = step.items or {}
        return [(run, items[run].inputs) for run in held.runs]

    def files_read(self, held: PipelineStep) -> FilesRead:
        """Every file, entrant input and output a Woodpecker step reads."""
        task: set[str] = set()
        workflow: set[str] = set()
        entrant: set[tuple[str, int | None]] = set()
        outputs: set[tuple[str, str]] = set()
        for _, inputs in self.items_of(held):
            for value in _leaves(inputs.values()):
                if "task" in value:
                    task |= named_by(self.task_files, value["task"])
                elif "workflow" in value:
                    workflow |= named_by(self.workflow.files, value["workflow"])
                elif "entrant" in value:
                    entrant.add((value["entrant"], value.get("part")))
                elif "step" in value:
                    outputs.add((value["step"], value["output"]))
        return FilesRead(
            frozenset(task), frozenset(workflow), frozenset(entrant), frozenset(outputs)
        )

    def task_paths(self) -> tuple[str, ...]:
        """Every file of the publication a step is given, sorted, but one
        only a check's copies are given, on which no grading depends."""
        copies: set[str] = set()
        graded: set[str] = set()
        for kind in PIPELINES:
            for held in self.pipeline(kind):
                read = self.files_read(held).task
                (copies if self.steps[held.step].check is not None else graded).update(read)
        return tuple(
            sorted(path for path in self.task_files if path in graded or path not in copies)
        )

    def checks(self) -> tuple[int, ...]:
        """The place of each of the task's checks, from 1, in order."""
        return tuple(
            sorted({step.check.index for step in self.steps.values() if step.check is not None})
        )


@dataclass(frozen=True, slots=True)
class RunPlanCheck:
    """Every problem a run task's plan is refused for, each at its path in
    `task.yaml`, and the plan when there is none.
    """

    problems: list[Problem]
    plan: RunPlan | None


@dataclass(frozen=True, slots=True)
class FilesNamed:
    """The task's files and folders, and the workflow's, that a step is
    given in some run, a folder ending in `/`.
    """

    task: frozenset[str]
    workflow: frozenset[str]


def files_named(workflow: RunWorkflow, task: RunTask) -> FilesNamed:
    """What the plan of `task` names, which a save reads the facts of before
    it makes the plan.
    """
    named_task: set[str] = {program.folder for program in task.programs.values()}
    named_task |= check_paths(task.checks)
    named_workflow: set[str] = set()
    for step in workflow.steps:
        for found in _references_in(step.with_.values()):
            if found.kind == "workflow":
                named_workflow.add(found.name)
                continue
            declared = workflow.inputs.get(found.name) if found.kind == "inputs" else None
            if declared is None or declared.type not in FILES:
                continue
            given = task.settings.get(found.name)
            if isinstance(given, str):
                named_task.add(given)
            for run in task.runs:
                value = run.key.get(found.name)
                if isinstance(value, str):
                    named_task.add(value)
    return FilesNamed(frozenset(named_task), frozenset(named_workflow))


def order_problems(
    classes: Mapping[str, StepClass], reads: Mapping[str, Sequence[str]]
) -> list[Problem]:
    """A problem for each step that reads one done more often, which would
    run before its input exists. A step's class follows from what it reads,
    so none is found unless that is wrong; the planner checks it anyway.
    """
    problems: list[Problem] = []
    for reader, read in reads.items():
        wants = _READS[classes[reader]]
        for each in read:
            gives = _READS[classes[each]]
            if gives[0] > wants[0] or gives[1] > wants[1]:
                problems.append(
                    Problem(
                        path="workflow",
                        message=f"The step {reader} runs {CLASS_WORDS[classes[reader]]}, and "
                        f"reads {each}, which runs {CLASS_WORDS[classes[each]]}, so {reader} "
                        f"would run before what it reads exists.",
                    )
                )
    return problems


def cache_problems(checked: RunWorkflowCheck) -> list[Problem]:
    """A problem for each step whose primitive reads the task's files
    through the machine's cache but makes no isolate boxes: the cache is
    root's, and only `exec` and `interact` run as root, every other step as
    the grading's own user, which cannot read it.
    """
    return [
        Problem(
            path="workflow",
            message=f"The step {step.id} uses {step.use}, which reads the task's files "
            "through the machine's cache, but only primitives that run programs in boxes "
            "may, such as unicon/exec.",
        )
        for step in checked.steps.values()
        if step.declaration.cache and step.use.partition("@")[0] not in BOXED
    ]


def side_by_side(steps: Sequence[PipelineStep]) -> int:
    """The most memory, in MB, the Woodpecker steps of one pipeline can use
    at once, at most: for each step, its own and that of every step it
    neither waits for nor is waited for by, through `depends_on`, and the
    largest of those. Each step waits only for steps before it.
    """
    place = {step.name: number for number, step in enumerate(steps)}
    before: list[int] = []
    for step in steps:
        mask = 0
        for name in step.depends_on:
            if name in place:
                mask |= before[place[name]] | 1 << place[name]
        before.append(mask)
    after = [0] * len(steps)
    for number in reversed(range(len(steps))):
        for name in steps[number].depends_on:
            if name in place:
                after[place[name]] |= after[number] | 1 << number
    by_memory: dict[int, int] = {}
    for number, step in enumerate(steps):
        by_memory[step.limits.memory_mb] = by_memory.get(step.limits.memory_mb, 0) | 1 << number
    everyone = (1 << len(steps)) - 1
    most = 0
    for number, step in enumerate(steps):
        apart = everyone & ~(before[number] | after[number] | 1 << number)
        beside = sum(memory * (apart & mask).bit_count() for memory, mask in by_memory.items())
        most = max(most, step.limits.memory_mb + beside)
    return most


def slot_of(steps: Sequence[PipelineStep], slots: Iterable[Slot]) -> Slot | None:
    """The smallest slot that holds the most the steps can use at once,
    none when no slot does."""
    needed = side_by_side(steps)
    return min(
        (slot for slot in slots if slot.memory_mb >= needed),
        key=lambda slot: slot.memory_mb,
        default=None,
    )


def plan_runs(
    definition: TaskDefinition,
    workflow: RunWorkflow,
    checked: RunWorkflowCheck,
    task: RunTask,
    *,
    ref: str,
    workflow_id: str,
    commit: str,
    task_files: Mapping[str, FileFact],
    workflow_files: Mapping[str, FileFact],
    machine: Machine,
    programs: Mapping[str, PrimitiveDeclaration] | None = None,
) -> RunPlanCheck:
    """The plan of the run task `definition`, as `check_run_task` checked it
    (`task`) against its workflow resolved at `commit` (`checked`).
    `task_files` and `workflow_files` hold the facts of every file
    `files_named` names, each folder's files each by itself; `machine` is
    the largest a step may take; `programs` the declarations of
    `PROGRAM_USES`, by use, when a value has an `exec` program, which is
    compiled at the save and run once per grading in the pipeline its
    value is shown from, after its group's runs (`_Planner.programs`).
    The plan holds both splits of a grading (9.4): the one made before any
    release, and when a release moves anything, the one made while the
    task's results are released (`RunPlan.released`), each fitted to the
    machine.
    """
    assert definition.scoring is not None, "a run task has scoring once it is checked"
    planner = _Planner(
        definition,
        definition.scoring,
        workflow,
        checked,
        task,
        task_files,
        workflow_files,
        machine,
    )
    for step in workflow.steps:
        planner.bind(step.id, step.with_)
    problems = [
        *cache_problems(checked),
        *order_problems(planner.classes, planner.reads),
        *planner.shown_reports(),
    ]
    if task.programs:
        assert programs is not None, "a save reads the program primitives when a value has one"
        problems += planner.programs(programs[COMPILE_USE], programs[EXEC_USE])
    if problems:
        return RunPlanCheck(problems, None)
    pipelines = planner.pipelines()
    on_release = planner.pipelines(released=True)
    fitted = planner.fit(pipelines)
    problems = [
        *fitted,
        *(problem for problem in planner.fit(on_release) if problem not in fitted),
        *planner.refusals(),
    ]
    if problems:
        return RunPlanCheck(problems, None)
    early_released = task.early(released=True)
    released = (
        ReleasedSplit(
            early=tuple(run.id for run in task.runs if run.id in early_released),
            pipelines={kind: on_release[kind] for kind in ("early", "late") if kind in on_release},
        )
        if early_released != task.early() or on_release != pipelines
        else None
    )
    copied, save, refused = _check_copies(
        task,
        planner.run_steps(),
        pipelines.get("save", ()),
        planner.whole(),
        task_files,
        len(planner.reports()),
    )
    if refused:
        return RunPlanCheck(refused, None)
    if save:
        pipelines = {
            kind: save if kind == "save" else pipelines[kind]
            for kind in PIPELINES
            if kind == "save" or kind in pipelines
        }
    plan = RunPlan(
        workflow=PinnedWorkflow(
            ref=ref, id=workflow_id, commit=commit, files=dict(sorted(workflow_files.items()))
        ),
        task_files=dict(sorted(task_files.items())),
        entrant=planner.entrant_entries(),
        runs=tuple(
            RunEntry(id=run.id, key=_key_json(run.key), early=run.early) for run in task.runs
        ),
        steps={**planner.run_steps(), **copied},
        pipelines=pipelines,
        reports=planner.reports(),
        notes=(*planner.notes(), *task.release_notes()),
        confirmations=tuple(
            Confirmation(column=column, inputs=tuple(sorted(inputs)))
            for column, inputs in sorted(task.self_reported.items())
        ),
        released=released,
    )
    broken = violation(exact_json.loads(plan.to_bytes()), "plan")
    if broken is not None:
        return RunPlanCheck(
            [
                Problem(
                    path="workflow",
                    message=f"Unicon cannot grade this task with {definition.workflow}: the "
                    f"plan it makes breaks one of the runner's rules ({broken}).",
                )
            ],
            None,
        )
    return RunPlanCheck([], plan)


def _check_copies(
    task: RunTask,
    steps: Mapping[str, RunStep],
    own: Sequence[PipelineStep],
    whole: Sequence[PipelineStep],
    task_files: Mapping[str, FileFact],
    reports: int,
) -> tuple[dict[str, RunStep], tuple[PipelineStep, ...], list[Problem]]:
    """Each check's copies of the steps graded per submission, and the
    save's pipeline with them after its `own` steps, check by check, each
    check's laid out as `whole` lays a grading out; or what refuses them: a
    copy's name taken or too long, a check's file larger than the entrant
    may give, the pipeline then past the run ceiling, or the checks able
    to keep more of the save's result than `CHECKS_ROOM`. None when the
    task has no checks.
    """
    if not task.checks:
        return {}, (), []
    order = list(dict.fromkeys(held.step for held in whole))
    problems: list[Problem] = []
    copied: dict[str, RunStep] = {}
    save = list(own)
    for check in task.checks:
        at = ("checks", check.index - 1)
        rename = {step: copy_of(check.index, step) for step in order}
        taken = next(
            (name for name in rename.values() if name in steps or len(name) > COPY_MOST), None
        )
        if taken is not None:
            problems.append(
                Problem(
                    path=path_text(at),
                    message=f"Unicon names this check's copy of a step {taken}, which "
                    + (
                        "the workflow's step of that name already takes."
                        if taken in steps
                        else f"is longer than the {COPY_MOST} characters a step's name may be."
                    )
                    + " Remove the check, or use a workflow whose steps' ids are shorter.",
                )
            )
            continue
        problems += _too_large(task, check, at, task_files)
        for step in order:
            copied[rename[step]] = _copied(steps[step], step, check, rename, task)
        before = _last(save)
        for each in whole:
            waits = tuple(copy_of(check.index, name) for name in each.depends_on)
            save.append(
                each.model_copy(
                    update={
                        "name": copy_of(check.index, each.name),
                        "step": rename[each.step],
                        "depends_on": waits or before,
                    }
                )
            )
    if problems:
        return {}, (), problems
    total = wall_of([held.limits.time_ms for held in save])
    ceiling = WALL_CEILING.total_seconds()
    if total > ceiling:
        problems.append(
            Problem(
                path="checks",
                message=f"The save's steps and its checks take {math.ceil(total / 60)} "
                f"minutes, but a pipeline may take at most {int(ceiling // 60)}. Give fewer "
                "checks.",
            )
        )
        return {}, (), problems
    room = len(task.checks) * check_room([run.key for run in task.runs], reports, len(order))
    if room > CHECKS_ROOM:
        problems.append(
            Problem(
                path="checks",
                message=f"A failed check keeps up to {RUNS_KEPT} of its runs in the save's "
                f"result, so these {len(task.checks)} checks could keep {room} bytes, more than "
                f"the {CHECKS_ROOM} they may. Give fewer checks.",
            )
        )
        return {}, (), problems
    return copied, tuple(save), []


def _last(steps: Sequence[PipelineStep]) -> tuple[str, ...]:
    """The steps of a pipeline no other step of it waits for."""
    waited = {name for held in steps for name in held.depends_on}
    return tuple(held.name for held in steps if held.name not in waited)


def _too_large(
    task: RunTask, check: TaskCheck, at: Path, task_files: Mapping[str, FileFact]
) -> list[Problem]:
    """A file or folder a check gives that holds more than the entrant may
    give that input."""
    found: list[Problem] = []
    for name, given in check.given.items():
        most = task.entrant[name].form.max_size or DEFAULT_MAX_SIZE
        for path in given.paths:
            size = sum(task_files[each].size for each in named_by(task_files, path))
            if size > most:
                found.append(
                    Problem(
                        path=path_text((*at, "submit", name)),
                        message=f"{path} holds {size} bytes, more than the {most} the entrant "
                        f"may give as {name}.",
                    )
                )
    return found


def _copied(
    step: RunStep, of: str, check: TaskCheck, rename: Mapping[str, str], task: RunTask
) -> RunStep:
    """Check `check`'s copy of `step`, the step `of`: done at the save, for
    every run, given what the check gives where the entrant's inputs were,
    and reading the check's copies of the steps it reads."""

    def given(value: Any) -> Any:
        return _for_check(value, check, rename, task)

    return step.model_copy(
        update={
            "class_": "save_once" if step.once else "save_per_run",
            "check": CheckStep(index=check.index, of=of),
            "inputs": (
                {port: given(value) for port, value in step.inputs.items()}
                if step.inputs is not None
                else None
            ),
            "items": (
                {
                    run: item.model_copy(
                        update={
                            "inputs": {port: given(value) for port, value in item.inputs.items()}
                        }
                    )
                    for run, item in step.items.items()
                }
                if step.items is not None
                else None
            ),
        }
    )


def _for_check(value: Any, check: TaskCheck, rename: Mapping[str, str], task: RunTask) -> Any:
    """A bound value as a check's copy is given it: an entrant's file or
    folder by the check's task path, any other entrant value, or text
    written with one, by the check's value, and a step graded per
    submission by the check's copy of it."""
    if not isinstance(value, dict):
        return value
    if "entrant" in value:
        given = check.part(value["entrant"], value.get("part"))
        return {"task": given} if task.entrant[value["entrant"]].type in FILES else {"value": given}
    if "template" in value:
        parts = [spelled(check.part(part["entrant"], part.get("part"))) for part in value["parts"]]
        return {"value": value["template"].format(*parts)}
    if "step" in value and value["step"] in rename:
        return {**value, "step": rename[value["step"]]}
    if "files" in value:
        return {
            "files": {
                name: _for_check(each, check, rename, task) for name, each in value["files"].items()
            }
        }
    if PROGRAMS in value:
        return {
            PROGRAMS: {
                program: {
                    port: _for_check(each, check, rename, task) for port, each in inputs.items()
                }
                for program, inputs in value[PROGRAMS].items()
            }
        }
    return value


@dataclass(frozen=True, slots=True)
class _Copy:
    """A file a boxed primitive copies into a box: the kilobytes it takes
    in the box's tmpfs, whole pages, what it is in words, and the task's
    input that gives it, none for the workflow's file or a step's output.
    """

    kb: int
    words: str
    input: str | None


@dataclass
class _Bound:
    """One run of a step, or its one run when done once: its inputs, every
    hidden file its outputs carry, why it is tainted, and its limits.
    """

    inputs: dict[str, Any]
    hidden: list[dict[str, str]]
    tainted: dict[str, Any] | None
    limits: dict[str, int] = field(default_factory=dict)
    copies: list[_Copy] = field(default_factory=list)


_ABSENT = object()


class _Planner:
    def __init__(
        self,
        definition: TaskDefinition,
        scoring: Scoring,
        workflow: RunWorkflow,
        checked: RunWorkflowCheck,
        task: RunTask,
        task_files: Mapping[str, FileFact],
        workflow_files: Mapping[str, FileFact],
        machine: Machine,
    ) -> None:
        self.definition = definition
        self.scoring = scoring
        self.workflow = workflow
        self.checked = checked
        self.task = task
        self.task_files = task_files
        self.workflow_files = workflow_files
        self.machine = machine
        self.entrant = frozenset(task.entrant)
        self.repeated = frozenset(task.runs[0].key) if task.runs else frozenset()
        """The inputs whose value differs per run: each repeated one, the
        entrant's given in parts among them."""
        self.classes: dict[str, StepClass] = {}
        self.reads: dict[str, list[str]] = {}
        """The steps each step reads directly, in the order it names them."""
        self.raising: dict[str, list[str]] = {}
        """The task's inputs each step raises its time from."""
        self.bound: dict[tuple[str, str | None], _Bound] = {}
        self.given_by: dict[str, str] = {}
        """The task's input each task file value is given by."""
        self.output_mb: dict[str, int] = {}
        """The most any run of each step may write, which bounds a file it
        gives another step."""
        self.program_steps: dict[str, RunStep] = {}
        """The steps of each value's `exec` program, by id."""

    # Binding

    def bind(self, step_id: str, given: Mapping[str, Any]) -> None:
        resolved = self.checked.steps[step_id]
        found = _references_in(given.values())
        read = list(dict.fromkeys(each.name for each in found if each.kind == "steps"))
        inputs = {each.name for each in found if each.kind == "inputs"}
        entrant = bool(inputs & self.entrant) or any(_READS[self.classes[s]][0] for s in read)
        repeated = bool(inputs & self.repeated) or any(_READS[self.classes[s]][1] for s in read)
        kind = next(name for name, flags in _READS.items() if flags == (entrant, repeated))
        self.classes[step_id] = kind
        self.reads[step_id] = read
        self.raising[step_id] = self._raising(resolved, given)
        runs_entrant = any(
            program.controlled_by & self.entrant
            for program in (resolved.program, *resolved.programs.values())
        )
        self_reported = self._self_reported(resolved)
        declaration = resolved.declaration
        for run in self.task.runs if kind not in ONCE else [None]:
            values: dict[str, Any] = {}
            hidden: list[dict[str, str]] = []
            beside: dict[str, str] | None = None
            for name, raw in given.items():
                port = declaration.inputs[name]
                value, carried, open_ = self._bind(raw, port, run)
                if value is _ABSENT:
                    continue
                values[name] = value
                hidden.extend(each for each in carried if each not in hidden)
                beside = beside or (open_[0] if open_ else None)
            if self_reported:
                values[SELF_REPORTED] = {"value": True}
            tainted: dict[str, Any] | None = None
            if runs_entrant and beside is not None:
                tainted = {"beside": beside}
            else:
                tainted = next(
                    ({"reads": each} for each in read if self._of(each, run).tainted), None
                )
            bound = _Bound(values, hidden, tainted)
            bound.limits, bound.copies = self._item_limits(resolved, values)
            self.bound[(step_id, run.id if run is not None else None)] = bound
        self.output_mb[step_id] = max(
            bound.limits["output_mb"] for (each, _), bound in self.bound.items() if each == step_id
        )

    def _of(self, step_id: str, run: Any) -> _Bound:
        """The run of `step_id` that `run` reads: its own, or the one run of a
        step done once.
        """
        if self.classes[step_id] in ONCE or run is None:
            return self.bound[(step_id, None)]
        return self.bound[(step_id, run.id)]

    def _bind(
        self, raw: object, port: Port, run: Any
    ) -> tuple[object, list[dict[str, str]], list[dict[str, str]]]:
        """A port's value in `run`, `_ABSENT` for an optional input the task
        leaves out; the hidden files it carries; and those of them it gives on
        a port not marked secret.
        """
        if port.type is Type.PROGRAMS:
            assert isinstance(raw, dict)
            programs: dict[str, dict[str, Any]] = {}
            hidden: list[dict[str, str]] = []
            open_: list[dict[str, str]] = []
            inner = port.inputs or {}
            for name, values in raw.items():
                assert isinstance(values, dict)
                bound: dict[str, Any] = {}
                for key, each in values.items():
                    value, carried, given_open = self._bind(each, inner[key], run)
                    if value is _ABSENT:
                        continue
                    bound[key] = value
                    hidden += carried
                    open_ += given_open
                programs[str(name)] = bound
            return {"programs": programs}, hidden, open_
        if port.type is Type.LINKS:
            assert isinstance(raw, list)
            return {"links": list(raw)}, [], []
        if port.type is Type.NAMED_FILES:
            assert isinstance(raw, dict)
            files: dict[str, Any] = {}
            hidden = []
            for name, each in raw.items():
                value, carried = self._plain(each, port, run)
                files[str(name)] = value
                hidden += carried
            return {"files": files}, hidden, [] if port.secret else hidden
        value, hidden = self._plain(raw, port, run)
        return value, hidden, [] if port.secret else hidden

    def _plain(self, raw: object, port: Port, run: Any) -> tuple[object, list[dict[str, str]]]:
        if isinstance(raw, bool) or is_number(raw):
            return {"value": spelled(raw) if port.type is Type.TEXT else raw}, []
        assert isinstance(raw, str)
        found = whole_reference(raw, runs=True)
        if found is None:
            return self._written(raw, run), []
        if found.kind == "workflow":
            value = {"workflow": found.name}
            return value, [value]
        if found.kind == "steps":
            assert found.port is not None
            return {"step": found.name, "output": found.port}, list(
                self._of(found.name, run).hidden
            )
        name = found.name
        declared = self.workflow.inputs[name]
        if name in self.entrant:
            entrant = self._entrant(name, run)
            if port.type is Type.TEXT and declared.type is not Type.TEXT:
                return {"template": "{0}", "parts": [entrant]}, []
            return entrant, []
        given = self._given(name, run)
        if given is _ABSENT:
            return _ABSENT, []
        if declared.type in FILES:
            assert isinstance(given, str)
            value = {"task": given}
            self.given_by.setdefault(given, name)
            return value, [value] if self._hidden(given) else []
        return {"value": spelled(given) if port.type is Type.TEXT else given}, []

    def _written(self, raw: str, run: Any) -> dict[str, Any]:
        """Text with references written into it: the task's scalars written
        in now, the entrant's as template parts.
        """
        text = ""
        parts: list[dict[str, Any]] = []
        last = 0
        for match, found in references(raw, runs=True):
            text += escaped(raw[last : match.start()])
            last = match.end()
            if found.name in self.entrant:
                text += f"{{{len(parts)}}}"
                parts.append(self._entrant(found.name, run))
            else:
                given = self._given(found.name, run)
                text += escaped(spelled(given) if given is not _ABSENT else "")
        text += escaped(raw[last:])
        if not parts:
            return {"value": text.replace("{{", "{").replace("}}", "}")}
        return {"template": text, "parts": parts}

    def _entrant(self, name: str, run: Any) -> dict[str, Any]:
        part = run.key.get(name) if run is not None else None
        if isinstance(part, Part):
            return {"entrant": name, "part": part.number}
        return {"entrant": name}

    def _given(self, name: str, run: Any) -> object:
        """What the task fills an input with in `run`: its repeated value or
        its setting, or `_ABSENT` for an optional input left out.
        """
        if run is not None and name in run.key:
            return run.key[name]
        if name in self.task.settings:
            given = self.task.settings[name]
            assert isinstance(given, str | bool) or is_number(given), (
                "a secret is refused at the save until org secrets exist"
            )
            return given
        return _ABSENT

    def _hidden(self, path: str) -> bool:
        """Whether entrants are not shown a task file during the contest."""
        return file_time(self.scoring, path) not in SHOWN_DURING

    def _self_reported(self, resolved: ResolvedStep) -> bool:
        """Whether the entrant's code reaches the step's own program given a
        manifest, where the primitive takes `self_reported` to say so.
        """
        program = resolved.program
        return (
            SELF_REPORTED in resolved.declaration.inputs
            and program.manifest is not None
            and bool(program.controlled_by & self.entrant)
        )

    def _raising(self, resolved: ResolvedStep, given: Mapping[str, Any]) -> list[str]:
        """The task's inputs a step's time is raised from."""
        found: list[str] = []
        programs = given.get(PROGRAMS)
        for name, source in resolved.declaration.limits_from.items():
            if name not in BATCH_SCALED:
                continue
            if isinstance(source, FromInput):
                raws = [given.get(source.input)]
            elif isinstance(source, FromSum | FromMax) and isinstance(programs, dict):
                key = source.program_input
                raws = [each.get(key) for each in programs.values() if isinstance(each, dict)]
            else:
                raws = []
            for each in _references_in(raws):
                if each.kind == "inputs" and each.name not in self.entrant:
                    found.append(each.name)
        return list(dict.fromkeys(found))

    # Limits

    def _item_limits(
        self, resolved: ResolvedStep, inputs: Mapping[str, Any]
    ) -> tuple[dict[str, int], list[_Copy]]:
        """One run's limits, raised from its numbers, and for a boxed
        primitive the files it copies into its boxes, its memory what its
        declaration gives, its boxes' memory limits and the room it adds,
        with those files on top, and at least its boxes' memory limits,
        those files and `PRIMITIVE_ROOM_MB`. The primitive caps its step at
        this figure, handed to it in the step's job, and does not work it
        out again.
        """
        declaration = resolved.declaration
        numbers = {
            name: value["value"]
            for name, value in inputs.items()
            if isinstance(value, dict) and set(value) == {"value"} and _number(value["value"])
        }
        held = inputs.get(PROGRAMS)
        given: dict[str, dict[str, Any]] = held["programs"] if isinstance(held, dict) else {}
        programs = {
            name: {
                key: value["value"]
                for key, value in values.items()
                if set(value) == {"value"} and _number(value["value"])
            }
            for name, values in given.items()
        }
        limits = declaration.item_limits(numbers, programs)
        copied = BOXED.get(resolved.use.partition("@")[0])
        copies: list[_Copy] = []
        if copied is not None:
            copies = [
                copy
                for values in (inputs, *given.values())
                for port in copied
                for copy in self._copies(values.get(port))
            ]
            kb = sum(copy.kb for copy in copies)
            boxes_mb = sum(
                each[BOX_MEMORY]
                for each in (numbers, *programs.values())
                if _number(each.get(BOX_MEMORY))
            )
            least = math.ceil(boxes_mb + kb / 1024) + PRIMITIVE_ROOM_MB
            limits["memory_mb"] = max(limits["memory_mb"] + math.ceil(kb / 1024), least)
        return limits, copies

    def _copies(self, value: object) -> list[_Copy]:
        """The files a value copies into a box, each to whole pages: a task
        or workflow file by its size, an entrant's by the most its input
        may hold, and an earlier step's by the most that step may write.
        """
        if not isinstance(value, dict):
            return []
        if "files" in value:
            return [copy for each in value["files"].values() for copy in self._copies(each)]
        if "task" in value:
            given = self.given_by.get(value["task"])
            return [
                _Copy(_pages(self.task_files[path].size), f"`{path}`", given)
                for path in sorted(named_by(self.task_files, value["task"]))
            ]
        if "workflow" in value:
            return [
                _Copy(_pages(self.workflow_files[path].size), f"the workflow's `{path}`", None)
                for path in sorted(named_by(self.workflow_files, value["workflow"]))
            ]
        if "entrant" in value:
            name = value["entrant"]
            return [_Copy(_pages(self._max_size(name)), f"the entrant's {name}", name)]
        if "step" in value:
            step = value["step"]
            return [_Copy(self.output_mb[step] * 1024, f"what {step} writes", None)]
        return []

    def _max_size(self, name: str) -> int:
        entrant = self.task.entrant[name]
        if entrant.type not in FILES:
            return 0
        return entrant.form.max_size or DEFAULT_MAX_SIZE

    # A value's program

    def programs(self, build: PrimitiveDeclaration, run: PrimitiveDeclaration) -> list[Problem]:
        """The two steps of each value's `exec` program: `build-<value>`,
        compiling the folder at the save, and `exec-<value>`, running it on
        the JSON file of its group's runs the forge builds (`RUNS_FILE`),
        its manifest the folder's `program.yaml`, under its limits, with
        room for what it copies into its box. A workflow step of either
        name is refused.
        """
        problems: list[Problem] = []
        for name, program in self.task.programs.items():
            ids = self.task.groups[program.group]
            taken = [step for step in (f"build-{name}", f"exec-{name}") if step in self.classes]
            if taken:
                problems.append(
                    Problem(
                        path=path_text(("scoring", "values", name)),
                        message=f"The workflow has a step called {taken[0]}, the name {name}'s "
                        "program step takes. Rename the value or the step.",
                    )
                )
                continue
            compiles = {"source": {"task": program.folder}, "language": {"value": program.language}}
            compiled = build.item_limits({})
            self.bound[(f"build-{name}", None)] = _Bound(compiles, [], None, compiled)
            inputs: dict[str, Any] = {
                "program": {"step": f"build-{name}", "output": "program"},
                "files": {"files": {RUNS_FILE: {"runs": list(ids)}}},
                "manifest": {"task": program.manifest},
                "time_limit": {"value": program.time_limit},
                "memory_limit": {"value": program.memory_limit},
            }
            copies = [
                _Copy(compiled["output_mb"] * 1024, f"the program of {name}", None),
                _Copy(_pages(self.task_files[program.manifest].size), program.manifest, None),
                _Copy(RUN_ENTRY_KB * len(ids), f"the runs {name} reads", None),
            ]
            limits = run.item_limits(
                {"time_limit": program.time_limit, "memory_limit": program.memory_limit}
            )
            kb = sum(copy.kb for copy in copies)
            least = (
                math.ceil(Decimal(program.memory_limit) + Decimal(kb) / 1024) + PRIMITIVE_ROOM_MB
            )
            limits["memory_mb"] = max(limits["memory_mb"] + math.ceil(kb / 1024), least)
            self.bound[(f"exec-{name}", None)] = _Bound(inputs, [], None, limits, copies)
            self.raising[f"build-{name}"] = self.raising[f"exec-{name}"] = []
            self.program_steps[f"build-{name}"] = RunStep.model_validate(
                {
                    "class": "save_once",
                    "primitive": COMPILE_USE,
                    "image": build.image,
                    "network": build.network,
                    "cache": build.cache,
                    "outputs": _declared(build),
                    "folders": ("source",),
                    "value": {"name": name},
                    "inputs": compiles,
                }
            )
            self.program_steps[f"exec-{name}"] = RunStep.model_validate(
                {
                    "class": "grading_once",
                    "primitive": EXEC_USE,
                    "image": run.image,
                    "network": run.network,
                    "cache": run.cache,
                    "boxes": 1,
                    "outputs": {**_declared(run), program.report: Type.NUMBER.value},
                    "value": {"name": name, "report": program.report},
                    "inputs": inputs,
                }
            )
        return problems

    def _program_pipelines(
        self, found: dict[PipelineKind, tuple[PipelineStep, ...]], *, released: bool
    ) -> dict[PipelineKind, tuple[PipelineStep, ...]]:
        """`found` with each value's program steps after the rest: its
        compile at the save, and its run in the early pipeline when its
        value is early (`RunTask.early_programs`), else the late one, each
        waiting for every step before it, so the runs it reads are done.
        """
        early = self.task.early_programs(released=released)
        held = dict(found)
        for name in self.task.programs:
            placed: list[tuple[PipelineKind, str]] = [
                ("save", f"build-{name}"),
                ("early" if name in early else "late", f"exec-{name}"),
            ]
            for kind, step in placed:
                if step not in self.program_steps:
                    continue
                before = held.get(kind, ())
                waited = {waits for each in before for waits in each.depends_on}
                last = tuple(each.name for each in before if each.name not in waited)
                made = PipelineStep(
                    name=step, step=step, depends_on=last, limits=self._limits(step, None)
                )
                held[kind] = (*before, made)
        return {kind: held[kind] for kind in PIPELINES if kind in held}

    # Pipelines

    def pipelines(self, *, released: bool = False) -> dict[PipelineKind, tuple[PipelineStep, ...]]:
        """The pipelines of a grading made before any release, or with
        `released`, of one made while the task's results are released."""
        early = self.task.early(released=released)
        early_runs = tuple(run.id for run in self.task.runs if run.id in early)
        late_runs = tuple(run.id for run in self.task.runs if run.id not in early)
        early_once = self._early_once(bool(early_runs), released=released)
        held: dict[PipelineKind, list[tuple[str, tuple[str, ...] | None]]] = {
            kind: [] for kind in PIPELINES
        }
        every = tuple(run.id for run in self.task.runs)
        for step in self.workflow.steps:
            placed = self.classes[step.id]
            if placed == "save_once":
                held["save"].append((step.id, None))
            elif placed == "save_per_run":
                held["save"].append((step.id, every))
            elif placed == "grading_once":
                held["early" if step.id in early_once else "late"].append((step.id, None))
            else:
                if early_runs:
                    held["early"].append((step.id, early_runs))
                if late_runs:
                    held["late"].append((step.id, late_runs))
        found: dict[PipelineKind, tuple[PipelineStep, ...]] = {}
        for kind, steps in held.items():
            made = self._chained(self._pipeline(steps))
            if made:
                found[kind] = made
        return self._program_pipelines(found, released=released)

    def whole(self) -> tuple[PipelineStep, ...]:
        """Every step graded per submission laid out as one pipeline over
        every run, each value's program after the rest: a check's grading,
        done in the save's pipeline (`_check_copies`)."""
        every = tuple(run.id for run in self.task.runs)
        held: list[tuple[str, tuple[str, ...] | None]] = []
        for step in self.workflow.steps:
            placed = self.classes[step.id]
            if placed == "grading_once":
                held.append((step.id, None))
            elif placed == "grading_per_run":
                held.append((step.id, every))
        made = self._chained(self._pipeline(held))
        for name in self.task.programs:
            program = f"exec-{name}"
            if program in self.program_steps:
                made = (
                    *made,
                    PipelineStep(
                        name=program,
                        step=program,
                        depends_on=_last(made),
                        limits=self._limits(program, None),
                    ),
                )
        return made

    def _early_once(self, any_early: bool, *, released: bool = False) -> set[str]:
        """The steps done once per grading in the early pipeline: every one
        when a run is early, since every run's verdict reads each; when none
        is but `compiled`, or a value reading it, is shown `now`, those
        that can give a compile error, which `compiled` reads, and the steps
        done once per grading they read, a tainted one among them refused
        (`refusals`); otherwise none.
        """
        once = [step.id for step in self.workflow.steps if self.classes[step.id] == "grading_once"]
        if any_early:
            return set(once)
        if not self.task.compile_early(released=released):
            return set()
        found: set[str] = set()
        waiting = self._compilers()
        while waiting:
            step = waiting.pop()
            if step in found:
                continue
            found.add(step)
            waiting += [each for each in self.reads[step] if self.classes[each] == "grading_once"]
        return found

    def _outcomes(self, step_id: str) -> set[str]:
        outcome = self.checked.steps[step_id].declaration.outputs.get("outcome")
        return set(outcome.outcomes or {}) if outcome is not None else set()

    def _pipeline(self, held: list[tuple[str, tuple[str, ...] | None]]) -> tuple[PipelineStep, ...]:
        """The Woodpecker steps of one pipeline from the workflow steps it
        holds, each with the runs it holds of it.
        """
        names: dict[str, dict[str | None, str]] = {}
        """Each workflow step's Woodpecker step by the run it holds, none
        for one holding every run of the step it holds."""
        made: list[PipelineStep] = []
        for step_id, runs in held:
            declaration = self.checked.steps[step_id].declaration
            if runs is None or declaration.batch:
                groups: list[tuple[str, tuple[str, ...] | None]] = [(step_id, runs)]
                names[step_id] = {None: step_id}
            else:
                groups = [(f"{step_id}.{run}", (run,)) for run in runs]
                names[step_id] = {run: f"{step_id}.{run}" for run in runs}
            for name, group in groups:
                depends: list[str] = []
                for read in self.reads[step_id]:
                    held_names = names.get(read)
                    if held_names is None:
                        continue  # in an earlier pipeline
                    if None in held_names:
                        depends.append(held_names[None])
                    else:
                        depends += [held_names[run] for run in group or () if run in held_names]
                made.append(
                    PipelineStep(
                        name=name,
                        step=step_id,
                        runs=group,
                        depends_on=tuple(dict.fromkeys(depends)),
                        limits=self._limits(step_id, group),
                    )
                )
        return tuple(made)

    def _chained(self, steps: tuple[PipelineStep, ...]) -> tuple[PipelineStep, ...]:
        """The pipeline in stages for the smallest slot that holds its
        largest step: a stage takes the next step while their memory adds up
        within the slot, their boxes, one for an `exec` step and one for each
        program of an `interact` step, number no more than the slot's cores,
        and both run in boxes, and each step of a stage waits for every step
        of the stage before that it does not wait for already. A step that
        runs no boxes, whose container may use the whole slot, is a stage
        alone, and so is one with more boxes than the slot has cores. With
        no slot, as without one that holds the largest step, which `fit`
        refuses, it is as it was.
        """
        largest = max((step.limits.memory_mb for step in steps), default=0)
        slot = next((each for each in self.machine.slots if each.memory_mb >= largest), None)
        if slot is None:
            return steps
        stages: list[list[PipelineStep]] = []
        held = boxes = 0
        open_ = False
        for step in steps:
            resolved = self.checked.steps[step.step]
            boxed = resolved.use.partition("@")[0] in BOXED
            memory = step.limits.memory_mb
            given = len(resolved.programs) or 1
            if open_ and boxed and held + memory <= slot.memory_mb and boxes + given <= slot.cores:
                stages[-1].append(step)
                held += memory
                boxes += given
            else:
                stages.append([step])
                held, boxes = memory, given
            open_ = boxed
        waits: dict[str, set[str]] = {}
        made: list[PipelineStep] = []
        for number, stage in enumerate(stages):
            for step in stage:
                found = {each for name in step.depends_on for each in (name, *waits.get(name, ()))}
                added = [
                    each.name
                    for each in (stages[number - 1] if number else ())
                    if each.name not in found
                ]
                waits[step.name] = found | set(added)
                made.append(step.model_copy(update={"depends_on": (*step.depends_on, *added)}))
        return tuple(made)

    def _limits(self, step_id: str, runs: tuple[str, ...] | None) -> StepLimits:
        """A container's limits over the runs it holds: time summed, the rest
        the most of any run.
        """
        items = [self.bound[(step_id, run)].limits for run in (runs or (None,))]
        return StepLimits(
            **{
                name: sum(item[name] for item in items)
                if name in BATCH_SCALED
                else max(item[name] for item in items)
                for name in LIMIT_NAMES
            }
        )

    def fit(self, pipelines: Mapping[PipelineKind, Sequence[PipelineStep]]) -> list[Problem]:
        """Each pipeline within the run ceiling, refused at what raises its
        longest step's time, and each step's memory, with the files it
        copies, on the machine, refused at what gives its largest file.
        """
        problems: list[Problem] = []
        ceiling = WALL_CEILING.total_seconds()
        for kind, steps in pipelines.items():
            total = wall_of([step.limits.time_ms for step in steps])
            if total <= ceiling:
                continue
            longest = max(steps, key=lambda step: step.limits.time_ms)
            raised = [name for step in (longest, *steps) for name in self.raising[step.step]]
            problems.append(
                Problem(
                    path=path_text(self._where(raised[0])) if raised else "workflow",
                    message=f"{PIPELINE_WORDS[kind]} take {math.ceil(total / 60)} minutes, but a "
                    f"pipeline may take at most {int(ceiling // 60)}. The longest, "
                    f"{longest.name}, takes {math.ceil(longest.limits.time_ms / 60_000)}.",
                )
            )
        told: set[str] = set()
        for steps in pipelines.values():
            for step in steps:
                wanted = step.limits.memory_mb
                if wanted <= self.machine.memory_mb or step.step in told:
                    continue
                told.add(step.step)
                copies = [
                    copy
                    for run in step.runs or (None,)
                    for copy in self.bound[(step.step, run)].copies
                ]
                largest = max(copies, key=lambda copy: copy.kb, default=None)
                such = (
                    f", the largest {largest.words} at {math.ceil(largest.kb / 1024)} MB,"
                    if largest is not None
                    else ""
                )
                problems.append(
                    Problem(
                        path=path_text(self._where(largest.input))
                        if largest is not None and largest.input is not None
                        else "workflow",
                        message=f"The step {step.step} needs {wanted} MB of memory, with the "
                        f"files it copies in beside its programs{such} but no machine this task "
                        f"may run on has more than {self.machine.memory_mb}.",
                    )
                )
        return problems

    def _where(self, name: str) -> Path:
        """Where in `task.yaml` the task fills an input."""
        if name in self.definition.inputs:
            return ("inputs", name)
        for index, axis in enumerate(self.definition.repeat):
            if name in axis.inputs:
                return ("repeat", index, "inputs", axis.inputs.index(name))
        return ("inputs",)

    # What the save refuses and notes

    def refusals(self) -> list[Problem]:
        """A value or group shown during the contest that reads runs behind
        a tainted step done once per grading, which every run reads (11.4);
        and `compiled`, or a value reading it, shown then when it cannot be
        known before the hidden runs, or tells of a tainted compile.
        """
        tainted = next(
            (
                step.id
                for step in self.workflow.steps
                if self.classes[step.id] == "grading_once"
                and self.bound[(step.id, None)].tainted is not None
            ),
            None,
        )
        problems: list[Problem] = []
        for time, index, entry in self._shown():
            if tainted is not None and self._runs_read(entry):
                chain, beside = self._chain(tainted, None)
                why = "every run reads " + ", which reads ".join(f"`{step}`" for step in chain)
                why += f", which runs the entrant's code beside {_words(beside)}"
                why += _UNSHOWN.format(entry=entry)
            elif self.task.reads_compiled(entry):
                why = self._compiled_problem(entry)
            else:
                continue
            if why:
                problems.append(
                    Problem(
                        path=path_text(("scoring", "show", time, index)),
                        message=f"{entry} cannot be shown {SHOWN_WORDS[time]}: {why}. Show "
                        f"{entry} after_close instead.",
                    )
                )
        return problems

    def _compiled_problem(self, entry: str) -> str:
        """Why `compiled`, which `entry` is or reads, cannot be shown during
        the contest: the step that can give a compile error runs once per
        run, or there is no such step done once per grading, so it would
        wait for the hidden runs; or that step is tainted. None when it can.
        """
        but = "" if entry == COMPILED else f"it reads `{COMPILED}`, but "
        per_run = [
            step.id
            for step in self.workflow.steps
            if self.classes[step.id] == "grading_per_run"
            and COMPILE_ERROR in self._outcomes(step.id)
        ]
        if per_run:
            return (
                f"{but}`{per_run[0]}`, which can give a compile error, runs once per run, so "
                f"`{COMPILED}` is known only once every run has run, the hidden ones too"
            )
        once = self._compilers()
        if not once:
            return (
                f"{but}no step done once per grading can give a compile error, so `{COMPILED}` "
                "is known only once the whole grading ends, its hidden runs too"
            )
        tainted = next((step for step in once if self.bound[(step, None)].tainted), None)
        if tainted is None:
            return ""
        chain, beside = self._chain(tainted, None)
        return (
            ("it comes from " if entry == COMPILED else f"it reads `{COMPILED}`, from ")
            + ", which reads ".join(f"`{step}`" for step in chain)
            + f", which runs the entrant's code beside {_words(beside)}"
            + _UNSHOWN.format(entry=entry)
        )

    def _compilers(self) -> list[str]:
        """The steps done once per grading that can give a compile error,
        which `compiled` reads.
        """
        return [
            step.id
            for step in self.workflow.steps
            if self.classes[step.id] == "grading_once" and COMPILE_ERROR in self._outcomes(step.id)
        ]

    def notes(self) -> list[str]:
        """For each value or group shown during the contest reading runs
        whose own steps are tainted, that the entrant's code can signal a
        little about the hidden input through it (15.4).
        """
        found: list[str] = []
        for _, _, entry in self._shown():
            runs = self._runs_read(entry)
            example = next(
                (
                    (run.id, step.id)
                    for run in self.task.runs
                    if run.id in runs
                    for step in self.workflow.steps
                    if self.classes[step.id] == "grading_per_run"
                    and self.bound[(step.id, run.id)].tainted is not None
                ),
                None,
            )
            if example is None:
                continue
            run_id, step_id = example
            chain, beside = self._chain(step_id, run_id)
            found.append(
                f"{entry} reads runs whose own steps run the entrant's code beside what entrants "
                f"are not shown during the contest, such as `{chain[-1]}` beside "
                f"{_words(beside)} in the run {run_id}, so the entrant's code can signal a "
                f"little about it through what {entry} shows."
            )
        return found

    def _chain(self, step_id: str, run: str | None) -> tuple[list[str], dict[str, str]]:
        """The steps from a tainted one to the one that runs the entrant's
        code beside a hidden file, and that file.
        """
        chain = [step_id]
        key = run if self.classes[step_id] not in ONCE else None
        tainted = self.bound[(step_id, key)].tainted
        assert tainted is not None
        while "reads" in tainted:
            step_id = tainted["reads"]
            chain.append(step_id)
            key = run if self.classes[step_id] not in ONCE else None
            tainted = self.bound[(step_id, key)].tainted
            assert tainted is not None
        return chain, tainted["beside"]

    def shown_reports(self) -> list[Problem]:
        """A report `show` lists by itself that a step gives for each run,
        which is shown with the runs of the groups `show` lists (stage 5
        section 6 item 30). An entry naming a group or a value names that.
        """
        names = show_names(self.scoring)
        problems: list[Problem] = []
        for time, entries in self.scoring.show.lists().items():
            for index, entry in enumerate(entries):
                report = self.checked.reports.get(entry)
                if report is None or entry in names or self.classes[report.step] in ONCE:
                    continue
                problems.append(
                    Problem(
                        path=path_text(("scoring", "show", time, index)),
                        message=f"{entry} is reported for each run, so it is shown with the runs "
                        "of the groups show lists. Remove it.",
                    )
                )
        return problems

    def _shown(self) -> Iterable[tuple[str, int, str]]:
        """Each entry of `show` during the contest, with its time and place."""
        lists = self.scoring.show.lists()
        for time in SHOWN_DURING:
            for index, entry in enumerate(lists.get(time, [])):
                yield time, index, entry

    def _runs_read(self, entry: str) -> set[str]:
        if entry in self.task.groups:
            return set(self.task.groups[entry])
        if entry in self.task.values.reads:
            groups = self.task.values.through(entry).groups
            return {run for group in groups for run in self.task.groups.get(group, ())}
        return set()

    # The plan's parts

    def run_steps(self) -> dict[str, RunStep]:
        steps: dict[str, RunStep] = {}
        for step in self.workflow.steps:
            resolved = self.checked.steps[step.id]
            declaration = resolved.declaration
            kind = self.classes[step.id]
            boxed = resolved.use.partition("@")[0]
            common: dict[str, Any] = {
                "class": kind,
                "primitive": resolved.use,
                "image": declaration.image,
                "network": declaration.network,
                "cache": declaration.cache,
                "boxes": (len(resolved.programs) or 1) if boxed in BOXED else None,
                "outputs": {
                    f"{name}?" if port.optional else name: port.type.value
                    for name, port in {
                        **resolved.outputs,
                        **self.task.manifest_outputs.get(step.id, {}),
                    }.items()
                },
                "folders": tuple(
                    sorted(
                        name
                        for name, port in declaration.inputs.items()
                        if port.type is Type.FOLDER and name in step.with_
                    )
                )
                or None,
                "self_reported": True if self._self_reported(resolved) else None,
            }
            if kind in ONCE:
                bound = self.bound[(step.id, None)]
                steps[step.id] = RunStep.model_validate(
                    {**common, "inputs": bound.inputs, "tainted": bound.tainted}
                )
            else:
                items = {
                    run.id: Item.model_validate(
                        {
                            "inputs": self.bound[(step.id, run.id)].inputs,
                            "tainted": self.bound[(step.id, run.id)].tainted,
                        }
                    )
                    for run in self.task.runs
                }
                steps[step.id] = RunStep.model_validate({**common, "items": items})
        return {**steps, **self.program_steps}

    def entrant_entries(self) -> dict[str, EntrantEntry]:
        return {
            name: EntrantEntry(
                type=each.type,
                options=each.options,
                parts=each.parts,
                max_size=self._max_size(name) or None,
            )
            for name, each in sorted(self.task.entrant.items())
        }

    def reports(self) -> dict[str, ReportEntry]:
        found: dict[str, ReportEntry] = {}
        for name, report in self.checked.reports.items():
            port = report.port
            number = port is not None and port.type is Type.NUMBER
            found[name] = ReportEntry(
                step=report.step,
                output=f"{report.program}.{report.output}" if report.program else report.output,
                better=port.better if number and port is not None else None,
                at_least=port.at_least if number and port is not None else None,
                at_most=port.at_most if number and port is not None else None,
            )
        return found


def _declared(declaration: PrimitiveDeclaration) -> dict[str, str]:
    """A primitive's own outputs as a plan's step lists them, a `?` after
    the name of one that may be missing."""
    return {
        f"{name}?" if port.optional else name: port.type.value
        for name, port in declaration.outputs.items()
    }


def _references_in(values: Iterable[object]) -> list[Reference]:
    """Every reference in `values`, inside mappings and lists too."""
    found: list[Reference] = []
    for value in values:
        if isinstance(value, str):
            try:
                found.extend(reference for _, reference in references(value, runs=True))
            except BadReference:
                continue
        elif isinstance(value, dict):
            found.extend(_references_in(value.values()))
        elif isinstance(value, list):
            found.extend(_references_in(value))
    return found


def _leaves(values: Iterable[object]) -> Iterable[dict[str, Any]]:
    """Every plain value among `values`: inside named files, programs and
    templates too.
    """
    for value in values:
        if not isinstance(value, dict):
            continue
        if "files" in value:
            yield from _leaves(value["files"].values())
        elif "programs" in value:
            for inputs in value["programs"].values():
                yield from _leaves(inputs.values())
        elif "template" in value:
            yield from _leaves(value["parts"])
        else:
            yield value


def _pages(size: int) -> int:
    return PAGE_KB * -(-size // (PAGE_KB * 1024))


def _number(value: object) -> bool:
    return is_number(value) and not isinstance(value, bool)


def _words(value: Mapping[str, str]) -> str:
    if "workflow" in value:
        return f"the workflow's `{value['workflow']}`"
    return f"`{value['task']}`"


def _key_json(key: Mapping[str, object]) -> dict[str, object]:
    return {
        name: {"part": value.number} if isinstance(value, Part) else value
        for name, value in key.items()
    }


# What a save changes


def run_plan_changes(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """What changed in a run task's plan between the previous publication and
    this one, in words a person reads, for the save's summary only: the
    workflow's version and files, each step added, removed or given other
    inputs, a release moving a primitive's image in place, runs added or
    removed, a task file a step is given, and what is shown during the
    contest, before a release or while one stands, moving which runs are
    early. Empty for a first publication.
    What a regrade reruns is not decided from this list.
    """
    if before is None:
        return ()
    new = RunPlan.from_bytes(after.plans[PLAN_PATH])
    try:
        old = RunPlan.from_bytes(before.plans[PLAN_PATH]) if PLAN_PATH in before.plans else None
    except ValueError:
        old = None
    if old is None:
        return ("The way the task grades changed.",)
    changes: list[str] = []
    if old.workflow.ref != new.workflow.ref:
        changes.append(f"The workflow changed from {old.workflow.ref} to {new.workflow.ref}.")
    elif old.workflow.commit != new.workflow.commit:
        changes.append(f"The workflow {new.workflow.ref} moved to another commit.")
    changes += _compared(old.workflow.files, new.workflow.files, "The workflow's file {}")
    # A check's copy grades no submission, so it changes nothing here.
    graded_old = [name for name, step in old.steps.items() if step.check is None]
    graded_new = [name for name, step in new.steps.items() if step.check is None]
    for step_id in [*graded_new, *(each for each in graded_old if each not in graded_new)]:
        was, now = old.steps.get(step_id), new.steps.get(step_id)
        if was is None:
            changes.append(f"The step {step_id} was added.")
        elif now is None:
            changes.append(f"The step {step_id} was removed.")
        elif was.primitive != now.primitive:
            changes.append(f"The step {step_id} now uses {now.primitive}.")
        elif was.image != now.image:
            changes.append(f"A release updated {now.primitive.partition('/')[2]} in place.")
        elif _inputs(was, now.items) != _inputs(now, was.items):
            changes.append(f"The inputs of the step {step_id} changed.")
    runs_before = {run.id: run for run in old.runs}
    runs_after = {run.id: run for run in new.runs}
    added = [run for run in runs_after if run not in runs_before]
    removed = [run for run in runs_before if run not in runs_after]
    if added:
        changes.append(f"{_counted(added)} added.")
    if removed:
        changes.append(f"{_counted(removed)} removed.")
    changes += _compared(before.data, after.data, "The file {}")
    moved = any(
        runs_before[run].early != runs_after[run].early for run in runs_before if run in runs_after
    ) or {step.step for step in old.pipeline("early") if step.runs is None} != {
        step.step for step in new.pipeline("early") if step.runs is None
    }
    if moved:
        changes.append("What is shown during the contest moved, so other runs are graded early.")
    released_before = {run.id: run.early for run in old.for_split(True).runs}
    released_after = {run.id: run.early for run in new.for_split(True).runs}
    if not moved and any(
        released_before[run] != released_after[run]
        for run in released_before
        if run in released_after
    ):
        changes.append(RELEASE_MOVED)
    return tuple(dict.fromkeys(changes))


def pin_moved(before: Snapshot | None, after: Snapshot) -> bool:
    """Whether a save moves the workflow's pin (SIMPLER-CORE.md 11.8): the
    task names another version or another workflow than the previous
    publication's plan, or the same at another commit. A first
    publication, or one whose plan does not read, moves none."""
    if before is None or PLAN_PATH not in before.plans:
        return False
    try:
        old = RunPlan.from_bytes(before.plans[PLAN_PATH])
    except ValueError:
        return False
    new = RunPlan.from_bytes(after.plans[PLAN_PATH])
    return (old.workflow.ref, old.workflow.commit) != (new.workflow.ref, new.workflow.commit)


def steps_run_again(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """The steps a submission graded under the previous publication runs
    again under this one, in the order its pipelines run them, for the
    save's effect: each
    step of the early and late pipelines that was added, uses another
    primitive or image, is given other inputs, reads a task or workflow
    file whose content changed, or reads an output of a step that runs
    again, since the store runs again only what changed by key. A run added
    or removed moves no step's other runs. A step done at the save runs
    once, at the save, and is left out. Every graded step when the previous
    plan no longer reads; none for a first publication.
    """
    if before is None:
        return ()
    new = RunPlan.from_bytes(after.plans[PLAN_PATH])
    graded = dict.fromkeys(each.step for kind in ("early", "late") for each in new.pipeline(kind))
    try:
        old = RunPlan.from_bytes(before.plans[PLAN_PATH]) if PLAN_PATH in before.plans else None
    except ValueError:
        old = None
    if old is None:
        return tuple(graded)
    again: set[str] = set()
    moved = True
    while moved:
        moved = False
        for step_id, now in new.steps.items():
            if step_id not in again and _runs_again(old, new, step_id, now, again):
                again.add(step_id)
                moved = True
    return tuple(step_id for step_id in graded if step_id in again)


def _runs_again(old: RunPlan, new: RunPlan, step_id: str, now: RunStep, again: set[str]) -> bool:
    was = old.steps.get(step_id)
    if was is None or was.primitive != now.primitive or was.image != now.image:
        return True
    if _inputs(was, now.items) != _inputs(now, was.items):
        return True
    inputs = (
        [now.inputs]
        if now.inputs is not None
        else [each.inputs for each in (now.items or {}).values()]
    )
    for value in _leaves(value for given in inputs for value in given.values()):
        if "task" in value:
            paths = named_by(new.task_files, value["task"]) | named_by(
                old.task_files, value["task"]
            )
            if any(old.task_files.get(path) != new.task_files.get(path) for path in paths):
                return True
        elif "workflow" in value:
            ours, theirs = new.workflow.files, old.workflow.files
            paths = named_by(ours, value["workflow"]) | named_by(theirs, value["workflow"])
            if any(theirs.get(path) != ours.get(path) for path in paths):
                return True
        elif "step" in value and value["step"] in again:
            return True
        elif "runs" in value and (
            old.reports != new.reports or any(new.steps[each].value is None for each in again)
        ):
            # A program reads its runs' verdicts and reports, which a step
            # run again, or other reports, may change.
            return True
    return False


def _inputs(step: RunStep, other: Mapping[str, Item] | None) -> object:
    """What a step is given, each run's by its id among those `other`, the
    same step in the other plan, holds too, for comparing: a run added or
    removed is no change of the step's inputs.
    """
    if step.inputs is not None:
        return step.inputs
    return {run: item.inputs for run, item in (step.items or {}).items() if run in (other or {})}


def _compared(before: Mapping[str, object], after: Mapping[str, object], named: str) -> list[str]:
    found: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            found.append(f"{named.format(key)} was added.")
        elif key not in after:
            found.append(f"{named.format(key)} was removed.")
        elif before[key] != after[key]:
            found.append(f"{named.format(key)} changed.")
    return found


def _counted(runs: Sequence[str]) -> str:
    if len(runs) == 1:
        return f"The run {runs[0]} was"
    return f"The runs {', '.join(runs)} were"


__all__ = [
    "BOXED",
    "COMPILE_USE",
    "EXEC_USE",
    "PIPELINES",
    "PROGRAM_USES",
    "RELEASE_MOVED",
    "SCHEMA_VERSION",
    "FileFact",
    "FilesNamed",
    "FilesRead",
    "GradingKind",
    "Item",
    "PinnedWorkflow",
    "PipelineKind",
    "PipelineStep",
    "ReleasedSplit",
    "RunPlan",
    "RunPlanCheck",
    "RunStep",
    "StepClass",
    "ValueStep",
    "files_named",
    "order_problems",
    "pin_moved",
    "plan_runs",
    "run_plan_changes",
    "steps_run_again",
]
