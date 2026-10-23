from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError

from forge.domain.definitions.basics import (
    CONTEST_FILE,
    STARTER_STATEMENT,
    STATEMENT_FILE,
    TASK_FILE,
    _yaml_text,
)
from forge.domain.definitions.contest import ContestDefinition
from forge.domain.definitions.retired_keys import (
    CONTEST_RETIRED,
    ENTRY_IGNORED_ON_READ,
    IGNORED_ON_READ,
    TASK_RETIRED,
    _old_leaderboards,
)
from forge.domain.definitions.task import FILE_CEILING, Form, TaskDefinition
from forge.domain.yaml_models import (
    InvalidDefinition,
    Problem,
    Retired,
    is_number,
    load_mapping,
    mapping_or_empty,
    problems_of,
    read_time,
    validate,
)


def parse_contest(text: bytes | str, *, saved: bool = False) -> ContestDefinition:
    """The `contest.yaml` in `text`, read, or, `saved`, as a save of it is
    checked: a key of `IGNORED_ON_READ`, a task entry's key of
    `ENTRY_IGNORED_ON_READ`, or leaderboards in the old list form, is
    ignored on read and refused in a save. Raises
    `InvalidDefinition` listing every problem with its YAML path.
    """
    document = load_mapping(CONTEST_FILE, text)
    if not saved:
        old = _old_leaderboards(document)
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


def stamp(moment: datetime) -> str:
    """A moment as `contest.yaml` writes one, in UTC to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def starter_contest(name: str, now: datetime) -> bytes:
    """The `contest.yaml` a new contest is created with: titled `name`, a
    draft seen by anyone signed in, open to register with manual approval,
    starting on the first full hour at least seven days after `now` and
    running five hours, with no leaderboards and no tasks, since a leaderboard's
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


def statement_written(content: bytes | None) -> bool:
    """Whether a task's `statement.md` holds a statement: it is there, it is
    not blank, and it is not the starter's placeholder, compared with its
    spaces and line endings at either end left out.
    """
    if content is None:
        return False
    text = content.decode("utf-8", errors="replace").strip()
    return bool(text) and text != STARTER_STATEMENT.decode().strip()


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
