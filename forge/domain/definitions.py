"""The two files an organiser defines a contest and a task with, as models:
`contest.yaml` (TASK-FORMAT.md section 1.1) and `task.yaml` (section 1.2).
Both are validated on every save. A key the format does not know is refused,
so a typo is an error, and a key the format no longer has is refused at that
key with the sentence that says what replaced it. Every error carries the
YAML path it is at.

What each file says of itself is checked here: a contest's times in order,
each task entry's timeline in order (C1, the part that needs no task), each
board's own rules (C3); a task's test groups, their rule weights, test
weights and `show` words, and its `credit` (T1, T2 and T6, and the parts of
T3 that need no workflow). What needs another file, the workflow a task
names, its tests, an earlier publication or the rows' submissions, is the
save's (`forge.domain.plans`, `forge.services.publications`,
`forge.services.timelines`).

Some keys are the frame of a scope and belong to its admin (PROPOSAL.md
section 10): `CONTEST_ADMIN_KEYS`, `TASK_ADMIN_KEYS`, and the statement,
`ADMIN_ONLY_FILES`. `admin_only_changes` names the ones a save changes, so a
manager's save that touches one can be refused naming it.

The starter files are what a new contest or task is created with; each is
valid as written.
"""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal

import regex
from pydantic import Field, PlainValidator, ValidationError, model_validator

from forge.domain.errors import InvalidName
from forge.domain.names import validate_contest_or_task_name
from forge.domain.workflow_definition import Options, Ref
from forge.domain.yaml_models import (
    ANY,
    AwareTime,
    InvalidDefinition,
    Line,
    Model,
    Number,
    Problems,
    Retired,
    Size,
    fail,
    is_number,
    load_mapping,
    mapping_or_empty,
    parse_size,
    problems_of,
    read_time,
    validate,
)

__all__ = [
    "ADMIN_ONLY_FILES",
    "CONTEST_ADMIN_KEYS",
    "CONTEST_FILE",
    "STATEMENT_FILE",
    "TASK_ADMIN_KEYS",
    "TASK_FILE",
    "Approval",
    "ContestDefinition",
    "ContestTask",
    "ContestVisibility",
    "Entrants",
    "Form",
    "Group",
    "InvalidDefinition",
    "InviteWay",
    "Leaderboard",
    "Locks",
    "OnSystemError",
    "Over",
    "Penalty",
    "Rate",
    "Registration",
    "Relative",
    "Select",
    "Show",
    "SignupWay",
    "State",
    "Submissions",
    "TaskDefinition",
    "Way",
    "Who",
    "admin_only_changes",
    "parse_contest",
    "parse_task",
    "starter_contest",
    "starter_task",
    "title_of",
    "worth_of",
]

CONTEST_FILE = "contest.yaml"
TASK_FILE = "task.yaml"
STATEMENT_FILE = "statement.md"
TESTS_FOLDER = "tests/"
PUBLIC_FOLDER = "public/"

Flag = Annotated[bool, Field(strict=True)]


class State(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ContestVisibility(StrEnum):
    """Who sees the contest at all: anyone, guests included; anyone signed
    in; or only its contestants and organisers.
    """

    EVERYONE = "everyone"
    SIGNED_IN = "signed-in"
    HIDDEN = "hidden"


class Approval(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Over(StrEnum):
    """Which test groups a board counts, by each group's declared `show`."""

    ALL = "all"
    LIVE = "live"
    AFTER_CLOSE = "after_close"


class Select(StrEnum):
    """Which of a row's submissions to a task a board counts."""

    BEST = "best"
    BEST_PER_GROUP = "best_per_group"
    MARKED = "marked"


class Who(StrEnum):
    """Who sees a board, among those who may see the contest."""

    ORGANISERS = "organisers"
    CONTESTANTS = "contestants"
    EVERYONE = "everyone"


class OnSystemError(StrEnum):
    """What a submission whose latest attempt is a `system_error`, or one
    staff cancelled after one, counts as: still grading, or void once
    cancelled, until staff end it; or the latest earlier attempt of it that
    finished with a result, while there is one.
    """

    GRADING = "grading"
    LAST_RESULT = "last_result"


class Show(StrEnum):
    """When contestants see a test group's results: as soon as a grading
    ends; its outcome and points now and its tests at the task's reveal; or
    everything but its name and its most points at the reveal.
    """

    ALWAYS = "always"
    VERDICT = "verdict"
    AFTER_CLOSE = "after_close"


def _pattern(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise ValueError("Must be one line: a regular expression.")
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
Count = Annotated[int, Field(strict=True, ge=1)]
Seconds = Annotated[int, Field(strict=True, ge=1)]
VALUE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
"""A domain a sign-up takes addresses at: labels of lower-case letters,
digits and hyphens, two or more, joined by dots, no label starting or ending
with a hyphen."""


def _domain(value: object) -> str:
    if not isinstance(value, str) or not DOMAIN.fullmatch(value):
        raise ValueError(
            "Must be a domain such as u.nus.edu: lower case letters, digits, hyphens and "
            "dots, with at least one dot, and no dot or hyphen at either end."
        )
    return value


Domain = Annotated[str, PlainValidator(_domain)]


class Locks(StrEnum):
    """When an entrant's roster locks: at the contest's start, or at the
    entrant's first submission.
    """

    START = "start"
    FIRST_SUBMISSION = "first_submission"


class InviteWay(Model):
    """`invite`: the people an organiser invited to the contest, who type
    nothing. Approved straight away unless `approval` says an organiser
    approves them, since an organiser who invited someone chose them.
    """

    approval: Approval = Approval.AUTO


class SignupWay(Model):
    """`signup`: people who register themselves, under every condition it
    gives: the `code`, and one confirmed address that is at one of the
    `domains` and matches `email_pattern`. One with no condition admits
    anyone with an account. `name` is shown to organisers beside each person
    who came in by it. An organiser approves each one unless `approval` says
    otherwise.
    """

    name: Line | None = None
    code: Line | None = None
    domains: Annotated[tuple[Domain, ...], Field(min_length=1)] | None = None
    email_pattern: Annotated[str, PlainValidator(_pattern)] | None = None
    approval: Approval = Approval.MANUAL

    @model_validator(mode="after")
    def _check(self) -> SignupWay:
        problems = Problems()
        domains = list(self.domains or ())
        for index, domain in enumerate(domains):
            if domain in domains[:index]:
                problems.add(("domains", index), f"{domain!r} is given more than once.")
        problems.raise_any()
        return self

    @property
    def asks_address(self) -> bool:
        """Whether it takes only some addresses."""
        return self.domains is not None or self.email_pattern is not None


class Way(Model):
    """One way into the contest: exactly one of `invite` and `signup`."""

    invite: InviteWay | None = None
    signup: SignupWay | None = None

    @model_validator(mode="before")
    @classmethod
    def _bare(cls, value: object) -> object:
        """A way written with nothing under its key, `- invite:`, is that
        way with every default.
        """
        if isinstance(value, dict):
            return {key: {} if found is None else found for key, found in value.items()}
        return value

    @model_validator(mode="after")
    def _one(self) -> Way:
        if (self.invite is None) == (self.signup is None):
            fail([((), "A way is one of invite: {...} or signup: {...}.")])
        return self

    @property
    def approval(self) -> Approval:
        if self.invite is not None:
            return self.invite.approval
        assert self.signup is not None
        return self.signup.approval

    @property
    def name(self) -> str | None:
        return self.signup.name if self.signup is not None else None


DEFAULT_WAYS = (Way(signup=SignupWay()),)
"""The ways of a contest that lists none: anyone with an account signs up,
and an organiser approves each one."""


class Registration(Model):
    """Who may enter and how: when the window is open, from `opens` until
    `closes`, always without them; at most `capacity` entrants pending or
    approved, no cap without it; and the `ways` someone qualifies by, any one
    enough, by default one sign-up that admits anyone with an account and
    waits for an organiser's approval. With no ways at all nobody may
    register.
    """

    opens: AwareTime | None = None
    closes: AwareTime | None = None
    capacity: Count | None = None
    ways: tuple[Way, ...] = DEFAULT_WAYS

    @model_validator(mode="after")
    def _check(self) -> Registration:
        problems = Problems()
        invites = [index for index, way in enumerate(self.ways) if way.invite is not None]
        for index in invites[1:]:
            problems.add(("ways", index, "invite"), "A contest has at most one invite way.")
        seen: set[str] = set()
        for index, way in enumerate(self.ways):
            if way.name is None:
                continue
            if way.name in seen:
                problems.add(
                    ("ways", index, "signup", "name"), f"{way.name!r} is given more than once."
                )
            seen.add(way.name)
        problems.raise_any()
        return self

    @property
    def invite_way(self) -> int | None:
        """The index of the contest's invite way, or none."""
        return next((index for index, way in enumerate(self.ways) if way.invite is not None), None)


class Entrants(Model):
    """Who an entrant is: from `fewest` to `most` people, one and one by
    default, so everyone enters alone; and when its roster `locks`, at the
    contest's start by default.
    """

    most: Count = 1
    fewest: Count = 1
    locks: Locks = Locks.START

    @model_validator(mode="after")
    def _check(self) -> Entrants:
        if self.fewest > self.most:
            fail([(("fewest",), f"Must be at most {self.most}, the most people in an entrant.")])
        return self


class Penalty(Model):
    """`penalty` with its one setting: minutes charged for each earlier
    attempt that does not count.
    """

    by: Literal["penalty"]
    per_attempt: Annotated[int, Field(strict=True, ge=0)] = 0


def _order_key(value: object) -> str | Penalty:
    if isinstance(value, dict):
        return Penalty.model_validate(value)
    if not isinstance(value, str) or not VALUE_NAME.match(value):
        raise ValueError(
            "Must be points, penalty, a value name the tasks report, or "
            "{by: penalty, per_attempt: <minutes>}."
        )
    return value


OrderKey = Annotated[str | Penalty, PlainValidator(_order_key)]


def _rows(value: object) -> Literal["all", "own"] | int:
    if value in ("all", "own"):
        return "all" if value == "all" else "own"
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise ValueError("Must be all, own, or a whole number of at least 1, the top rows shown.")


def key_name(key: str | Penalty) -> str:
    """The name an `order` key ranks on: `points`, `penalty` or a value's."""
    return key if isinstance(key, str) else "penalty"


class Leaderboard(Model):
    """One board: the tasks it covers, every task when left out; the groups
    it counts; which submission counts per row and task; the keys rows are
    ranked on in turn; who sees it; and which rows they see.
    """

    name: Line
    tasks: tuple[TaskHandle, ...] | None = None
    over: Over = Over.ALL
    select: Select = Select.BEST
    order: tuple[OrderKey, ...] = Field(default=("points",), min_length=1)
    who: Who = Who.ORGANISERS
    rows: Annotated[Literal["all", "own"] | int, PlainValidator(_rows)] = "all"

    @model_validator(mode="after")
    def _check(self) -> Leaderboard:
        problems = Problems()
        names = [key_name(key) for key in self.order]
        for index, name in enumerate(names):
            if name in names[:index]:
                problems.add(("order", index), f"{name} is ranked on twice.")
        if names[0] == "penalty":
            problems.add(
                ("order", 0),
                "penalty is never the first key: it breaks ties among rows equal on the "
                "keys before it.",
            )
        if self.select is Select.BEST_PER_GROUP:
            if names[0] != "points":
                problems.add(("select",), "best_per_group ranks points first.")
            values = [name for name in names if name not in ("points", "penalty")]
            if values:
                problems.add(
                    ("select",),
                    f"best_per_group sums groups across submissions, so it cannot rank "
                    f"{values[0]}, which belongs to one submission.",
                )
        if self.rows == "own" and self.who is Who.EVERYONE:
            problems.add(("rows",), "own is not for everyone: a guest has no row of their own.")
        problems.raise_any()
        return self

    def covers(self, task: str) -> bool:
        return self.tasks is None or task in self.tasks


class ContestTask(Model):
    """A task's whole timeline in the contest: what it is worth, when it is
    released, falls due and closes, how much a late day takes off, and how
    many of its submissions a row may mark. Every time but `due` defaults to
    the contest's own.
    """

    id: TaskHandle
    worth: Annotated[Number, Field(ge=0)] | None = None
    release_at: AwareTime | None = None
    due: AwareTime | None = None
    late_per_day: Number | None = None
    closes: AwareTime | None = None
    marks: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None


DEFAULT_LATE_PER_DAY = 1
"""The fraction a started late day takes off a task whose entry gives a
`due` and no `late_per_day`: all of it, so a late submission earns nothing."""


class ContestDefinition(Model):
    """A `contest.yaml`. `registration` defaults to open, anyone signing up
    and an organiser approving each, `entrants` to one person each,
    `on_system_error` to still grading, and `leaderboards` and `tasks` to
    none.
    """

    name: Line
    description: str | None = None
    start: AwareTime
    end: AwareTime
    state: State
    visibility: ContestVisibility
    registration: Registration = Registration()
    entrants: Entrants = Entrants()
    on_system_error: OnSystemError = OnSystemError.GRADING
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
        problems.duplicates([board.name for board in self.leaderboards], ("leaderboards",), "name")
        known = {task.id for task in self.tasks}
        for index, board in enumerate(self.leaderboards):
            for position, task in enumerate(board.tasks or ()):
                if task not in known:
                    problems.add(
                        ("leaderboards", index, "tasks", position),
                        f"{task!r} is not in the contest's tasks.",
                    )
        for index, entry in enumerate(self.tasks):
            for path, message in self._entry_problems(entry):
                problems.add(("tasks", index, *path), message)
        problems.raise_any()
        return self

    def _entry_problems(self, entry: ContestTask) -> list[tuple[tuple[str, ...], str]]:
        """C1 as far as the file alone says: the four times in order, a
        fraction a late day takes, and marks only under a marked board.
        """
        found: list[tuple[tuple[str, ...], str]] = []
        # An absent release_at or closes is read at its default and named as
        # the contest's start or end, so a problem is never at a key the
        # entry does not write.
        times = [
            ("start", self.start),
            ("release_at" if entry.release_at is not None else "start", self.release_of(entry)),
            ("due", entry.due),
            ("closes" if entry.closes is not None else "end", self.closes_of(entry)),
            ("end", self.end),
        ]
        chain = [(key, at) for key, at in times if at is not None]
        for (before, earlier), (key, later) in pairwise(chain):
            if later >= earlier:
                continue
            if key == "end":
                found.append(((before,), f"{before} is after the contest's end."))
            elif before == "start":
                found.append(((key,), f"{key} is before the contest's start."))
            else:
                found.append(
                    (
                        (key,),
                        f"{key} is before {before}: a task's times run start <= "
                        "release_at <= due <= closes <= end.",
                    )
                )
        if entry.late_per_day is not None:
            if entry.due is None:
                found.append((("late_per_day",), "Applies only with a due."))
            if not 0 < entry.late_per_day <= 1:
                found.append(
                    (
                        ("late_per_day",),
                        "Must be more than 0 and at most 1, a fraction taken per day.",
                    )
                )
        if entry.marks is not None and not any(
            board.select is Select.MARKED and board.covers(entry.id) for board in self.leaderboards
        ):
            found.append(
                (("marks",), "Applies only when a board with select: marked covers the task.")
            )
        return found

    def entry(self, task: str) -> ContestTask | None:
        """The task's entry in `tasks`, or none when the contest lists it not."""
        return next((entry for entry in self.tasks if entry.id == task), None)

    def release_of(self, entry: ContestTask) -> datetime:
        return entry.release_at if entry.release_at is not None else self.start

    def closes_of(self, entry: ContestTask) -> datetime:
        return entry.closes if entry.closes is not None else self.end

    def late_per_day_of(self, entry: ContestTask) -> int | Decimal | None:
        """The fraction a started late day takes off: the entry's, or
        `DEFAULT_LATE_PER_DAY` when it gives a `due` and none, and none on a
        task with no due, which has no late submissions.
        """
        if entry.due is None:
            return None
        return entry.late_per_day if entry.late_per_day is not None else DEFAULT_LATE_PER_DAY

    def label_of(self, task: str) -> str | None:
        """The task's label, its position as a letter, A, B, ..., Z, AA, ...;
        none when the contest does not list it.
        """
        for index, entry in enumerate(self.tasks):
            if entry.id == task:
                return letters(index)
        return None

    def marks_of(self, entry: ContestTask) -> int | None:
        """How many submissions a row may mark: the entry's, or 1 when a
        marked board covers the task, or none.
        """
        if entry.marks is not None:
            return entry.marks
        marked = any(
            board.select is Select.MARKED and board.covers(entry.id) for board in self.leaderboards
        )
        return 1 if marked else None


def letters(index: int) -> str:
    """The label of the entry at `index`: A, B, ..., Z, AA, AB, ..."""
    label = ""
    count = index + 1
    while count:
        count, rest = divmod(count - 1, 26)
        label = chr(ord("A") + rest) + label
    return label


class Rate(Model):
    """At most `count` submissions in any window of `per` seconds."""

    count: Count
    per: Seconds

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.per)


DEFAULT_SUBMISSIONS = 50
DEFAULT_RATE = Rate(count=1, per=30)


class Submissions(Model):
    """What a submit is counted against: at most `max` submissions, 50 by
    default, and at most one every 30 seconds unless `rate` says otherwise.
    """

    max: Count = DEFAULT_SUBMISSIONS
    rate: Rate = DEFAULT_RATE


DEFAULT_MAX_SIZE = parse_size("10MB")
FILE_CEILING = parse_size("2GB")
"""The most any one file input may total, whatever a task allows. It is the
forge's own limit on an object in its large-file store (Forgejo: `[server]
LFS_MAX_FILE_SIZE`), which refuses a larger one itself, so a task allowing
more would only promise what the forge then takes back."""
SUBMISSION_CEILING = parse_size("2GB")
"""The most any submission may be, whatever a task allows. No submission
passes through the platform's memory any more, so what this bounds is the
disk one person's submissions take in the store every contest shares; it is
also what the open-upload allowance is counted against (`domain.uploads`).
Raising it costs disk and nothing else."""

NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class Group(Model):
    """One test group: its rule weights, the share of its tests `pass` asks
    for, its tests' weights, and when it is shown. A rule weight left out is
    0; `show` left out is `always`, except on a group added once the task
    has a graded submission, which must say (T10).
    """

    each: Annotated[Number, Field(ge=0)] | None = None
    worst: Annotated[Number, Field(ge=0)] | None = None
    pass_: Annotated[Number, Field(ge=0)] | None = Field(default=None, alias="pass")
    pass_at: Number | None = None
    test_weights: dict[str, Number] | None = None
    show: Show | None = None

    @property
    def weight(self) -> int | Decimal:
        """What the group can earn, `R_g`: its rule weights summed."""
        total: int | Decimal = 0
        for value in (self.each, self.worst, self.pass_):
            if value is not None:
                total += value
        return total

    @property
    def shown(self) -> Show:
        return self.show if self.show is not None else Show.ALWAYS


def _value_name(value: object) -> str:
    if not isinstance(value, str) or not VALUE_NAME.match(value):
        raise ValueError("Must be the name of a value the workflow reports.")
    return value


class Relative(Model):
    """`{relative: <value>}`: a test earns its value against the best any
    contestant reached on it.
    """

    relative: Annotated[str, PlainValidator(_value_name)]


def _credit(value: object) -> str | Relative:
    if isinstance(value, dict):
        return Relative.model_validate(value)
    return _value_name(value)


class Form(Model):
    """The form details of an input the contestant gives, all optional: its
    label; a default for text, a number, true or false and an enum; the
    least and the most a number may be; the options an enum offers; and the
    most a file or folder input's files may total.
    """

    label: Line | None = None
    default: Any = None
    min: Number | None = None
    max: Number | None = None
    options: Options | None = None
    max_size: Size | None = None


class TaskDefinition(Model):
    """A `task.yaml`. `inputs` defaults to none, `credit` to an accepted test
    earning 1, and `submissions` to 50 at one every 30 seconds.
    """

    name: Line
    workflow: Ref
    inputs: dict[str, Any] = Field(default_factory=dict)
    credit: Annotated[str | Relative, PlainValidator(_credit)] | None = None
    test_groups: dict[str, Group] = Field(min_length=1)
    submissions: Submissions = Submissions()

    @model_validator(mode="after")
    def _check(self) -> TaskDefinition:
        problems = Problems()
        for name, group in self.test_groups.items():
            for path, message in self._group_problems(name, group):
                problems.add(("test_groups", name, *path), message)
        if self.credit is not None and not self.gives_points:
            problems.add(("credit",), "No group has a rule weight, so credit scores nothing.")
        problems.raise_any()
        return self

    def _group_problems(self, name: str, group: Group) -> list[tuple[tuple[str, ...], str]]:
        """T1, T2 as far as the file says, and T6."""
        found: list[tuple[tuple[str, ...], str]] = []
        if not NAME.match(name):
            found.append(((), "A group's name is letters, digits, _ and -."))
        if group.pass_at is not None:
            if group.pass_ is None:
                found.append((("pass_at",), "Applies only beside pass."))
            if not 0 < group.pass_at <= 1:
                found.append((("pass_at",), "Must be more than 0 and at most 1."))
        if group.worst is not None and self.credit is None:
            found.append((("worst",), "With pass/fail tests `worst` is `pass`: write `pass`."))
        if group.test_weights is not None:
            for test, weight in group.test_weights.items():
                if not NAME.match(test):
                    found.append(
                        (("test_weights", test), "A test's name is letters, digits, _ and -.")
                    )
                elif weight <= 0:
                    found.append(
                        (
                            ("test_weights", test),
                            f"{name}/{test} weighs {weight}: put a test that counts for nothing "
                            "in a group with no rule weight.",
                        )
                    )
            reads = group.each is not None or (
                group.pass_ is not None and group.pass_at is not None and group.pass_at < 1
            )
            if not reads:
                found.append(
                    (
                        ("test_weights",),
                        f"test_weights on {name} change nothing: only each, or pass with a "
                        "pass_at below 1, reads them.",
                    )
                )
        if group.show is Show.VERDICT:
            problem = self._verdict_problem(name, group)
            if problem is not None:
                found.append((("show",), problem))
        return found

    def _verdict_problem(self, name: str, group: Group) -> str | None:
        """T6: a verdict shows only all-or-nothing points."""
        if group.each is not None:
            return (
                f"{name} is shown as a verdict, but its each points would tell how many of "
                "its tests passed; use pass, or show it always or after_close."
            )
        if group.worst is not None:
            return (
                f"{name} is shown as a verdict, but its worst points would tell its worst "
                "test's credit; use pass, or show it always or after_close."
            )
        if isinstance(self.credit, Relative):
            return (
                "A verdict group's outcome and points would move when other rows improve on "
                "its hidden tests; show it always or after_close."
            )
        return None

    @property
    def gives_points(self) -> bool:
        """Whether some group carries a rule weight, `W > 0`."""
        return any(group.weight > 0 for group in self.test_groups.values())

    @property
    def total_weight(self) -> int | Decimal:
        """`W`, every group's rule weight summed: what the task is worth in a
        contest whose entry gives no `worth`, so a group's numbers are its
        points unless the contest says otherwise.
        """
        total: int | Decimal = 0
        for group in self.test_groups.values():
            total += group.weight
        return total


def worth_of(entry: ContestTask, definition: TaskDefinition) -> int | Decimal | None:
    """The most points a task gives in its contest: the entry's `worth`, or
    the task's groups' total when the entry gives none, and none on a task
    that gives no points.
    """
    if not definition.gives_points:
        return None
    return entry.worth if entry.worth is not None else definition.total_weight


def _is_public(value: object) -> bool:
    return value == "public"


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _old_select(value: object) -> bool:
    return value in ("latest", "selected", "first_accepted")


def _old_order(value: object) -> bool:
    return isinstance(value, dict) and bool({"metric", "key", "direction"} & set(value))


WAYS_REPLACED = (
    "invite_only, approval, email_pattern and code are now ways: write registration.ways."
)

CONTEST_RETIRED = (
    Retired(
        ("submissions_closed",),
        "A task takes submissions until the closes on its entry in tasks; give each task "
        "closes, or move end. Remove submissions_closed.",
    ),
    Retired(("teams",), "Write entrants: {most: <n>} for entrants of up to n people."),
    Retired(("team_size",), "team_size is now entrants.most."),
    *(
        Retired(("registration", key), WAYS_REPLACED)
        for key in ("invite_only", "approval", "email_pattern", "code", "mode", "eligibility")
    ),
    Retired(("visibility",), "public is now everyone.", _is_public),
    Retired(
        ("tasks", ANY, "label"),
        "A task's label is its place in tasks, A, B, ...; remove label.",
    ),
    Retired(("tasks", ANY, "points"), "points is now worth."),
    Retired(("leaderboards", ANY, "stage"), "A board counts test groups; over says which."),
    Retired(("leaderboards", ANY, "combine"), "A board sums its tasks; remove combine."),
    Retired(
        ("leaderboards", ANY, "visibility"),
        "visibility is now who: organisers, contestants or everyone.",
    ),
    Retired(("leaderboards", ANY, "freeze_at"), "A board does not freeze; remove freeze_at."),
    Retired(
        ("leaderboards", ANY, "team_only"),
        "A board's rows are the contest's entrants; remove team_only.",
    ),
    Retired(
        ("leaderboards", ANY, "select"),
        "select is best, best_per_group or marked.",
        _old_select,
    ),
    Retired(
        ("leaderboards", ANY, "order", ANY),
        "An order key is points, a value name, penalty or {by: penalty, per_attempt: "
        "<minutes>}; a value's direction is its workflow's.",
        _old_order,
    ),
)

TASK_RETIRED = (
    Retired(
        ("release_at",),
        "A task's times are on its entry in contest.yaml's tasks; write release_at there.",
    ),
    Retired(
        ("hidden",),
        "A task shows from the release_at on its entry in contest.yaml; to hide results, "
        "give a test group show: after_close.",
    ),
    Retired(
        ("limits",),
        "limits is now submissions: {max, rate: {count, per}}; each file input sets its own "
        "max_size.",
    ),
    Retired(
        ("subtasks",),
        "Subtasks are test groups: folders under tests/ listed in test_groups.",
    ),
    Retired(
        ("stages",),
        "A task has one plan: hidden results are a test group's show, and which submission "
        "counts is a board's select.",
    ),
    Retired(
        ("inputs", "contestant"),
        "inputs is one mapping keyed by the workflow's input ids: form details for the "
        "contestant's inputs, values for the rest.",
    ),
    Retired(
        ("inputs", "setter"),
        "inputs is one mapping keyed by the workflow's input ids: form details for the "
        "contestant's inputs, values for the rest.",
    ),
    Retired(
        ("submissions", "rate"),
        "rate is {count, per}, such as {count: 1, per: 30}.",
        _is_text,
    ),
)


def parse_contest(text: bytes | str) -> ContestDefinition:
    """The `contest.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(
        ContestDefinition, CONTEST_FILE, load_mapping(CONTEST_FILE, text), CONTEST_RETIRED
    )


def parse_task(text: bytes | str) -> TaskDefinition:
    """The `task.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(TaskDefinition, TASK_FILE, load_mapping(TASK_FILE, text), TASK_RETIRED)


CONTEST_ADMIN_KEYS = ("name", "description", "state", "visibility", "registration")
TASK_ADMIN_KEYS = ("name", "submissions")
ADMIN_ONLY_FILES = (STATEMENT_FILE,)

_MISSING = object()


def admin_only_changes(
    kind: Literal["contest", "task"], before: bytes | None, after: bytes | None
) -> list[str]:
    """The admin-only top-level keys whose values differ between `before`
    and `after`, in the order the keys are listed. `before` is none for a
    new file, and `after` for a file removed. A key added or removed counts
    as changed. A text that does not
    parse compares as an empty mapping, because reporting it is
    validation's work.
    """
    keys = CONTEST_ADMIN_KEYS if kind == "contest" else TASK_ADMIN_KEYS
    old, new = mapping_or_empty(before), mapping_or_empty(after)
    return [
        key
        for key in keys
        if _compared(old.get(key, _MISSING)) != _compared(new.get(key, _MISSING))
    ]


def _compared(value: object) -> object:
    """`value` with every text that is a date and time with a timezone read
    as that moment, so one time written two ways is the same value.
    """
    if isinstance(value, dict):
        return {key: _compared(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compared(item) for item in value]
    if isinstance(value, str):
        moment = read_time(value)
        if isinstance(moment, datetime) and moment.tzinfo is not None:
            return moment
    return value


def title_of(title: str | None, name: str) -> str:
    """The title a new contest or task is created with: the one asked for,
    or its name when none is given or the one given is blank, so the starter
    file is valid as written.
    """
    return title if title and title.strip() else name


def _yaml_text(value: str) -> str:
    """`value` as a YAML scalar in double quotes; a JSON string is one."""
    return json.dumps(" ".join(value.split()), ensure_ascii=False)


def stamp(moment: datetime) -> str:
    """A moment as `contest.yaml` writes one, in UTC to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def starter_contest(name: str, now: datetime) -> bytes:
    """The `contest.yaml` a new contest is created with: titled `name`, a
    draft seen by anyone signed in, open to register with manual approval,
    starting on the first full hour at least seven days after `now` and
    running five hours, with one board shown to contestants and no tasks.
    """
    week_on = now.astimezone(UTC) + timedelta(days=7)
    start = week_on.replace(minute=0, second=0, microsecond=0)
    if start < week_on:
        start += timedelta(hours=1)
    end = start + timedelta(hours=5)
    return f"""\
# The contest's settings. The format is TASK-FORMAT.md, section 1.1.
name: {_yaml_text(name)}
start: {stamp(start)}
end: {stamp(end)}
state: draft
visibility: signed-in
leaderboards:
  - name: Standings
    who: contestants
""".encode()


def starter_task(name: str) -> dict[str, bytes]:
    """The files a new task is created with: a `task.yaml` titled `name` that
    grades Python with `unicon/classic@v2` on one group of tests, a
    placeholder statement, an empty `public/`, and one test, `main/1`, whose
    input is `1 2` and whose answer is `3`, so the first save publishes.
    """
    task = f"""\
# The task's settings. The format is TASK-FORMAT.md, section 1.2.
name: {_yaml_text(name)}
workflow: unicon/classic@v2
inputs:
  submission: {{label: Your solution}}
  language: {{options: [python]}}
  time_limit: 2
  memory_limit: 256
test_groups:
  main: {{each: 100}}
"""
    return {
        TASK_FILE: task.encode(),
        STATEMENT_FILE: b"Write the statement contestants read here.\n",
        f"{PUBLIC_FOLDER}.gitkeep": b"",
        f"{TESTS_FOLDER}main/1/input": b"1 2\n",
        f"{TESTS_FOLDER}main/1/answer": b"3\n",
    }


def form_problems(
    form: Mapping[str, Any], kind: str, workflow_options: tuple[str, ...] | None
) -> list[tuple[tuple[str, ...], str]]:
    """What is wrong with a contestant input's form details, given the
    input's type and, for an enum, the workflow's options; each with its
    path below the input.
    """
    try:
        details = Form.model_validate(form)
    except ValidationError as error:
        return [
            (tuple(problem["path"].split(".")) if problem["path"] else (), problem["message"])
            for problem in problems_of(error)
        ]
    return details_problems(details, kind, workflow_options)


def details_problems(
    details: Form, kind: str, workflow_options: tuple[str, ...] | None
) -> list[tuple[tuple[str, ...], str]]:
    found: list[tuple[tuple[str, ...], str]] = []
    allowed = {
        "text": {"label", "default"},
        "number": {"label", "default", "min", "max"},
        "boolean": {"label", "default"},
        "enum": {"label", "default", "options"},
        "file": {"label", "max_size"},
        "folder": {"label", "max_size"},
    }[kind]
    for key in sorted(details.model_fields_set - allowed):
        found.append(((key,), f"A {kind} input does not take {key}."))
    if details.min is not None and details.max is not None and details.max < details.min:
        found.append((("max",), "Must be at least min."))
    if details.max_size is not None and details.max_size > FILE_CEILING:
        found.append((("max_size",), "Must be at most 2GB, the largest the platform takes."))
    if details.options is not None and workflow_options is not None:
        extra = [option for option in details.options if option not in workflow_options]
        if extra:
            found.append(
                (
                    ("options",),
                    f"{extra[0]} is not an option of the workflow's: "
                    f"{', '.join(workflow_options)}.",
                )
            )
    if details.default is not None and "default" in allowed:
        problem = _default_problem(details, kind, workflow_options)
        if problem is not None:
            found.append((("default",), problem))
    return found


def _default_problem(
    details: Form, kind: str, workflow_options: tuple[str, ...] | None
) -> str | None:
    value = details.default
    match kind:
        case "text":
            return None if isinstance(value, str) else "Must be text."
        case "boolean":
            return None if isinstance(value, bool) else "Must be true or false."
        case "number":
            if not is_number(value):
                return "Must be a number."
            if details.min is not None and value < details.min:
                return "Must be at least min."
            if details.max is not None and value > details.max:
                return "Must be at most max."
            return None
        case "enum":
            offered = details.options or workflow_options or ()
            return None if value in offered else f"Must be one of {', '.join(offered)}."
    return None
