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
T3 that need no workflow), kept until today's format goes (stage 4 open
132), though since stage 5 its rule weights and `credit` score nothing
(stage 5 section 6, item 123). What needs another file, the workflow a task
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
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import MAX_PREC, Decimal, localcontext
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
    ANY_KEY,
    AwareTime,
    InvalidDefinition,
    Line,
    Model,
    Number,
    Problem,
    Problems,
    Retired,
    Size,
    already_listed,
    aware_time,
    fail,
    is_number,
    load_mapping,
    mapping_or_empty,
    name_rule,
    parse_size,
    problems_of,
    read_time,
    validate,
)

__all__ = [
    "ADMIN_ONLY_FILES",
    "CONTEST_ADMIN_KEYS",
    "CONTEST_FILE",
    "ENTRANT",
    "RUN_FORMAT_KEYS",
    "STATEMENT_FILE",
    "TASK_ADMIN_KEYS",
    "TASK_FILE",
    "Approval",
    "Axis",
    "BoardAudience",
    "Check",
    "ContestDefinition",
    "ContestTask",
    "ContestVisibility",
    "Entrants",
    "Form",
    "Group",
    "Hold",
    "InvalidDefinition",
    "InviteWay",
    "Leaderboard",
    "Locks",
    "Rate",
    "Registration",
    "Relative",
    "Scoring",
    "Show",
    "ShowTimes",
    "SignupWay",
    "State",
    "Submissions",
    "TaskDefinition",
    "Way",
    "admin_only_changes",
    "admin_only_refusal",
    "parse_contest",
    "parse_task",
    "starter_contest",
    "starter_task",
    "title_of",
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


class Show(StrEnum):
    """When contestants see a test group's results: as soon as a grading
    ends; its outcome now and its tests at the task's reveal; or, but for
    its name, everything at the reveal.
    """

    ALWAYS = "always"
    VERDICT = "verdict"
    AFTER_CLOSE = "after_close"


def _pattern(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise ValueError("Give a pattern on one line, written as a regular expression.")
    try:
        regex.compile(value)
    except regex.error as error:
        raise ValueError(f"This is not a regular expression Unicon can read: {error}.") from None
    return value


def _task_handle(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("This must be text.")
    try:
        return validate_contest_or_task_name(value)
    except InvalidName:
        raise ValueError(name_rule(value, 24)) from None


TaskHandle = Annotated[str, PlainValidator(_task_handle)]
Count = Annotated[int, Field(strict=True, ge=1)]
Seconds = Annotated[int, Field(strict=True, ge=1)]
VALUE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
"""A domain a sign-up takes addresses at: labels of lower-case letters,
digits and hyphens, two or more, joined by dots, no label starting or ending
with a hyphen."""
UNDER = "*."
"""What a sign-up's domain starts with to take the domain after it and every
domain under it: `*.nus.edu.sg` takes `nus.edu.sg` and `comp.nus.edu.sg`
(#439). It stands only as the whole first label; nothing else is wild."""


def _domain(value: object) -> str:
    if not isinstance(value, str) or not DOMAIN.fullmatch(value.removeprefix(UNDER)):
        raise ValueError(
            "Give a domain such as u.nus.edu, or *.nus.edu.sg to take nus.edu.sg and every "
            "domain under it: lower case letters, digits, hyphens and dots, with at least one "
            "dot, no dot or hyphen at either end, and * only as the whole first part."
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
                problems.add(("domains", index), already_listed(domain))
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
            fail([((), "A way in is either an invite or a sign-up. Choose one of the two.")])
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
            problems.add(
                ("ways", index, "invite"),
                "A contest has only one way in by invite. Remove this one.",
            )
        seen: set[str] = set()
        for index, way in enumerate(self.ways):
            if way.name is None:
                continue
            if way.name in seen:
                problems.add(
                    ("ways", index, "signup", "name"),
                    f"Another sign-up is already called {way.name!r}. Give this one another name.",
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
            fail(
                [
                    (
                        ("fewest",),
                        f"This must be at most {self.most}, the most people in a team.",
                    )
                ]
            )
        return self


BOARD_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
"""A board's column's name: lower case letters, digits, hyphens and
underscores, starting with a letter, as a formula reads it once each `-`
is written `_`."""
COLUMN_LONGEST = 40


def _rows(value: object) -> Literal["all", "own"] | int:
    if value in ("all", "own"):
        return "all" if value == "all" else "own"
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise ValueError(
        "Choose every row, only each entrant's own row, or a number of top rows of at least 1."
    )


class BoardAudience(Model):
    """Who reads a board (SIMPLER-CORE.md 10.2 rule 9): `who`, staff,
    entrants or everyone, and which `rows` they are given: every row, each
    entrant's own, or a number of top rows, those tied with the last and
    the viewer's own added.
    """

    who: Literal["staff", "entrants", "everyone"] = "staff"
    rows: Annotated[Literal["all", "own"] | int, PlainValidator(_rows)] = "all"

    @model_validator(mode="after")
    def _check(self) -> BoardAudience:
        if self.rows == "own" and self.who == "everyone":
            fail(
                [
                    (
                        ("rows",),
                        "A leaderboard shown to everyone cannot show each entrant only their "
                        "own row, since a guest has none. Show every row or the top rows.",
                    )
                ]
            )
        return self


STAFF_ONLY = BoardAudience()
"""A board with no `audience`: staff's alone (#512)."""


def _column_name(value: object) -> str:
    if not isinstance(value, str) or not BOARD_NAME.match(value) or len(value) > COLUMN_LONGEST:
        raise ValueError(
            f"{value!r} cannot be a column's name. Use at most {COLUMN_LONGEST} lower case "
            "letters, digits, hyphens (-) and underscores (_), starting with a letter."
        )
    return value


ColumnName = Annotated[str, PlainValidator(_column_name)]


def _sort_key(value: object) -> tuple[str, Literal["desc", "asc"]]:
    if isinstance(value, dict) and len(value) == 1:
        [(name, way)] = value.items()
        if way in ("desc", "asc"):
            return _column_name(name), way
    raise ValueError(
        "Give one column and which way it sorts, such as {total: desc}: desc puts the "
        "highest first, asc the lowest."
    )


def _shown(value: object) -> tuple[str, int | None]:
    if isinstance(value, str):
        return _column_name(value), None
    if isinstance(value, dict) and len(value) == 1:
        [(name, how)] = value.items()
        places = how.get("places") if isinstance(how, dict) and set(how) == {"places"} else None
        if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 10:
            return _column_name(name), places
    raise ValueError(
        "Give a column's name, or its name with how many decimal places it is shown, such "
        "as {total: {places: 2}}, from 0 to 10."
    )


class Leaderboard(Model):
    """One board (SIMPLER-CORE.md 10, the leaderboards note's 5.10): its
    `title`, the key when left out; who reads it, staff alone when left
    out; its `columns`, each a tree of blocks, checked against the
    catalogue and the tasks they read at the contest's save and at a
    task's (`forge.domain.board_checks`); the
    columns it `sort`s by, in turn, each `desc` or `asc`; and those it
    `display`s, in order, each with how many decimal `places` it is shown
    to, a column's number itself kept exact.
    """

    title: Line | None = None
    audience: BoardAudience = STAFF_ONLY
    columns: dict[Annotated[str, PlainValidator(_column_name)], Any] = Field(default_factory=dict)
    sort: tuple[Annotated[tuple[str, str], PlainValidator(_sort_key)], ...] = ()
    display: tuple[Annotated[tuple[str, int | None], PlainValidator(_shown)], ...] = ()

    def title_or(self, key: str) -> str:
        return self.title if self.title is not None else key

    @property
    def shown(self) -> tuple[str, ...]:
        """The columns displayed, in order."""
        return tuple(name for name, _ in self.display)

    def places(self, column: str) -> int | None:
        return next((places for name, places in self.display if name == column), None)


def _hold_end(value: object) -> datetime | Literal["staff"]:
    if value == "staff":
        return "staff"
    try:
        return aware_time(value)
    except ValueError as wrong:
        raise ValueError(f"{wrong} Or give staff, for a hold staff end themselves.") from None


class Hold(Model):
    """The contest's hold, the freeze (SIMPLER-CORE.md 10.2 rule 10): from
    `from`, a time, until `until`, a time or `staff`, who end it. That
    `from` lies inside the contest, and is not set to a time passed, is the
    save's (`forge.domain.board_checks.hold_problems`)."""

    from_: AwareTime = Field(alias="from")
    until: Annotated[datetime | Literal["staff"], PlainValidator(_hold_end)]

    @model_validator(mode="after")
    def _check(self) -> Hold:
        if not isinstance(self.until, str) and self.until <= self.from_:
            fail([(("until",), "The hold must end after it starts.")])
        return self


class ContestTask(Model):
    """A task's whole timeline in the contest: when it is released, falls
    due and closes, and how many of its submissions a row may mark. Every
    time but `due` defaults to the contest's own. A `withdrawn` task stays in the list, keeping its
    place and so its letter, but entrants no longer see it or submit to it
    and no leaderboard counts it; its submissions stay for the organisers,
    and taking the key out puts it back as it was (#450).
    """

    id: TaskHandle
    release_at: AwareTime | None = None
    due: AwareTime | None = None
    closes: AwareTime | None = None
    marks: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    withdrawn: Annotated[bool, Field(strict=True)] = False


_TIMES = {
    "start": "The contest's start",
    "release_at": "The release time",
    "due": "The due time",
    "closes": "The closing time",
    "end": "The contest's end",
}
"""A task's times as a person names them, each starting a sentence."""


class ContestDefinition(Model):
    """A `contest.yaml`. `registration` defaults to open, anyone signing up
    and an organiser approving each, `entrants` to one person each,
    `leaderboards` and `tasks` to none, and there is no `hold`. Boards are
    keyed by a name tasks and boards share, so a column reads `a.b`
    knowing which `a` is.
    """

    name: Line
    description: str | None = None
    start: AwareTime
    end: AwareTime
    state: State
    visibility: ContestVisibility
    registration: Registration = Registration()
    entrants: Entrants = Entrants()
    leaderboards: dict[TaskHandle, Leaderboard] = Field(default_factory=dict)
    hold: Hold | None = None
    tasks: tuple[ContestTask, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> ContestDefinition:
        problems = Problems()
        if self.end <= self.start:
            problems.add(("end",), "The end must be after the start.")
        opens, closes = self.registration.opens, self.registration.closes
        if opens is not None and closes is not None and closes <= opens:
            problems.add(("registration", "closes"), "Registration must close after it opens.")
        problems.duplicates([task.id for task in self.tasks], ("tasks",))
        known = {task.id for task in self.tasks}
        for key in self.leaderboards:
            if key in known:
                problems.add(
                    ("leaderboards", key),
                    f"{key} is a task of the contest, and tasks and leaderboards share one set "
                    "of names, so a column can read either. Name the leaderboard otherwise.",
                )
        for index, entry in enumerate(self.tasks):
            for path, message in self._entry_problems(entry):
                problems.add(("tasks", index, *path), message)
        problems.raise_any()
        return self

    def _entry_problems(self, entry: ContestTask) -> list[tuple[tuple[str, ...], str]]:
        """C1 as far as the file alone says: the four times in order. Marks
        only under a board reading the task's `marked` is the board check's
        (`board_checks`).
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
                found.append(
                    ((before,), f"{_TIMES[before]} is after the contest's end. Make it earlier.")
                )
            elif before == "start":
                found.append(
                    ((key,), f"{_TIMES[key]} is before the contest's start. Make it later.")
                )
            else:
                found.append(
                    (
                        (key,),
                        f"{_TIMES[key]} is before {_TIMES[before].lower()}. A task is released, "
                        "falls due and closes in that order, within the contest.",
                    )
                )
        return found

    def entry(self, task: str) -> ContestTask | None:
        """The task's entry in `tasks`, or none when the contest lists it not."""
        return next((entry for entry in self.tasks if entry.id == task), None)

    def release_of(self, entry: ContestTask) -> datetime:
        return entry.release_at if entry.release_at is not None else self.start

    def closes_of(self, entry: ContestTask) -> datetime:
        return entry.closes if entry.closes is not None else self.end

    def label_of(self, task: str) -> str | None:
        """The task's label, its position as a letter, A, B, ..., Z, AA, ...;
        none when the contest does not list it.
        """
        for index, entry in enumerate(self.tasks):
            if entry.id == task:
                return letters(index)
        return None


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
        """What the group can earn, `R_g`: its rule weights summed, to the
        digit."""
        return exact_sum(
            value for value in (self.each, self.worst, self.pass_) if value is not None
        )

    @property
    def shown(self) -> Show:
        return self.show if self.show is not None else Show.ALWAYS


def _value_name(value: object) -> str:
    if not isinstance(value, str) or not VALUE_NAME.match(value):
        raise ValueError("Give the name of a value the workflow reports.")
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


ENTRANT = "entrant"
"""What makes an input the entrant's in a run task's `inputs`, alone or as
the key of its form details, and what stands for the entrant's value in a
tuple of `repeat`."""


def _row_value_problem(value: object) -> str | None:
    if isinstance(value, dict) and "secret" in value:
        return "A secret is given once, in inputs, never in a tuple, so a run's key never shows it."
    if value is None or isinstance(value, list | dict):
        return (
            "Give text, a number, true or false, a file or folder in the task, or entrant, "
            "not a list, a set of settings or nothing."
        )
    return None


class Axis(Model):
    """One axis of a run task's `repeat` (SIMPLER-CORE.md 8.2): the workflow
    inputs that move together, and one row per tuple, written as a list in
    the order of `inputs`, as the value alone when the axis repeats one
    input, or as a mapping of each input to its value, so no two get
    swapped. A value may be `entrant`: the entrant gives that input once for
    the tuple.
    """

    inputs: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    tuples: list[Any] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Axis:
        problems = Problems()
        seen: set[str] = set()
        for index, name in enumerate(self.inputs):
            if name in seen:
                problems.add(("inputs", index), already_listed(name))
            seen.add(name)
        for index, row in enumerate(self.tuples):
            for path, message in self._row_problems(row):
                problems.add(("tuples", index, *path), message)
        problems.raise_any()
        return self

    def _row_problems(self, row: object) -> list[tuple[tuple[str | int, ...], str]]:
        found: list[tuple[tuple[str | int, ...], str]] = []
        count = len(self.inputs)
        if isinstance(row, dict):
            for name in self.inputs:
                if name not in row:
                    found.append(((), f"This tuple gives no value for {name}."))
            for key, value in row.items():
                if key not in self.inputs:
                    found.append(
                        (
                            (str(key),),
                            f"{key} is not one of this axis's inputs, which are "
                            f"{', '.join(self.inputs)}.",
                        )
                    )
                elif (problem := _row_value_problem(value)) is not None:
                    found.append(((str(key),), problem))
            return found
        if isinstance(row, list):
            if len(row) != count:
                found.append(
                    (
                        (),
                        f"This tuple has {len(row)} {'value' if len(row) == 1 else 'values'}, "
                        f"and the axis repeats {', '.join(self.inputs)}, so give one value "
                        "for each, in that order.",
                    )
                )
                return found
            for index, value in enumerate(row):
                if (problem := _row_value_problem(value)) is not None:
                    found.append(((index,), problem))
            return found
        if count != 1:
            return [
                (
                    (),
                    f"The axis repeats {', '.join(self.inputs)}, so give each tuple as a list "
                    "in that order, or as a mapping of each input to its value.",
                )
            ]
        problem = _row_value_problem(row)
        return [((), problem)] if problem is not None else []

    @property
    def rows(self) -> list[tuple[object, ...]]:
        """Each tuple as its values in the order of `inputs`."""
        found: list[tuple[object, ...]] = []
        for row in self.tuples:
            if isinstance(row, dict):
                found.append(tuple(row[name] for name in self.inputs))
            elif isinstance(row, list):
                found.append(tuple(row))
            else:
                found.append((row,))
        return found


class ShowTimes(Model):
    """A run task's `show` (SIMPLER-CORE.md 9.4): the values, groups and
    task files entrants see as soon as a grading ends, at the reveal, and
    once staff release the task's results.
    """

    now: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    after_close: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    on_release: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)

    def lists(self) -> dict[str, list[str]]:
        """Each time's entries, earliest first."""
        return {"now": self.now, "after_close": self.after_close, "on_release": self.on_release}


class Scoring(Model):
    """A run task's `scoring` (SIMPLER-CORE.md 9): its groups of runs, each
    picked by repeated input values; its values, each a tree of blocks
    (`forge.domain.blocks`); and what entrants see when. What each reads is
    checked against the task's runs and the workflow's columns at the save
    (`forge.domain.run_task`).
    """

    groups: dict[str, dict[str, Any]] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    show: ShowTimes = ShowTimes()


class Check(Model):
    """One of a run task's `checks` (SIMPLER-CORE.md 9.6): a made-up
    submission, `submit`, giving each entrant input by its name, a task
    path for a file or folder, a list of them in tuple order for one given
    in parts, a value otherwise; and what it must get, `expect`: a
    `verdict` every run must get, a number for each value named, or both.
    What each names is checked against the task at its save
    (`forge.domain.checks`).
    """

    submit: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(min_length=1)


class TaskDefinition(Model):
    """A `task.yaml`, in one of two formats, as its workflow's: a workflow
    with a test block is graded test by test, under `test_groups` and
    `credit`; a run workflow (SIMPLER-CORE.md 8 and 9) runs once per
    combination of `repeat`'s tuples, is scored by `scoring`, which such a
    task always has (`runs`), and may list `checks`, graded at its save.
    `inputs` defaults to none, `credit` to an accepted test earning 1, and
    `submissions` to 50 at one every 30 seconds.
    """

    name: Line
    workflow: Ref
    inputs: dict[str, Any] = Field(default_factory=dict)
    credit: Annotated[str | Relative, PlainValidator(_credit)] | None = None
    test_groups: dict[str, Group] = Field(default_factory=dict, min_length=1)
    repeat: list[Axis] = Field(default_factory=list)
    scoring: Scoring | None = None
    checks: list[Check] = Field(default_factory=list)
    submissions: Submissions = Submissions()

    @property
    def runs(self) -> bool:
        """Whether the task is in the run format, its workflow a run workflow."""
        return self.scoring is not None

    @model_validator(mode="after")
    def _check(self) -> TaskDefinition:
        problems = Problems()
        for name, group in self.test_groups.items():
            for path, message in self._group_problems(name, group):
                problems.add(("test_groups", name, *path), message)
        if self.credit is not None and not self.gives_points:
            problems.add(
                ("credit",),
                "No test group gives points, so what an accepted test earns changes nothing. "
                "Give a test group points, or let an accepted test earn 1.",
            )
        problems.raise_any()
        return self

    def _group_problems(self, name: str, group: Group) -> list[tuple[tuple[str, ...], str]]:
        """T1, T2 as far as the file says, and T6."""
        found: list[tuple[tuple[str, ...], str]] = []
        if not NAME.match(name):
            found.append(
                (
                    (),
                    "A test group's name may use only letters, digits, hyphens (-) and "
                    "underscores (_).",
                )
            )
        if group.pass_at is not None:
            if group.pass_ is None:
                found.append(
                    (
                        ("pass_at",),
                        'This applies only when the group gives points under "at least a share '
                        'of its tests must pass". Give those points, or leave this empty.',
                    )
                )
            if not 0 < group.pass_at <= 1:
                found.append(
                    (
                        ("pass_at",),
                        "This must be more than 0 and at most 1, such as 0.5 for half the tests.",
                    )
                )
        if group.worst is not None and self.credit is None:
            found.append(
                (
                    ("worst",),
                    "Each test here either passes or fails, so the lowest test score is the "
                    'same as "every test must pass". Give these points there instead.',
                )
            )
        if group.test_weights is not None:
            for test, weight in group.test_weights.items():
                if not NAME.match(test):
                    found.append(
                        (
                            ("test_weights", test),
                            "A test's name may use only letters, digits, hyphens (-) and "
                            "underscores (_).",
                        )
                    )
                elif weight <= 0:
                    found.append(
                        (
                            ("test_weights", test),
                            f"{name}/{test} has a weight of {weight}, and a weight must be "
                            "more than 0. Put a test that should count for nothing in a test "
                            "group that gives no points.",
                        )
                    )
            reads = group.each is not None or (
                group.pass_ is not None and group.pass_at is not None and group.pass_at < 1
            )
            if not reads:
                found.append(
                    (
                        ("test_weights",),
                        f"The test weights on {name} change nothing. They count only "
                        "towards points for each test passed, or when the share of tests "
                        "that must pass is below 1.",
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
                f"{name} shows entrants only its outcome until the reveal, but points for "
                "each test passed would tell them how many of its tests passed. Give its "
                'points under "every test must pass" instead, or change "When entrants see '
                'results".'
            )
        if group.worst is not None:
            return (
                f"{name} shows entrants only its outcome until the reveal, but points for "
                "the lowest test score would tell them how its worst test did. Give its "
                'points under "every test must pass" instead, or change "When entrants see '
                'results".'
            )
        if isinstance(self.credit, Relative):
            return (
                "This test group shows entrants only its outcome until the reveal, but its "
                "points would change as other entrants do better on its hidden tests. Show "
                "its results to entrants at once or only at the reveal."
            )
        return None

    @property
    def gives_points(self) -> bool:
        """Whether some group carries a rule weight, `W > 0`."""
        return any(group.weight > 0 for group in self.test_groups.values())


def exact_sum(values: Iterable[int | Decimal]) -> int | Decimal:
    """The numbers summed with no rounding: a sum of finite decimals is one,
    and Decimal's usual 28 digits would round a long weight's last ones.
    """
    total: int | Decimal = 0
    with localcontext(prec=MAX_PREC):
        for value in values:
            total += value
    return total


def _is_public(value: object) -> bool:
    return value == "public"


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_list(value: object) -> bool:
    return isinstance(value, list)


_OLD_BOARDS = (
    "Leaderboards are now keyed by name, each with its columns written as blocks, such as "
    "standings: {columns: {...}, sort: [...], display: [...]}. Write each board under its "
    "name."
)
_OLD_BOARD_KEY = (
    "A leaderboard is now its columns, written as blocks, with sort, display and audience. "
    "Write which submission counts and how rows rank as columns, and remove this."
)


_NO_WORTH = (
    "A task no longer gives points: what each task counts for is said by a leaderboard's "
    "columns, such as a total that weighs each task's value. Remove this."
)


WAYS_REPLACED = (
    "This setting is no longer used: who may register, and how, is now set by the contest's "
    "ways in. Add a way in instead, and remove this."
)

CONTEST_RETIRED = (
    Retired(
        ("submissions_closed",),
        "This setting is no longer used: each task takes submissions until its own closing "
        "time. Give each task a closing time, or move the contest's end, and remove this.",
    ),
    Retired(
        ("teams",),
        "This setting is no longer used. Set the most people in a team instead, and remove this.",
    ),
    Retired(
        ("team_size",),
        "This is now the most people in a team. Set that instead, and remove this.",
    ),
    *(
        Retired(("registration", key), WAYS_REPLACED)
        for key in ("invite_only", "approval", "email_pattern", "code", "mode", "eligibility")
    ),
    Retired(("visibility",), 'This is now called "everyone". Choose that instead.', _is_public),
    Retired(
        ("on_system_error",),
        "This setting is no longer used: a grading that fails is tried again once by itself, "
        "and a submission keeps its last result until a newer one replaces it. Remove this.",
    ),
    Retired(
        ("tasks", ANY, "label"),
        "A task's label is now its place in the contest's list of tasks, A, B and so on. "
        "Remove this.",
    ),
    Retired(("tasks", ANY, "points"), _NO_WORTH),
    Retired(("tasks", ANY, "worth"), _NO_WORTH),
    Retired(
        ("tasks", ANY, "late_per_day"),
        "A task no longer takes a share off per late day: a late penalty is a value of the "
        "task that reads seconds_late, or a leaderboard column that reads it. Remove this.",
    ),
    Retired(("leaderboards",), _OLD_BOARDS, _is_list),
    *(
        Retired(("leaderboards", ANY_KEY, key), _OLD_BOARD_KEY)
        for key in (
            "name",
            "tasks",
            "over",
            "select",
            "order",
            "who",
            "rows",
            "stage",
            "combine",
            "visibility",
            "freeze_at",
            "team_only",
        )
    ),
    Retired(
        ("freeze_at",),
        "Leaderboards no longer freeze: give the contest a hold, from a time until a time "
        "or until staff end it. Remove this.",
    ),
)

TASK_RETIRED = (
    Retired(
        ("release_at",),
        "A task's release time is now set in the contest, on the task's timeline. Set it "
        "there, and remove this.",
    ),
    Retired(
        ("hidden",),
        "A task is now shown from its release time in the contest. To hide results until "
        'the reveal, set a test group\'s "When entrants see results". Remove this.',
    ),
    Retired(
        ("limits",),
        "This setting is no longer used. Set the most submissions and how often entrants may "
        "submit instead, and the largest size on each file input. Remove this.",
    ),
    Retired(
        ("subtasks",),
        "Subtasks are now test groups. Add a test group for each, and remove this.",
    ),
    Retired(
        ("stages",),
        "This setting is no longer used. To hide results, set a test group's \"When entrants "
        'see results"; which submission counts is set on each leaderboard. Remove this.',
    ),
    Retired(
        ("inputs", "contestant"),
        "Inputs are no longer split into the entrant's and the task's. List each input of the "
        "workflow once, and remove this.",
    ),
    Retired(
        ("inputs", "setter"),
        "Inputs are no longer split into the entrant's and the task's. List each input of the "
        "workflow once, and remove this.",
    ),
    Retired(
        ("submissions", "rate"),
        "Give how often entrants may submit as a count and a number of seconds, such as 1 "
        "every 30 seconds.",
        _is_text,
    ),
)


IGNORED_ON_READ = ("on_system_error", "freeze_at")
"""Retired contest keys that nothing took the place of, so a stored file that
still holds one reads with it ignored, and only a save that has it is
refused, naming it. Leaderboards written in the old list form are ignored
on read the same way (`_old_boards`): a stored contest reads with no boards
until a save writes them as columns."""
ENTRY_IGNORED_ON_READ = ("worth", "late_per_day")
"""Retired keys of a task's entry, ignored on read and refused in a save the
same way: a task gives no points since stage 5 (stage 5 section 6, item
123)."""


def _old_boards(document: dict[str, Any]) -> bool:
    return isinstance(document.get("leaderboards"), list)


def parse_contest(text: bytes | str, *, saved: bool = False) -> ContestDefinition:
    """The `contest.yaml` in `text`, read, or, `saved`, as a save of it is
    checked: a key of `IGNORED_ON_READ`, a task entry's key of
    `ENTRY_IGNORED_ON_READ`, or leaderboards in the old list form, is
    ignored on read and refused in a save. Raises
    `InvalidDefinition` listing every problem with its YAML path.
    """
    document = load_mapping(CONTEST_FILE, text)
    if not saved:
        old = _old_boards(document)
        document = {
            key: value
            for key, value in document.items()
            if key not in IGNORED_ON_READ and not (old and key == "leaderboards")
        }
        if isinstance(tasks := document.get("tasks"), list):
            document["tasks"] = [
                {key: value for key, value in each.items() if key not in ENTRY_IGNORED_ON_READ}
                if isinstance(each, dict)
                else each
                for each in tasks
            ]
    return validate(ContestDefinition, CONTEST_FILE, document, CONTEST_RETIRED)


RUN_FORMAT_KEYS = ("repeat", "scoring")
"""The keys only a run task has; a file with either reads as one."""

RUN_FORMAT_RETIRED = (
    Retired(
        ("test_groups",),
        "This task's workflow describes one run, with no test block, so the task has no test "
        "groups: its runs are grouped under scoring's groups. Remove this.",
    ),
    Retired(
        ("credit",),
        "This task's workflow describes one run, with no test block, so what its runs earn is "
        "said by scoring's values. Remove this.",
    ),
)
TEST_FORMAT_RETIRED = (
    Retired(
        ("repeat",),
        "This task's workflow has a test block, so its tests are the folders under tests/ and "
        "nothing repeats. Choose a workflow that describes one run, such as "
        "unicon/classic@v3, or remove this.",
    ),
    Retired(
        ("scoring",),
        "This task's workflow has a test block, so it is scored by its test groups, not by "
        "scoring. Choose a workflow that describes one run, such as unicon/classic@v3, or "
        "remove this.",
    ),
    Retired(
        ("checks",),
        "This task's workflow has a test block, and checks grade a made-up submission through "
        "a run task's runs. Choose a workflow that describes one run, such as "
        "unicon/classic@v3, or remove this.",
    ),
)


def parse_task(text: bytes | str, *, runs: bool | None = None) -> TaskDefinition:
    """The `task.yaml` in `text`, in the run format when `runs`, in today's
    when not, and, when `runs` is none, in the format its keys say: the run
    format when it has `repeat` or `scoring` (`RUN_FORMAT_KEYS`). A key of
    the other format is refused naming the format, and a run task without
    `scoring` or another without `test_groups` is refused at that key.
    Raises `InvalidDefinition` listing every problem with its YAML path.
    """
    document = load_mapping(TASK_FILE, text)
    if runs is None:
        runs = any(key in document for key in RUN_FORMAT_KEYS)
    required = "scoring" if runs else "test_groups"
    # A run task's scoring left empty is none, which no run task can be.
    given = required in document and not (runs and document[required] is None)
    missing = [] if given else [Problem(path=required, message="This is required.")]
    retired = (*TASK_RETIRED, *(RUN_FORMAT_RETIRED if runs else TEST_FORMAT_RETIRED))
    try:
        found = validate(TaskDefinition, TASK_FILE, document, retired)
    except InvalidDefinition as invalid:
        raise InvalidDefinition(TASK_FILE, [*invalid.errors, *missing]) from None
    if missing:
        raise InvalidDefinition(TASK_FILE, missing)
    return found


CONTEST_ADMIN_KEYS = ("name", "description", "state", "visibility", "registration")
TASK_ADMIN_KEYS = ("name", "submissions")
ADMIN_ONLY_FILES = (STATEMENT_FILE,)
_ADMIN_ONLY_WORDS = {
    "name": "the name",
    "description": "the description",
    "state": "whether the contest is open",
    "visibility": "who can see the contest",
    "registration": "registration",
    "submissions": "the submission limits",
    STATEMENT_FILE: "the statement",
}


def admin_only_refusal(scope: str, keys: list[str]) -> str:
    """The sentence refusing a manager's change to admin-only `keys` at the
    scope named `scope`, each key named by what it is.
    """
    named = [_ADMIN_ONLY_WORDS.get(key, key) for key in keys]
    listed = named[0] if len(named) == 1 else f"{', '.join(named[:-1])} and {named[-1]}"
    return f"Only an admin of {scope} can change {listed}. Ask one of them to make this change."


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
    running five hours, with no leaderboards and no tasks, since a board's
    columns read tasks the contest does not have yet.
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
""".encode()


STARTER_STATEMENT = b"Write the statement entrants read here.\n"
"""The placeholder statement a new task is created with: the one copy of it,
which `statement_written` compares against."""


def statement_written(content: bytes | None) -> bool:
    """Whether a task's `statement.md` holds a statement: it is there, it is
    not blank, and it is not the starter's placeholder, compared with its
    spaces and line endings at either end left out.
    """
    if content is None:
        return False
    text = content.decode("utf-8", errors="replace").strip()
    return bool(text) and text != STARTER_STATEMENT.decode().strip()


def starter_task(name: str) -> dict[str, bytes]:
    """The files a new task is created with: a `task.yaml` titled `name` that
    grades Python with `unicon/classic@v2` on one group of tests, a
    placeholder statement (`STARTER_STATEMENT`), no `public/`, which its first
    picture makes, since git keeps no empty folder, and one
    test, `main/1`, whose input is `1 2` and whose answer is `3`, so the
    first save publishes.
    """
    task = f"""\
# The task's settings. The format is TASK-FORMAT.md, section 1.2.
name: {_yaml_text(name)}
workflow: unicon/classic@v2
inputs:
  submission: {{label: Your solution}}
  language: {{label: Language, options: [python]}}
  time_limit: 2
  memory_limit: 256
test_groups:
  main: {{each: 100}}
"""
    return {
        TASK_FILE: task.encode(),
        STATEMENT_FILE: STARTER_STATEMENT,
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
        found.append(
            ((key,), f"A {_KIND_WORDS[kind]} input does not take {_FORM_WORDS[key]}. Remove it.")
        )
    if details.min is not None and details.max is not None and details.max < details.min:
        found.append((("max",), "The maximum must be at least the minimum."))
    if details.max_size is not None and details.max_size > FILE_CEILING:
        found.append((("max_size",), "This must be at most 2GB, the largest file Unicon takes."))
    if details.options is not None and workflow_options is not None:
        extra = [option for option in details.options if option not in workflow_options]
        if extra:
            found.append(
                (
                    ("options",),
                    f"{extra[0]} is not one of the workflow's options, which are "
                    f"{', '.join(workflow_options)}.",
                )
            )
    if details.default is not None and "default" in allowed:
        problem = _default_problem(details, kind, workflow_options)
        if problem is not None:
            found.append((("default",), problem))
    return found


_KIND_WORDS = {
    "text": "text",
    "number": "number",
    "boolean": "true-or-false",
    "enum": "choice",
    "file": "file",
    "folder": "folder",
}
_FORM_WORDS = {
    "label": "a label",
    "default": "a default",
    "min": "a minimum",
    "max": "a maximum",
    "options": "options",
    "max_size": "a largest size",
}


def _default_problem(
    details: Form, kind: str, workflow_options: tuple[str, ...] | None
) -> str | None:
    value = details.default
    match kind:
        case "text":
            return None if isinstance(value, str) else "The default must be text."
        case "boolean":
            return None if isinstance(value, bool) else "The default must be true or false."
        case "number":
            if not is_number(value):
                return "The default must be a number."
            if details.min is not None and value < details.min:
                return "The default must be at least the minimum."
            if details.max is not None and value > details.max:
                return "The default must be at most the maximum."
            return None
        case "enum":
            offered = details.options or workflow_options or ()
            return None if value in offered else f"The default must be one of {', '.join(offered)}."
    return None
