"""The two definition files: what `contest.yaml` and `task.yaml` accept, the
path every refusal names, the file inputs a task names with the YAML path
of each, the starter files, and which admin-only keys a save changes.
"""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from forge.domain.definitions import (
    CONTEST_ADMIN_KEYS,
    DEFAULT_STAGE,
    ContestVisibility,
    InvalidDefinition,
    Leaderboard,
    OrderKey,
    Rate,
    Show,
    State,
    Trigger,
    admin_only_changes,
    parse_contest,
    parse_rate,
    parse_task,
    starter_contest,
    starter_task,
)
from forge.domain.errors import ServiceError
from forge.domain.workflow_definition import InputType, WorkflowRef

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)

CONTEST_EXAMPLE = b"""\
name: Example Contest 2026
start: 2026-06-01T09:00:00Z
end: 2026-06-01T14:00:00Z
state: published
submissions_closed: false
visibility: public

registration:
  mode: open
  opens: 2026-05-01T00:00:00Z
  closes: 2026-06-01T09:00:00Z
  approval: manual
  eligibility:
    email_pattern: ".*@u\\\\.nus\\\\.edu"
    invite_code: null
  capacity: 500

teams:
  enabled: true
  max_size: 3

leaderboards:
  - name: Official
    tasks: [sum-of-two, shortest-path-output]
    stage: default
    select: best
    combine: sum
    order:
      - metric: points
        direction: desc
      - key: last_improvement
    visibility: public
    freeze_at: 2026-06-01T13:00:00Z
  - name: Team standings
    select: selected
    combine: sum
    order:
      - metric: points
        direction: desc
    visibility: contestants
    team_only: true
  - name: ICPC standings
    select: first_accepted
    combine: count
    order:
      - metric: solved
        direction: desc
      - key: penalty
        per_rejected: 20
    visibility: public

tasks:
  - id: sum-of-two
    label: A
    points: 100
  - id: shortest-path-output
    label: B
    points: 100
"""

TASK_EXAMPLE = b"""\
name: Sum of Two
workflow: unicon/checked@v1
release_at: 2026-06-01T10:00:00Z
hidden: false

inputs:
  contestant:
    - id: submission
      type: code
      label: "Your Solution"
      language: [c++, python, java]

  setter:
    - id: checker
      type: file
      value: checker/checker.cpp
    - id: time_limit
      type: number
      value: 2.0
    - id: memory_limit
      type: number
      value: 256

limits:
  submissions: 50
  rate: 1 per 30s
  max_size: 10MB

subtasks:
  - id: subtask1
    points: 30
    inputs:
      setter:
        - id: testcases
          type: file[]
          value: data/testcases/subtask1/

  - id: subtask2
    points: 70
    workflow: unicon/checked@v2
    inputs:
      setter:
        - id: testcases
          type: file[]
          value: data/testcases/subtask2/
        - id: time_limit
          type: number
          value: 3.0

stages:
  - id: validation
    trigger: on_submit
    show: full
    counts: false
    inputs:
      setter:
        - id: testcases
          type: file[]
          value: data/testcases/public/

  - id: test
    trigger: at_end
    show: hidden
    counts: true
    workflow: unicon/checked@v2
    inputs:
      setter:
        - id: testcases
          type: file[]
          value: data/testcases/hidden/
"""

PARAMETERS_EXAMPLE = b"""\
name: Tunable Solver
workflow: my-org/tunable-eval@v1

inputs:
  contestant:
    - id: submission
      type: code
      label: "Your Solver"
      language: [c++, python]
    - id: threshold
      type: number
      label: "Decision Threshold"
      min: 0.0
      max: 1.0
      default: 0.5
    - id: use_heuristic
      type: boolean
      label: "Enable heuristic?"
      default: false
    - id: answer_file
      type: file
      label: "Your Output"
      accept: [.txt]
      max_size: 10MB

  setter:
    - id: testcases
      type: file[]
      value: data/testcases/
    - id: weights
      type: dataset
      value: datasets/weights.bin

subtasks:
  - id: main
    points: 100
"""


def contest_document() -> dict[str, Any]:
    document = yaml.safe_load(CONTEST_EXAMPLE)
    assert isinstance(document, dict)
    return document


def task_document() -> dict[str, Any]:
    document = yaml.safe_load(TASK_EXAMPLE)
    assert isinstance(document, dict)
    return document


def refused(error: pytest.ExceptionInfo[InvalidDefinition]) -> dict[str, str]:
    return {problem["path"]: problem["message"] for problem in error.value.errors}


def test_the_format_documents_example_contest_parses() -> None:
    contest = parse_contest(CONTEST_EXAMPLE)
    assert contest.name == "Example Contest 2026"
    assert contest.start == datetime(2026, 6, 1, 9, tzinfo=UTC)
    assert contest.state is State.PUBLISHED
    assert contest.visibility is ContestVisibility.PUBLIC
    assert contest.registration.capacity == 500
    assert contest.registration.eligibility.email_pattern == r".*@u\.nus\.edu"
    assert contest.registration.eligibility.invite_code is None
    official, _, icpc = contest.leaderboards
    assert official.tasks == ("sum-of-two", "shortest-path-output")
    assert official.order[1].key is OrderKey.LAST_IMPROVEMENT
    assert icpc.order[1].per_rejected == 20
    assert [task.label for task in contest.tasks] == ["A", "B"]


def test_a_contest_leaves_the_optional_blocks_to_their_defaults() -> None:
    contest = parse_contest(
        b"name: Short\nstart: 2026-06-01T09:00:00Z\nend: 2026-06-01T12:00:00+01:00\n"
        b"state: draft\nvisibility: hidden\n"
    )
    assert contest.description == ""
    assert contest.submissions_closed is False
    assert contest.registration.mode == "open"
    assert contest.registration.approval == "manual"
    assert contest.registration.opens is None
    assert contest.registration.capacity is None
    assert contest.teams.enabled is False
    assert contest.teams.max_size == 3
    assert contest.leaderboards == ()
    assert contest.tasks == ()


def test_a_leaderboard_reads_the_default_stage_unless_it_names_one() -> None:
    board = Leaderboard.model_validate(
        {
            "name": "Board",
            "select": "latest",
            "combine": "mean",
            "order": [{"metric": "f1", "direction": "asc"}],
            "visibility": "organisers",
        }
    )
    assert board.stage == DEFAULT_STAGE
    assert board.tasks is None
    assert board.team_only is False


def _set(document: dict[str, Any], path: str, value: Any) -> None:
    """Sets a dotted path with [i] indices; a value of `...` deletes it."""
    parts: list[str | int] = []
    for piece in path.split("."):
        name, _, rest = piece.partition("[")
        if name:
            parts.append(name)
        for index in rest.rstrip("]").split("]["):
            if index:
                parts.append(int(index))
    target: Any = document
    for part in parts[:-1]:
        target = target[part]
    if value is ...:
        del target[parts[-1]]
    else:
        target[parts[-1]] = value


MALFORMED_CONTESTS: list[tuple[str, Any, str]] = [
    ("name", ..., "name"),
    ("name", "   ", "name"),
    ("name", 2026, "name"),
    ("start", "2026-06-01T09:00:00", "start"),
    ("start", datetime(2026, 6, 1).date(), "start"),
    ("end", datetime(2026, 6, 1, 8, tzinfo=UTC), "end"),
    ("state", "running", "state"),
    ("visibility", "private", "visibility"),
    ("submissions_closed", "yes", "submissions_closed"),
    ("registration.mode", "closed", "registration.mode"),
    ("registration.approval", "later", "registration.approval"),
    ("registration.closes", datetime(2026, 4, 1, tzinfo=UTC), "registration.closes"),
    ("registration.capacity", 0, "registration.capacity"),
    ("registration.eligibility.email_pattern", "([", "registration.eligibility.email_pattern"),
    ("registration.eligibility.invite_code", "", "registration.eligibility.invite_code"),
    ("registration.aproval", "auto", "registration.aproval"),
    ("teams.max_size", 0, "teams.max_size"),
    ("teams.enabled", 1, "teams.enabled"),
    ("leaderboards[0].select", "worst", "leaderboards[0].select"),
    ("leaderboards[0].combine", "product", "leaderboards[0].combine"),
    ("leaderboards[0].visibility", "everyone", "leaderboards[0].visibility"),
    ("leaderboards[0].order[0].direction", "up", "leaderboards[0].order[0].direction"),
    ("leaderboards[0].order[0].direction", ..., "leaderboards[0].order[0].direction"),
    ("leaderboards[0].order[1].direction", "asc", "leaderboards[0].order[1].direction"),
    ("leaderboards[0].order[1].key", "speed", "leaderboards[0].order[1].key"),
    ("leaderboards[0].order[1].per_rejected", 20, "leaderboards[0].order[1].per_rejected"),
    ("leaderboards[0].order[0]", {}, "leaderboards[0].order[0]"),
    ("leaderboards[0].order[0].key", "penalty", "leaderboards[0].order[0].key"),
    ("leaderboards[0].order", [], "leaderboards[0].order"),
    ("leaderboards[0].tasks", ["sum-of-two", "nope"], "leaderboards[0].tasks[1]"),
    ("leaderboards[0].freeze_at", "tomorrow", "leaderboards[0].freeze_at"),
    ("leaderboards[0].stage", "Final Round", "leaderboards[0].stage"),
    ("leaderboards[1].name", "Official", "leaderboards[1].name"),
    ("leaderboards[2].order[1].per_rejected", -1, "leaderboards[2].order[1].per_rejected"),
    ("teams.enabled", False, "leaderboards[1].team_only"),
    ("tasks[1].id", "sum-of-two", "tasks[1].id"),
    ("tasks[1].label", "A", "tasks[1].label"),
    ("tasks[0].id", "Sum Of Two", "tasks[0].id"),
    ("tasks[0].id", "a" * 25, "tasks[0].id"),
    ("tasks[0].points", -1, "tasks[0].points"),
    ("tasks[0].points", 99.5, "tasks[0].points"),
    ("tasks[0].label", ..., "tasks[0].label"),
    ("visbility", "public", "visbility"),
    ("tasks", {"id": "sum-of-two"}, "tasks"),
]


@pytest.mark.parametrize(("key", "value", "path"), MALFORMED_CONTESTS)
def test_a_malformed_contest_is_refused_at_the_path_that_is_wrong(
    key: str, value: Any, path: str
) -> None:
    document = contest_document()
    _set(document, key, value)
    with pytest.raises(InvalidDefinition) as error:
        parse_contest(yaml.safe_dump(document))
    assert path in refused(error), refused(error)


def test_each_refusal_is_a_sentence_and_a_typo_says_so() -> None:
    document = contest_document()
    _set(document, "registration.capacity", 0)
    _set(document, "visbility", "public")
    _set(document, "leaderboards[0].order[1].key", "speed")
    with pytest.raises(InvalidDefinition) as error:
        parse_contest(yaml.safe_dump(document))
    messages = refused(error)
    assert messages == {
        "visbility": "This key is not part of the format; check its spelling.",
        "registration.capacity": "Must be at least 1.",
        "leaderboards[0].order[1].key": "Must be one of last_improvement or penalty.",
    }
    assert error.value.code == "invalid_definition"
    assert isinstance(error.value, ServiceError)
    assert error.value.extra["errors"] == error.value.errors
    assert "has 3 problems" in error.value.detail


def test_every_problem_in_one_file_is_reported_together() -> None:
    document = contest_document()
    _set(document, "state", "running")
    _set(document, "teams.max_size", 0)
    _set(document, "tasks[0].points", -1)
    with pytest.raises(InvalidDefinition) as error:
        parse_contest(yaml.safe_dump(document))
    assert set(refused(error)) == {"state", "teams.max_size", "tasks[0].points"}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (b"name: [unclosed\n", "line 2"),
        (b"name: a\n  bad: indent\n", "line 2"),
        (b"name: a\nname: b\n", "given twice"),
        (b"- name: a\n", "mapping"),
        (b"", "mapping"),
        (b"just text", "mapping"),
    ],
)
def test_a_file_that_is_not_a_mapping_is_one_problem_at_the_top(text: bytes, fragment: str) -> None:
    for parse in (parse_contest, parse_task):
        with pytest.raises(InvalidDefinition) as error:
            parse(text)
        [problem] = error.value.errors
        assert problem["path"] == ""
        assert fragment in problem["message"]


def test_the_format_documents_example_task_parses() -> None:
    task = parse_task(TASK_EXAMPLE)
    assert task.workflow == WorkflowRef("unicon", "checked", "v1")
    assert str(task.workflow) == "unicon/checked@v1"
    assert task.release_at == datetime(2026, 6, 1, 10, tzinfo=UTC)
    assert task.inputs.contestant[0].language == ("c++", "python", "java")
    assert task.limits.submissions == 50
    assert task.limits.rate == Rate(1, timedelta(seconds=30))
    assert task.limits.max_size == 10 * 1024 * 1024
    assert task.subtasks[1].workflow == WorkflowRef("unicon", "checked", "v2")


def test_a_task_with_contestant_parameters_and_a_dataset_parses() -> None:
    task = parse_task(PARAMETERS_EXAMPLE)
    threshold = task.inputs.contestant[1]
    assert (threshold.min, threshold.max, threshold.default) == (0.0, 1.0, 0.5)
    assert task.inputs.contestant[3].accept == (".txt",)
    assert task.inputs.contestant[3].max_size == 10 * 1024 * 1024
    assert task.named_files() == ("data/testcases/", "datasets/weights.bin")


def test_a_task_leaves_limits_release_and_stages_to_their_defaults() -> None:
    task = parse_task(b"name: Bare\nworkflow: unicon/classic@v1\n")
    assert task.hidden is False
    assert task.release_at is None
    assert task.limits.as_mapping() == {
        "submissions": 50,
        "rate": "1 per 30s",
        "max_size": 10 * 1024 * 1024,
    }
    [stage] = task.stages_resolved()
    assert (stage.id, stage.trigger, stage.show, stage.counts) == (
        DEFAULT_STAGE,
        Trigger.ON_SUBMIT,
        Show.FULL,
        True,
    )
    assert stage.index is None


def test_a_stage_takes_its_own_workflow_and_setter_inputs_and_inherits_the_rest() -> None:
    validation, test = parse_task(TASK_EXAMPLE).stages_resolved()
    assert validation.workflow == WorkflowRef("unicon", "checked", "v1")
    assert (validation.trigger, validation.show, validation.counts) == (
        Trigger.ON_SUBMIT,
        Show.FULL,
        False,
    )
    assert [(entry.id, entry.value) for entry in validation.setter] == [
        ("checker", "checker/checker.cpp"),
        ("time_limit", 2.0),
        ("memory_limit", 256),
        ("testcases", "data/testcases/public/"),
    ]
    assert test.workflow == WorkflowRef("unicon", "checked", "v2")
    assert (test.trigger, test.show, test.index) == (Trigger.AT_END, Show.HIDDEN, 1)
    assert {entry.id: entry.value for entry in test.setter}["testcases"] == (
        "data/testcases/hidden/"
    )


def test_a_stage_override_replaces_the_task_level_input_in_place() -> None:
    task = parse_task(
        b"name: T\nworkflow: unicon/classic@v1\n"
        b"inputs:\n  setter:\n"
        b"    - {id: time_limit, type: number, value: 2}\n"
        b"    - {id: memory_limit, type: number, value: 256}\n"
        b"stages:\n  - id: slow\n    inputs:\n      setter:\n"
        b"        - {id: time_limit, type: number, value: 9}\n"
    )
    [slow] = task.stages_resolved()
    assert [(entry.id, entry.value) for entry in slow.setter] == [
        ("time_limit", 9),
        ("memory_limit", 256),
    ]


def test_named_files_lists_every_path_at_every_level_once() -> None:
    assert parse_task(TASK_EXAMPLE).named_files() == (
        "checker/checker.cpp",
        "data/testcases/hidden/",
        "data/testcases/public/",
        "data/testcases/subtask1/",
        "data/testcases/subtask2/",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 per 30s", Rate(1, timedelta(seconds=30))),
        ("5 per 10m", Rate(5, timedelta(minutes=10))),
        ("20 per 1h", Rate(20, timedelta(hours=1))),
        (" 3 per 45s ", Rate(3, timedelta(seconds=45))),
    ],
)
def test_a_rate_is_a_count_in_a_window(text: str, expected: Rate) -> None:
    assert parse_rate(text) == expected


MALFORMED_TASKS: list[tuple[str, Any, str]] = [
    ("name", ..., "name"),
    ("workflow", ..., "workflow"),
    ("workflow", "classic", "workflow"),
    ("workflow", "unicon/classic", "workflow"),
    ("workflow", "Unicon/classic@v1", "workflow"),
    ("workflow", "unicon/classic@", "workflow"),
    ("release_at", datetime(2026, 6, 1).date(), "release_at"),
    ("hidden", "no", "hidden"),
    ("inputs.contestant[0].type", "video", "inputs.contestant[0].type"),
    ("inputs.contestant[0].type", "dataset", "inputs.contestant[0].type"),
    ("inputs.contestant[0].language", [], "inputs.contestant[0].language"),
    ("inputs.contestant[0].min", 1, "inputs.contestant[0].min"),
    ("inputs.contestant[0].accept", [".cpp"], "inputs.contestant[0].accept"),
    ("inputs.contestant[0].default", 3, "inputs.contestant[0].default"),
    ("inputs.contestant[0].colour", "red", "inputs.contestant[0].colour"),
    ("inputs.contestant[0].id", "Submission", "inputs.contestant[0].id"),
    ("inputs.setter[0].value", "checker/", "inputs.setter[0].value"),
    ("inputs.setter[0].value", "../etc/passwd", "inputs.setter[0].value"),
    ("inputs.setter[0].value", "/checker.cpp", "inputs.setter[0].value"),
    ("inputs.setter[0].value", "checker\\checker.cpp", "inputs.setter[0].value"),
    ("inputs.setter[0].type", "jupyter", "inputs.setter[0].type"),
    ("inputs.setter[1].value", "two", "inputs.setter[1].value"),
    ("inputs.setter[1].value", True, "inputs.setter[1].value"),
    ("inputs.setter[1].value", ..., "inputs.setter[1].value"),
    ("inputs.setter[2].id", "time_limit", "inputs.setter[2].id"),
    ("inputs.cheater", [], "inputs.cheater"),
    ("limits.submissions", 0, "limits.submissions"),
    ("limits.rate", "fast", "limits.rate"),
    ("limits.rate", "0 per 30s", "limits.rate"),
    ("limits.max_size", "10 parsecs", "limits.max_size"),
    ("limits.max_size", 10, "limits.max_size"),
    ("subtasks[0].points", 1.5, "subtasks[0].points"),
    ("subtasks[1].id", "subtask1", "subtasks[1].id"),
    ("subtasks[1].workflow", "checked@v2", "subtasks[1].workflow"),
    (
        "subtasks[1].inputs.setter[1]",
        {"id": "time_limit", "type": "text", "value": "3"},
        "subtasks[1].inputs.setter[1].type",
    ),
    ("subtasks[1].inputs.setter[1].id", "testcases", "subtasks[1].inputs.setter[1].id"),
    (
        "subtasks[0].inputs.setter[0].value",
        "data/testcases/subtask1",
        "subtasks[0].inputs.setter[0].value",
    ),
    ("stages[1].id", "validation", "stages[1].id"),
    ("stages[0].trigger", "later", "stages[0].trigger"),
    ("stages[0].show", "partial", "stages[0].show"),
    ("stages[0].counts", "no", "stages[0].counts"),
    ("stages[0].id", "Public Tests", "stages[0].id"),
    ("stages[0].inputs.contestant", [], "stages[0].inputs.contestant"),
]


@pytest.mark.parametrize(("key", "value", "path"), MALFORMED_TASKS)
def test_a_malformed_task_is_refused_at_the_path_that_is_wrong(
    key: str, value: Any, path: str
) -> None:
    document = task_document()
    _set(document, key, value)
    with pytest.raises(InvalidDefinition) as error:
        parse_task(yaml.safe_dump(document))
    assert path in refused(error), refused(error)


def test_a_contestant_numbers_bounds_hold_its_default() -> None:
    document = yaml.safe_load(PARAMETERS_EXAMPLE)
    _set(document, "inputs.contestant[1].default", 2.0)
    _set(document, "inputs.contestant[2].default", "maybe")
    _set(document, "inputs.contestant[3].default", "out.txt")
    with pytest.raises(InvalidDefinition) as error:
        parse_task(yaml.safe_dump(document))
    assert refused(error) == {
        "inputs.contestant[1].default": "Must be at most max.",
        "inputs.contestant[2].default": "Must be true or false.",
        "inputs.contestant[3].default": "A file input has no default.",
    }
    document = yaml.safe_load(PARAMETERS_EXAMPLE)
    _set(document, "inputs.contestant[1].min", 2.0)
    with pytest.raises(InvalidDefinition) as error:
        parse_task(yaml.safe_dump(document))
    assert set(refused(error)) == {"inputs.contestant[1].max", "inputs.contestant[1].default"}


def test_the_starter_contest_is_valid_and_starts_on_the_hour_a_week_on() -> None:
    text = starter_contest('Spring: "2026"', NOW)
    assert text.startswith(b"# ")
    assert b"TASK-FORMAT.md" in text.splitlines()[0]
    contest = parse_contest(text)
    assert contest.name == 'Spring: "2026"'
    assert contest.state is State.DRAFT
    assert contest.visibility is ContestVisibility.SIGNED_IN
    assert contest.registration.mode == "open"
    assert contest.registration.approval == "manual"
    assert contest.start == datetime(2026, 10, 6, 11, tzinfo=UTC)
    assert contest.end == contest.start + timedelta(hours=5)
    assert contest.teams.enabled is False
    assert contest.leaderboards == ()
    assert contest.tasks == ()


def test_the_starter_contest_on_the_hour_starts_exactly_a_week_on() -> None:
    on_the_hour = datetime(2026, 9, 29, 10, tzinfo=UTC)
    assert parse_contest(starter_contest("x", on_the_hour)).start == on_the_hour + timedelta(days=7)


def test_the_starter_task_is_valid_and_names_only_files_it_carries() -> None:
    files = starter_task("Sum of Two")
    assert set(files) == {
        "task.yaml",
        "statement.md",
        "data/testcases/.gitkeep",
        "checker/.gitkeep",
    }
    assert files["data/testcases/.gitkeep"] == b""
    assert files["checker/.gitkeep"] == b""
    assert files["statement.md"].count(b"\n") == 1
    task = parse_task(files["task.yaml"])
    assert task.name == "Sum of Two"
    assert str(task.workflow) == "unicon/classic@v1"
    [submission] = task.inputs.contestant
    assert (submission.id, submission.type, submission.label, submission.language) == (
        "submission",
        InputType.CODE,
        "Your solution",
        ("python",),
    )
    assert [(entry.id, entry.type, entry.value) for entry in task.inputs.setter] == [
        ("testcases", InputType.FILES, "data/testcases/"),
        ("time_limit", InputType.NUMBER, 2.0),
        ("memory_limit", InputType.NUMBER, 256),
    ]
    assert task.limits == parse_task(b"name: x\nworkflow: a/b@v1\n").limits
    for path in task.named_files():
        if path.endswith("/"):
            assert any(name.startswith(path) for name in files)
        else:
            assert path in files


CONTEST_CHANGES: list[tuple[str, Any]] = [
    ("name", "Renamed"),
    ("description", "Now described"),
    ("start", datetime(2026, 6, 1, 8, tzinfo=UTC)),
    ("end", datetime(2026, 6, 1, 15, tzinfo=UTC)),
    ("submissions_closed", True),
    ("state", "archived"),
    ("visibility", "hidden"),
    ("registration", {"mode": "invite-only"}),
]


@pytest.mark.parametrize(("key", "value"), CONTEST_CHANGES)
def test_changing_an_admin_only_contest_key_is_named(key: str, value: Any) -> None:
    before = contest_document()
    after = copy.deepcopy(before)
    after[key] = value
    changed = admin_only_changes(
        "contest", yaml.safe_dump(before).encode(), yaml.safe_dump(after).encode()
    )
    assert changed == [key]


def test_a_change_deep_in_the_registration_block_names_the_block() -> None:
    before = contest_document()
    after = copy.deepcopy(before)
    after["registration"]["eligibility"]["invite_code"] = "sesame"
    changed = admin_only_changes(
        "contest", yaml.safe_dump(before).encode(), yaml.safe_dump(after).encode()
    )
    assert changed == ["registration"]


def test_changing_manager_keys_changes_nothing_admin_only() -> None:
    before = contest_document()
    after = copy.deepcopy(before)
    after["teams"] = {"enabled": False}
    after["leaderboards"] = []
    after["tasks"].append({"id": "extra", "label": "C", "points": 10})
    assert admin_only_changes("contest", CONTEST_EXAMPLE, yaml.safe_dump(after).encode()) == []


def test_the_same_settings_written_differently_change_nothing() -> None:
    rewritten = b"# a comment\n" + yaml.safe_dump(contest_document(), sort_keys=True).encode()
    assert admin_only_changes("contest", CONTEST_EXAMPLE, rewritten) == []
    shifted = CONTEST_EXAMPLE.replace(
        b"start: 2026-06-01T09:00:00Z", b"start: 2026-06-01T17:00:00+08:00"
    )
    assert admin_only_changes("contest", CONTEST_EXAMPLE, shifted) == []


def test_adding_or_removing_an_admin_key_counts_as_a_change() -> None:
    without = contest_document()
    del without["submissions_closed"]
    assert admin_only_changes("contest", CONTEST_EXAMPLE, yaml.safe_dump(without).encode()) == [
        "submissions_closed"
    ]
    assert admin_only_changes("contest", yaml.safe_dump(without).encode(), CONTEST_EXAMPLE) == [
        "submissions_closed"
    ]


def test_a_new_file_names_every_admin_key_it_sets() -> None:
    assert admin_only_changes("contest", None, CONTEST_EXAMPLE) == [
        key for key in CONTEST_ADMIN_KEYS if key != "description"
    ]
    assert admin_only_changes("task", None, TASK_EXAMPLE) == ["name", "limits"]


def test_an_unparsable_side_compares_as_empty() -> None:
    assert admin_only_changes("task", TASK_EXAMPLE, b"name: [broken") == ["name", "limits"]
    assert admin_only_changes("task", b"- a list", b"workflow: unicon/classic@v1\n") == []


def test_the_task_admin_keys_are_the_name_and_the_limits() -> None:
    document = task_document()
    for key, value in (("name", "Renamed"), ("limits", {"submissions": 10})):
        after = copy.deepcopy(document)
        after[key] = value
        assert admin_only_changes("task", TASK_EXAMPLE, yaml.safe_dump(after).encode()) == [key]
    manager = copy.deepcopy(document)
    manager["workflow"] = "unicon/checked@v2"
    manager["hidden"] = True
    manager["release_at"] = datetime(2026, 6, 2, tzinfo=UTC)
    manager["inputs"]["setter"][1]["value"] = 4.0
    manager["stages"] = []
    manager["subtasks"] = []
    assert admin_only_changes("task", TASK_EXAMPLE, yaml.safe_dump(manager).encode()) == []


def test_every_file_input_is_named_with_the_yaml_path_of_its_value() -> None:
    task = parse_task(
        starter_task("Sum")["task.yaml"]
        + b"""
stages:
  - id: public
    inputs:
      setter:
        - id: testcases
          type: file[]
          value: data/public/
"""
    )

    assert task.file_inputs() == (
        ("inputs.setter[0].value", "data/testcases/"),
        ("stages[0].inputs.setter[0].value", "data/public/"),
    )
    assert task.missing_files(lambda path: path == "data/testcases/") == [
        {
            "path": "stages[0].inputs.setter[0].value",
            "message": "There is no file under data/public/ in the task.",
        }
    ]
