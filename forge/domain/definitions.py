"""The two files an organiser defines a contest and a task with, as models:
`contest.yaml` (TASK-FORMAT.md section 5) and `task.yaml` (section 3). Both
are validated on every save. A key the format does not know is refused, so a
typo is an error, and every error carries the YAML path it is at.

Some keys are the frame of a scope and belong to its admin (PROPOSAL.md
section 10): `CONTEST_ADMIN_KEYS`, `TASK_ADMIN_KEYS`, and the statement,
`ADMIN_ONLY_FILES`. `admin_only_changes` names the ones a save changes, so a
manager's save that touches one can be refused naming it.

The starter files are what a new contest or task is created with; each is
valid as written.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

import regex
from pydantic import Field, PlainValidator, model_validator

from forge.domain.errors import InvalidName
from forge.domain.names import validate_contest_or_task_name
from forge.domain.workflow_definition import InputType, Ref, WorkflowRef
from forge.domain.yaml_models import (
    AwareTime,
    Handle,
    InvalidDefinition,
    Model,
    Number,
    Path,
    Problem,
    Problems,
    Size,
    Text,
    format_size,
    is_number,
    load_mapping,
    mapping_or_empty,
    parse_size,
    path_text,
    validate,
)

__all__ = [
    "ADMIN_ONLY_FILES",
    "CONTEST_ADMIN_KEYS",
    "CONTEST_FILE",
    "DEFAULT_STAGE",
    "STATEMENT_FILE",
    "TASK_ADMIN_KEYS",
    "TASK_FILE",
    "Approval",
    "BoardVisibility",
    "Combine",
    "ContestDefinition",
    "ContestTask",
    "ContestVisibility",
    "ContestantInput",
    "Direction",
    "Eligibility",
    "Inputs",
    "InvalidDefinition",
    "Leaderboard",
    "Limits",
    "OrderEntry",
    "OrderKey",
    "Overrides",
    "Rate",
    "Registration",
    "RegistrationMode",
    "ResolvedStage",
    "Select",
    "SetterInput",
    "Show",
    "Stage",
    "State",
    "Subtask",
    "TaskDefinition",
    "Teams",
    "Trigger",
    "admin_only_changes",
    "parse_contest",
    "parse_rate",
    "parse_task",
    "starter_contest",
    "starter_task",
    "title_of",
]

CONTEST_FILE = "contest.yaml"
TASK_FILE = "task.yaml"
STATEMENT_FILE = "statement.md"

Flag = Annotated[bool, Field(strict=True)]


class State(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ContestVisibility(StrEnum):
    """Who sees the contest: visitors and everyone signed in, anyone with a
    session, or only its contestants and organisers.
    """

    PUBLIC = "public"
    SIGNED_IN = "signed-in"
    HIDDEN = "hidden"


class RegistrationMode(StrEnum):
    OPEN = "open"
    INVITE_ONLY = "invite-only"


class Approval(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Select(StrEnum):
    BEST = "best"
    LATEST = "latest"
    SELECTED = "selected"
    FIRST_ACCEPTED = "first_accepted"


class Combine(StrEnum):
    SUM = "sum"
    MEAN = "mean"
    COUNT = "count"


class Direction(StrEnum):
    ASC = "asc"
    DESC = "desc"


class OrderKey(StrEnum):
    """A ranking key the platform computes: `last_improvement`, earlier
    wins; `penalty`, minutes to the counted submission plus `per_rejected`
    for each rejected one before it.
    """

    LAST_IMPROVEMENT = "last_improvement"
    PENALTY = "penalty"


class BoardVisibility(StrEnum):
    PUBLIC = "public"
    CONTESTANTS = "contestants"
    ORGANISERS = "organisers"


def _pattern(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be text: a regular expression.")
    try:
        regex.compile(value)
    except regex.error as error:
        raise ValueError(f"Is not a regular expression: {error}.") from None
    return value


def _task_handle(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be text.")
    try:
        return validate_contest_or_task_name(value)
    except InvalidName:
        raise ValueError(
            f"{value!r} must be lower case letters, digits, hyphens and underscores, "
            "starting with a letter or a digit, at most 24 characters."
        ) from None


TaskHandle = Annotated[str, PlainValidator(_task_handle)]


class Eligibility(Model):
    """Who may register: an email address matching `email_pattern`, and the
    code a contestant types when `invite_code` is set. Both are off unless
    given.
    """

    email_pattern: Annotated[str, PlainValidator(_pattern)] | None = None
    invite_code: Text | None = None


class Registration(Model):
    """Who may enter and how. By default anyone may register at any time,
    each registration waits for an organiser's approval, and there is no
    cap on the number of contestants.
    """

    mode: RegistrationMode = RegistrationMode.OPEN
    opens: AwareTime | None = None
    closes: AwareTime | None = None
    approval: Approval = Approval.MANUAL
    eligibility: Eligibility = Eligibility()
    capacity: Annotated[int, Field(strict=True, ge=1)] | None = None


class Teams(Model):
    """Teams are off unless enabled; an enabled team holds up to three
    unless `max_size` says otherwise.
    """

    enabled: Flag = False
    max_size: Annotated[int, Field(strict=True, ge=1)] = 3


class OrderEntry(Model):
    """One ranking criterion: a metric the workflow reports, ranked in a
    direction, or a key the platform computes.
    """

    metric: Text | None = None
    direction: Direction | None = None
    key: OrderKey | None = None
    per_rejected: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def _check(self) -> OrderEntry:
        problems = Problems()
        if self.metric is None and self.key is None:
            problems.add((), "Each entry names a metric with a direction, or a built-in key.")
        if self.metric is not None and self.key is not None:
            problems.add(("key",), "Give either metric or key, not both.")
        if self.metric is not None and self.direction is None:
            problems.add(("direction",), "A metric needs a direction, asc or desc.")
        if self.metric is None and self.direction is not None:
            problems.add(("direction",), "Applies only to a metric; a built-in key has its own.")
        if self.per_rejected is not None and self.key is not OrderKey.PENALTY:
            problems.add(("per_rejected",), "Applies only to key: penalty.")
        problems.raise_any()
        return self


class Leaderboard(Model):
    """One board. `tasks` left out ranks every task in the contest; `stage`
    left out reads the gradings of the stage `default`.
    """

    name: Text
    tasks: tuple[TaskHandle, ...] | None = None
    stage: Handle = "default"
    select: Select
    combine: Combine
    order: tuple[OrderEntry, ...] = Field(min_length=1)
    visibility: BoardVisibility
    freeze_at: AwareTime | None = None
    team_only: Flag = False


class ContestTask(Model):
    """A task's place in the contest: its id, the label shown for it and the
    points it is worth.
    """

    id: TaskHandle
    label: Text
    points: Annotated[int, Field(strict=True, ge=0)]


class ContestDefinition(Model):
    """A `contest.yaml`. `description` defaults to empty, `submissions_closed`
    to off, `registration` to open with manual approval, `teams` to off,
    and `leaderboards` and `tasks` to none.
    """

    name: Text
    description: str = ""
    start: AwareTime
    end: AwareTime
    state: State
    submissions_closed: Flag = False
    visibility: ContestVisibility
    registration: Registration = Registration()
    teams: Teams = Teams()
    leaderboards: tuple[Leaderboard, ...] = ()
    tasks: tuple[ContestTask, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> ContestDefinition:
        problems = Problems()
        if self.end <= self.start:
            problems.add(("end",), "Must be after start.")
        opens, closes = self.registration.opens, self.registration.closes
        if opens is not None and closes is not None and closes <= opens:
            problems.add(("registration", "closes"), "Must be after registration.opens.")
        problems.duplicates([task.id for task in self.tasks], ("tasks",))
        problems.duplicates([task.label for task in self.tasks], ("tasks",), "label")
        problems.duplicates([board.name for board in self.leaderboards], ("leaderboards",), "name")
        known = {task.id for task in self.tasks}
        for index, board in enumerate(self.leaderboards):
            for position, task in enumerate(board.tasks or ()):
                if task not in known:
                    problems.add(
                        ("leaderboards", index, "tasks", position),
                        f"{task!r} is not in the contest's tasks.",
                    )
            if board.team_only and not self.teams.enabled:
                problems.add(
                    ("leaderboards", index, "team_only"), "Needs teams.enabled to be true."
                )
        problems.raise_any()
        return self


class Trigger(StrEnum):
    ON_SUBMIT = "on_submit"
    ON_SELECT = "on_select"
    AT_END = "at_end"
    MANUAL = "manual"


class Show(StrEnum):
    """What a contestant sees of a stage's result."""

    FULL = "full"
    METRICS = "metrics"
    HIDDEN = "hidden"


FILE_TYPES = frozenset({InputType.FILE, InputType.FILES, InputType.DATASET})
DEFAULT_STAGE = "default"


def _path_problem(value: object, *, folder: bool | None) -> str | None:
    """What is wrong with `value` as a path in the task repo, if anything.
    `folder` says whether it must name a folder, one file, or either.
    """
    if not isinstance(value, str) or not value:
        return "Must be a path in the task repo, such as data/testcases/."
    if value.startswith("/") or "\\" in value:
        return "Must be a path from the top of the task repo, with forward slashes."
    if any(part in ("", ".", "..") for part in value.removesuffix("/").split("/")):
        return "Must not have empty, . or .. parts."
    if folder is True and not value.endswith("/"):
        return "Names a folder, so it ends with /, such as data/testcases/."
    if folder is False and value.endswith("/"):
        return "Names one file, so it does not end with /."
    return None


def _value_problem(kind: InputType, value: object) -> str | None:
    """What is wrong with `value` as a setter's value of type `kind`."""
    match kind:
        case InputType.CODE | InputType.TEXT:
            return None if isinstance(value, str) else "Must be text."
        case InputType.NUMBER:
            return None if is_number(value) else "Must be a number."
        case InputType.BOOLEAN:
            return None if isinstance(value, bool) else "Must be true or false."
        case InputType.FILE:
            return _path_problem(value, folder=False)
        case InputType.FILES:
            return _path_problem(value, folder=True)
        case InputType.DATASET:
            return _path_problem(value, folder=None)
        case InputType.JUPYTER:
            return "A jupyter input is the contestant's; a setter cannot give one."


class ContestantInput(Model):
    """An input the contestant gives, with the fields its form shows:
    `language` for code, `min` and `max` for a number, `accept` and
    `max_size` for a file, and a `default` for code, text, a number or a
    true-or-false. `label` defaults to the id.
    """

    id: Handle
    type: InputType
    label: Text | None = None
    language: tuple[Text, ...] | None = Field(default=None, min_length=1)
    min: Number | None = None
    max: Number | None = None
    accept: tuple[Text, ...] | None = Field(default=None, min_length=1)
    max_size: Size | None = None
    default: Any = None

    @model_validator(mode="after")
    def _check(self) -> ContestantInput:
        problems = Problems()
        kind = self.type
        if kind is InputType.DATASET:
            problems.add(("type",), "A dataset is the setter's; a contestant cannot give one.")
        if self.language is not None and kind is not InputType.CODE:
            problems.add(("language",), "Applies only to a code input.")
        for key in ("min", "max"):
            if getattr(self, key) is not None and kind is not InputType.NUMBER:
                problems.add((key,), "Applies only to a number input.")
        if self.min is not None and self.max is not None and self.max < self.min:
            problems.add(("max",), "Must be at least min.")
        for key in ("accept", "max_size"):
            if getattr(self, key) is not None and kind not in (InputType.FILE, InputType.FILES):
                problems.add((key,), "Applies only to a file or file[] input.")
        if self.default is not None:
            problem = self._default_problem()
            if problem:
                problems.add(("default",), problem)
        problems.raise_any()
        return self

    def _default_problem(self) -> str | None:
        if self.type in (*FILE_TYPES, InputType.JUPYTER):
            return f"A {self.type} input has no default."
        problem = _value_problem(self.type, self.default)
        if problem or self.type is not InputType.NUMBER:
            return problem
        if self.min is not None and self.default < self.min:
            return "Must be at least min."
        if self.max is not None and self.default > self.max:
            return "Must be at most max."
        return None


class SetterInput(Model):
    """An input the setter gives, with its `value`: text, a number or a
    true-or-false as written, or for a file a path in the task repo. A
    `file[]` value names a folder and ends with `/`; a `dataset` may name
    a file or a folder.
    """

    id: Handle
    type: InputType
    value: Any

    @model_validator(mode="after")
    def _check(self) -> SetterInput:
        problem = _value_problem(self.type, self.value)
        if problem:
            key = "type" if self.type is InputType.JUPYTER else "value"
            problems = Problems()
            problems.add((key,), problem)
            problems.raise_any()
        return self

    @property
    def names_file(self) -> bool:
        return self.type in FILE_TYPES


class Inputs(Model):
    contestant: tuple[ContestantInput, ...] = ()
    setter: tuple[SetterInput, ...] = ()


class Overrides(Model):
    """The setter inputs a subtask or a stage gives in place of the task's,
    or in addition to them.
    """

    setter: tuple[SetterInput, ...] = ()


_RATE = re.compile(r"^\s*(\d+)\s+per\s+(\d+)\s*([smh])\s*$")
_RATE_UNITS = {"s": 1, "m": 60, "h": 3600}


@dataclass(frozen=True, slots=True)
class Rate:
    """At most `count` submissions in any window of length `per`."""

    count: int
    per: timedelta

    def __str__(self) -> str:
        return f"{self.count} per {int(self.per.total_seconds())}s"


def parse_rate(value: object) -> Rate:
    """`N per Xs`, `N per Xm` or `N per Xh` as a `Rate`."""
    if isinstance(value, Rate):
        return value
    match = _RATE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Must be a rate such as 1 per 30s, 5 per 10m or 20 per 1h.")
    count, length = int(match.group(1)), int(match.group(2))
    if count < 1 or length < 1:
        raise ValueError("Both numbers in a rate must be at least 1.")
    return Rate(count, timedelta(seconds=length * _RATE_UNITS[match.group(3)]))


DEFAULT_SUBMISSIONS = 50
DEFAULT_RATE = Rate(1, timedelta(seconds=30))
DEFAULT_MAX_SIZE = parse_size("10MB")
SUBMISSION_CEILING = parse_size("64MB")
"""The most any submission may be, whatever a task allows. A submit reads
its files whole and writes them to the forge in one request, so the ceiling
is what bounds the memory one submit takes; a larger one waits for files to
be streamed into the submission repo."""


class Limits(Model):
    """What a submit is checked against. Each has a default: 50 submissions
    per contestant, at most one every 30 seconds, of at most 10MB, where a
    megabyte is 1024 kilobytes of 1024 bytes.
    """

    submissions: Annotated[int, Field(strict=True, ge=1)] = DEFAULT_SUBMISSIONS
    rate: Annotated[Rate, PlainValidator(parse_rate)] = DEFAULT_RATE
    max_size: Size = DEFAULT_MAX_SIZE

    def as_mapping(self) -> dict[str, object]:
        """The limits in the form a publication records and compares them."""
        return {
            "submissions": self.submissions,
            "rate": str(self.rate),
            "max_size": self.max_size,
        }


class Subtask(Model):
    id: Handle
    points: Annotated[int, Field(strict=True, ge=0)]
    workflow: Ref | None = None
    inputs: Overrides = Overrides()


class Stage(Model):
    """One grading round. Graded on submit, its result shown in full and
    counted for the leaderboards unless it says otherwise.
    """

    id: Handle
    trigger: Trigger = Trigger.ON_SUBMIT
    show: Show = Show.FULL
    counts: Flag = True
    workflow: Ref | None = None
    inputs: Overrides = Overrides()


@dataclass(frozen=True, slots=True)
class ResolvedStage:
    """A stage with the inheritance rule applied: its own workflow or the
    task's, and the task's setter inputs with the stage's in their place.
    `index` is its place in `stages`, or none for the implicit `default`.
    """

    id: str
    trigger: Trigger
    show: Show
    counts: bool
    workflow: WorkflowRef
    setter: tuple[SetterInput, ...]
    index: int | None


def _overlay(base: Sequence[SetterInput], over: Sequence[SetterInput]) -> tuple[SetterInput, ...]:
    replaced = {entry.id: entry for entry in over}
    merged = [replaced.pop(entry.id, entry) for entry in base]
    return (*merged, *(entry for entry in over if entry.id in replaced))


class TaskDefinition(Model):
    """A `task.yaml`. `release_at` left out releases the task when the
    contest starts; `hidden` defaults to off; `limits` to the defaults in
    `Limits`. With no `stages` the task has one, `default`, graded on
    submit, shown in full and counted.
    """

    name: Text
    workflow: Ref
    release_at: AwareTime | None = None
    hidden: Flag = False
    inputs: Inputs = Inputs()
    limits: Limits = Limits()
    subtasks: tuple[Subtask, ...] = ()
    stages: tuple[Stage, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> TaskDefinition:
        problems = Problems()
        problems.duplicates(
            [entry.id for entry in self.inputs.contestant], ("inputs", "contestant")
        )
        problems.duplicates([entry.id for entry in self.inputs.setter], ("inputs", "setter"))
        problems.duplicates([subtask.id for subtask in self.subtasks], ("subtasks",))
        problems.duplicates([stage.id for stage in self.stages], ("stages",))
        types = {entry.id: entry.type for entry in self.inputs.setter}
        overrides: list[tuple[Path, Overrides]] = [
            *((("subtasks", index), subtask.inputs) for index, subtask in enumerate(self.subtasks)),
            *((("stages", index), stage.inputs) for index, stage in enumerate(self.stages)),
        ]
        for place, given in overrides:
            at: Path = (*place, "inputs", "setter")
            problems.duplicates([entry.id for entry in given.setter], at)
            for position, entry in enumerate(given.setter):
                known = types.get(entry.id)
                if known is not None and known is not entry.type:
                    problems.add(
                        (*at, position, "type"),
                        f"Must be {known}, the type {entry.id} has at the task level.",
                    )
        problems.raise_any()
        return self

    def stages_resolved(self) -> tuple[ResolvedStage, ...]:
        """Every stage with its workflow and setter inputs resolved."""
        if not self.stages:
            return (
                ResolvedStage(
                    id=DEFAULT_STAGE,
                    trigger=Trigger.ON_SUBMIT,
                    show=Show.FULL,
                    counts=True,
                    workflow=self.workflow,
                    setter=self.inputs.setter,
                    index=None,
                ),
            )
        return tuple(
            ResolvedStage(
                id=stage.id,
                trigger=stage.trigger,
                show=stage.show,
                counts=stage.counts,
                workflow=stage.workflow or self.workflow,
                setter=_overlay(self.inputs.setter, stage.inputs.setter),
                index=index,
            )
            for index, stage in enumerate(self.stages)
        )

    def workflow_refs(self) -> tuple[tuple[WorkflowRef, str], ...]:
        """Each workflow the stages grade with, once, in the order the stages
        are listed, with the YAML path of the first place that names it: the
        task's `workflow`, or a stage's own. These are the workflows read
        before the task compiles.
        """
        found: dict[str, tuple[WorkflowRef, str]] = {}
        for stage in self.stages_resolved():
            own = stage.index is not None and self.stages[stage.index].workflow is not None
            at = f"stages[{stage.index}].workflow" if own else "workflow"
            found.setdefault(str(stage.workflow), (stage.workflow, at))
        return tuple(found.values())

    def named_files(self) -> tuple[str, ...]:
        """Every path in the task repo a file, file[] or dataset setter input
        names, at the task level, in a subtask or in a stage, sorted. A path
        ending in `/` names a folder.
        """
        entries = [
            *self.inputs.setter,
            *(entry for subtask in self.subtasks for entry in subtask.inputs.setter),
            *(entry for stage in self.stages for entry in stage.inputs.setter),
        ]
        return tuple(sorted({entry.value for entry in entries if entry.names_file}))

    def file_inputs(self) -> tuple[tuple[str, str], ...]:
        """Every file, file[] or dataset setter input as the YAML path of its
        value and the path in the task repo it names, at the task level,
        then in each subtask, then in each stage.
        """
        places: list[tuple[Path, Sequence[SetterInput]]] = [
            (("inputs", "setter"), self.inputs.setter),
            *(
                (("subtasks", index, "inputs", "setter"), subtask.inputs.setter)
                for index, subtask in enumerate(self.subtasks)
            ),
            *(
                (("stages", index, "inputs", "setter"), stage.inputs.setter)
                for index, stage in enumerate(self.stages)
            ),
        ]
        return tuple(
            (path_text((*at, position, "value")), str(entry.value))
            for at, entries in places
            for position, entry in enumerate(entries)
            if entry.names_file
        )

    def oversized(self) -> list[Problem]:
        """A problem at every size limit above `SUBMISSION_CEILING`: the
        task's `limits.max_size` and each contestant input's `max_size`.
        """
        message = (
            f"Must be at most {format_size(SUBMISSION_CEILING)}, the largest submission "
            "the platform takes."
        )
        problems: list[Problem] = []
        if self.limits.max_size > SUBMISSION_CEILING:
            problems.append(Problem(path="limits.max_size", message=message))
        for index, entry in enumerate(self.inputs.contestant):
            if entry.max_size is not None and entry.max_size > SUBMISSION_CEILING:
                problems.append(
                    Problem(path=f"inputs.contestant[{index}].max_size", message=message)
                )
        return problems

    def missing_files(self, has: Callable[[str], bool]) -> list[Problem]:
        """A problem at the value of every file input whose path `has` says
        is not in the state being saved. A path ending in `/` is a folder,
        there when some file is under it.
        """
        problems: list[Problem] = []
        for at, path in self.file_inputs():
            if not has(path):
                message = (
                    f"There is no file under {path} in the task."
                    if path.endswith("/")
                    else f"There is no file {path} in the task."
                )
                problems.append(Problem(path=at, message=message))
        return problems


def parse_contest(text: bytes | str) -> ContestDefinition:
    """The `contest.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(ContestDefinition, CONTEST_FILE, load_mapping(CONTEST_FILE, text))


def parse_task(text: bytes | str) -> TaskDefinition:
    """The `task.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(TaskDefinition, TASK_FILE, load_mapping(TASK_FILE, text))


CONTEST_ADMIN_KEYS = (
    "name",
    "description",
    "start",
    "end",
    "submissions_closed",
    "state",
    "visibility",
    "registration",
)
TASK_ADMIN_KEYS = ("name", "limits")
ADMIN_ONLY_FILES = (STATEMENT_FILE,)

_MISSING = object()


def admin_only_changes(
    kind: Literal["contest", "task"], before: bytes | None, after: bytes
) -> list[str]:
    """The admin-only top-level keys whose values differ between `before`
    and `after`, in the order the keys are listed. `before` is none for a
    new file. A key added or removed counts as changed. A text that does not
    parse compares as an empty mapping, because reporting it is
    validation's work.
    """
    keys = CONTEST_ADMIN_KEYS if kind == "contest" else TASK_ADMIN_KEYS
    old, new = mapping_or_empty(before), mapping_or_empty(after)
    return [key for key in keys if old.get(key, _MISSING) != new.get(key, _MISSING)]


def title_of(title: str | None, name: str) -> str:
    """The title a new contest or task is created with: the one asked for,
    or its name when none is given or the one given is blank, so the starter
    file is valid as written.
    """
    return title if title and title.strip() else name


def _yaml_text(value: str) -> str:
    """`value` as a YAML scalar in double quotes; a JSON string is one."""
    return json.dumps(value, ensure_ascii=False)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def starter_contest(name: str, now: datetime) -> bytes:
    """The `contest.yaml` a new contest is created with: titled `name`, a
    draft seen by anyone signed in, open to register with manual approval,
    starting on the first full hour at least seven days after `now` and
    running five hours, with teams off and no leaderboards or tasks.
    """
    week_on = now.astimezone(UTC) + timedelta(days=7)
    start = week_on.replace(minute=0, second=0, microsecond=0)
    if start < week_on:
        start += timedelta(hours=1)
    end = start + timedelta(hours=5)
    return f"""\
# The contest's settings. The format is TASK-FORMAT.md, section 5.
name: {_yaml_text(name)}
description: ""
start: {_stamp(start)}
end: {_stamp(end)}
state: draft
submissions_closed: false
visibility: signed-in

registration:
  mode: open
  approval: manual

teams:
  enabled: false
  max_size: 3

leaderboards: []

tasks: []
""".encode()


def starter_task(name: str) -> dict[str, bytes]:
    """The files a new task is created with: a `task.yaml` titled `name` that
    grades Python with `unicon/classic@v1` against the testcases in
    `data/testcases/`, a placeholder statement, one example testcase, `1.in`
    with its answer `1.ans`, so the first save compiles a plan that grades.
    """
    task = f"""\
# The task's settings. The format is TASK-FORMAT.md, section 3.
name: {_yaml_text(name)}
workflow: unicon/classic@v1

inputs:
  contestant:
    - id: submission
      type: code
      label: Your solution
      language: [python]
  setter:
    - id: testcases
      type: file[]
      value: data/testcases/
    - id: time_limit
      type: number
      value: 2.0
    - id: memory_limit
      type: number
      value: 256

limits:
  submissions: {DEFAULT_SUBMISSIONS}
  rate: {DEFAULT_RATE.count} per {int(DEFAULT_RATE.per.total_seconds())}s
  max_size: {format_size(DEFAULT_MAX_SIZE)}
"""
    return {
        TASK_FILE: task.encode(),
        STATEMENT_FILE: b"Write the statement contestants read here.\n",
        "data/testcases/1.in": b"1 2\n",
        "data/testcases/1.ans": b"3\n",
    }
