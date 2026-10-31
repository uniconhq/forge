from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from forge.domain import exact_json
from forge.domain.content import named_by
from forge.domain.definitions import VALUE_NAME
from forge.domain.plan_basics import PRIMITIVE, Image, ReportEntry, StepLimits
from forge.domain.run_plan.plan_helpers import _leaves
from forge.domain.run_plan.records import (
    COMMIT,
    ONCE,
    PIPELINE_STEP,
    PIPELINES,
    SCHEMA_VERSION,
    SHA256,
    GradingKind,
    Inputs,
    PipelineKind,
    RunId,
    StepClass,
    StepId,
    Taint,
)
from forge.domain.types import Type
from forge.domain.yaml_models import Model, Problem


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


class PartDetails(Model):
    """One part of an entrant input: the tuple's title, and the most that
    part may hold, in bytes.
    """

    title: str | None = None
    max_size: Annotated[int, Field(ge=1)]


class EntrantEntry(Model):
    """An input the entrant gives: its type, an enum's options, the parts it
    is given in, and on a file or folder the most it may hold, in bytes.
    `max_size` is the largest part's when the input is given in parts.
    `part_details` is one per part, in order, when a part has a title or
    its own size.
    """

    type: Type
    options: tuple[str, ...] | None = None
    parts: Annotated[int, Field(ge=1)] | None = None
    max_size: Annotated[int, Field(ge=1)] | None = None
    part_details: tuple[PartDetails, ...] | None = None


class RunEntry(Model):
    """A run: its id, its key with a part as `{part: <n>}`, whether it is
    early, and its tuples' titles, one per axis, none where that tuple
    has none.
    """

    id: RunId
    key: dict[str, Any]
    early: bool
    titles: tuple[str | None, ...] | None = None


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
    A step graded per run says what its primitive gives the same for every
    item (`once_outputs`, `once_outcomes`), and a step that compiles the
    entrant's code says `compiled` reads it. A value's program is two steps
    of its own (`value`): compiled once at the save, and run once per
    grading after the runs it reads. A check's copy of a step graded per
    submission says which (`check`).
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
    once_outputs: tuple[str, ...] | None = None
    once_outcomes: tuple[str, ...] | None = None
    compiled: Literal[True] | None = None
    tainted: Taint | None = None
    value: ValueStep | None = None
    check: CheckStep | None = None
    woodpecker: Annotated[str, Field(pattern=r"^s[0-9]+$")] | None = None
    inputs: Inputs | None = None
    items: dict[RunId, Item] | None = None

    @property
    def once(self) -> bool:
        return self.class_ in ONCE


class PipelineStep(Model):
    """One step of a pipeline: its name inside the plan, the workflow step it
    runs, the runs it holds, none for a step done once, the steps of the same
    pipeline it waits for, and its container's limits. The Woodpecker file
    names it by `RunPlan.woodpecker_name`.
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
    """A task's plan, in the shape of the runner's plan schema
    version 6.
    """

    schema_version: Literal[6] = SCHEMA_VERSION
    workflow: PinnedWorkflow
    inner_workflows: tuple[PinnedWorkflow, ...] = ()
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
        if not document.get("inner_workflows"):
            document.pop("inner_workflows", None)
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

    def pinned(self, pin: int) -> PinnedWorkflow:
        """The workflow of pin `pin`: the task's own for 0, and inner pin k
        at index k - 1."""
        if pin == 0:
            return self.workflow
        return self.inner_workflows[pin - 1]

    def pipeline(self, kind: PipelineKind) -> tuple[PipelineStep, ...]:
        """The steps of one pipeline, none when it is left out."""
        return self.pipelines.get(kind, ())

    def woodpecker_name(self, name: str) -> str:
        """The Woodpecker name of a plan name: the step's `woodpecker`, then
        `.<run>` when `name` is one run of it. A step with no `woodpecker`,
        and a name that is not a step's, such as the report step, is `name`
        itself, so a plan written before steps carried one runs as it did.
        """
        base, dot, run = name.rpartition(".")
        key = base if dot and base in self.steps else name
        if key not in self.steps:
            return name
        named = self.steps[key].woodpecker or key
        return f"{named}.{run}" if key == base else named

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
                    pin = value.get("pin", 0)
                    workflow |= named_by(self.pinned(pin).files, value["workflow"])
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
    """Every problem a task's plan is refused for, each at its path in
    `task.yaml`, and the plan when there is none.
    """

    problems: list[Problem]
    plan: RunPlan | None


@dataclass(frozen=True, slots=True)
class FilesNamed:
    """The task's files and folders, and the workflow's, that a step is
    given in some run, a folder ending in `/`. `inner` is the same for
    each pin from 1.
    """

    task: frozenset[str]
    workflow: frozenset[str]
    inner: dict[int, frozenset[str]] = field(default_factory=dict)
