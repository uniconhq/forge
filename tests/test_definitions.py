"""The two definition files, `contest.yaml` and `task.yaml`: the format's own
examples and the starters validate, every key the format no longer has is
refused at that key with the sentence that says what replaced it, the checks
each file makes of itself (a task entry's timeline, C1; a leaderboard's rules,
C3) refuse at the path that is wrong, and a save names the admin-only keys
it changes.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forge.domain.definitions import (
    CONTEST_ADMIN_KEYS,
    DEFAULT_RATE,
    DEFAULT_SUBMISSIONS,
    TASK_ADMIN_KEYS,
    Approval,
    ContestVisibility,
    Entrants,
    InviteWay,
    Locks,
    Registration,
    SignupWay,
    State,
    Way,
    admin_only_changes,
    form_problems,
    letters,
    parse_contest,
    parse_task,
    starter_contest,
    starter_task,
    title_of,
)
from forge.domain.errors import ServiceError
from forge.domain.leaderboard_task_check import marks_of
from forge.domain.workflow_definition import WorkflowRef
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml, one_line, parse_size

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)

CONTEST_EXAMPLE = b"""\
name: Example Contest 2026
description: A short paragraph
start: 2026-06-01T09:00:00Z
end: 2026-06-01T14:00:00Z
state: published
visibility: everyone
registration:
  opens: 2026-05-01T00:00:00Z
  closes: 2026-06-01T09:00:00Z
  capacity: 500
  ways:
    - invite: {approval: auto}
    - signup: {name: NUS students, domains: [u.nus.edu], approval: auto}
    - signup: {name: Guests, code: guest-2026, approval: manual}
    - signup: {email_pattern: ".*@u\\\\.nus\\\\.edu"}
entrants:
  most: 3
  fewest: 1
  locks: start
leaderboards:
  standings:
    title: Standings
    audience: {who: entrants, rows: 10}
    columns:
      picked:
        block: latest
        from: {results: shortest}
        where: {value: [shortest, marked]}
      score:
        block: otherwise
        of: [{value: [{column: picked}, score]}, 0]
    sort: [{score: desc}]
    display: [{score: {places: 2}}]
hold: {from: 2026-06-01T13:00:00Z, until: staff}
tasks:
  - {id: sum-of-two}
  - id: shortest
    release_at: 2026-06-01T10:00:00Z
    due: 2026-06-01T12:00:00Z
    closes: 2026-06-01T13:00:00Z
    marks: 2
"""

TASK_EXAMPLE = b"""\
name: Shortest Path
workflow: unicon/checked@v1
inputs:
  code: {entrant: {label: Your solution, max_size: 1MB}}
  language: {entrant: {options: [cpp, python], default: cpp}}
  checker: checker/checker.cpp
  time_limit: 2
  memory_limit: 256
repeat:
  - inputs: [input, answer]
    tuples:
      - [samples/1.in, samples/1.ans]
      - [tests/2.in, tests/2.ans]
scoring:
  groups:
    samples: {input: [samples/1.in]}
  values:
    score: {block: count, over: {runs: all}}
  show:
    now: [samples]
    after_close: [score]
submissions:
  max: 50
  rate: {count: 1, per: 30}
"""

HEAD = """\
name: C
start: 2026-06-01T09:00:00Z
end: 2026-06-01T14:00:00Z
state: published
visibility: everyone
"""

START = datetime(2026, 6, 1, 9, tzinfo=UTC)
END = datetime(2026, 6, 1, 14, tzinfo=UTC)


def contest_problems(text: str) -> list[Problem]:
    """What a save of `text` is refused with."""
    with pytest.raises(InvalidDefinition) as refused:
        parse_contest(text, saved=True)
    return refused.value.errors


def task_problems(text: str) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as refused:
        parse_task(text)
    return refused.value.errors


def paths(problems: list[Problem]) -> list[str]:
    return [problem["path"] for problem in problems]


def at(problems: list[Problem], path: str) -> str:
    """The one sentence the problems give at `path`."""
    found = [problem["message"] for problem in problems if problem["path"] == path]
    assert len(found) == 1, problems
    return found[0]


def task_text(extra: str = "") -> str:
    return "name: T\nworkflow: unicon/classic@v3\nscoring: {}\n" + extra


# contest.yaml


def test_the_format_documents_example_contest_parses() -> None:
    contest = parse_contest(CONTEST_EXAMPLE)

    assert (contest.state, contest.visibility, contest.entrants.most) == (
        State.PUBLISHED,
        ContestVisibility.EVERYONE,
        3,
    )
    invite, students, guests, pattern = contest.registration.ways
    assert invite == Way(invite=InviteWay(approval=Approval.AUTO))
    assert students.signup == SignupWay(
        name="NUS students", domains=("u.nus.edu",), approval=Approval.AUTO
    )
    assert (guests.name, guests.approval) == ("Guests", Approval.MANUAL)
    assert pattern.signup is not None
    assert pattern.signup.email_pattern == r".*@u\.nus\.edu"
    assert contest.registration.invite_way == 0
    assert contest.registration.capacity == 500
    assert contest.entrants == Entrants(most=3, fewest=1, locks=Locks.START)
    standings = contest.leaderboards["standings"]
    assert (standings.title, standings.audience.who, standings.audience.rows) == (
        "Standings",
        "entrants",
        10,
    )
    assert (standings.sort, standings.display) == ((("score", "desc"),), (("score", 2),))
    assert contest.hold is not None
    assert (contest.hold.from_, contest.hold.until) == (
        datetime(2026, 6, 1, 13, tzinfo=UTC),
        "staff",
    )
    shortest = contest.tasks[1]
    assert shortest.due == datetime(2026, 6, 1, 12, tzinfo=UTC)
    assert marks_of(contest, shortest) == 2
    assert marks_of(contest, contest.tasks[0]) is None
    assert [contest.label_of(task.id) for task in contest.tasks] == ["A", "B"]


def test_a_contest_leaves_the_optional_blocks_to_their_defaults() -> None:
    contest = parse_contest(HEAD + "leaderboards: {standings: {}}\ntasks: [{id: a}]\n")

    assert contest.description is None
    assert contest.entrants == Entrants(most=1, fewest=1, locks=Locks.START)
    assert contest.registration.ways == (Way(signup=SignupWay(approval=Approval.MANUAL)),)
    assert contest.registration.invite_way is None
    assert contest.registration.capacity is None
    leaderboard = contest.leaderboards["standings"]
    assert (
        leaderboard.title,
        leaderboard.audience.who,
        leaderboard.audience.rows,
        leaderboard.sort,
    ) == (
        None,
        "staff",
        "all",
        (),
    )
    assert (leaderboard.columns, leaderboard.display, contest.hold) == ({}, (), None)
    entry = contest.tasks[0]
    assert (entry.release_at, entry.due, entry.closes, entry.marks) == (None, None, None, None)
    assert contest.release_of(entry) == START
    assert contest.closes_of(entry) == END
    assert contest.entry("a") == entry
    assert contest.entry("b") is None
    assert contest.label_of("b") is None


def test_a_label_is_the_entrys_place_as_a_letter() -> None:
    assert [letters(index) for index in (0, 1, 25, 26, 27, 51, 52, 701, 702)] == [
        "A",
        "B",
        "Z",
        "AA",
        "AB",
        "AZ",
        "BA",
        "ZZ",
        "AAA",
    ]


@pytest.mark.parametrize(
    ("extra", "path", "sentence"),
    [
        ("submissions_closed: false\n", "submissions_closed", "its own closing time"),
        ("teams: {enabled: true, max_size: 3}\n", "teams", "the most people in a team"),
        ("team_size: 3\n", "team_size", "the most people in a team"),
        ("on_system_error: last_result\n", "on_system_error", "tried again once by itself"),
        ("on_system_error: grading\n", "on_system_error", "keeps its last result"),
        ("registration: {mode: open}\n", "registration.mode", "ways in"),
        (
            "registration: {eligibility: {email_pattern: x}}\n",
            "registration.eligibility",
            "ways in",
        ),
        ("registration: {invite_only: true}\n", "registration.invite_only", "ways in"),
        ("registration: {approval: auto}\n", "registration.approval", "ways in"),
        ("registration: {email_pattern: x}\n", "registration.email_pattern", "Add a way in"),
        ("registration: {code: sesame}\n", "registration.code", "Add a way in"),
        ("tasks: [{id: a, label: A}]\n", "tasks[0].label", "its place in the contest's list"),
        ("tasks: [{id: a, points: 100}]\n", "tasks[0].points", "no longer gives points"),
        ("tasks: [{id: a, worth: 100}]\n", "tasks[0].worth", "no longer gives points"),
        ("tasks: [{id: a, late_per_day: 0.1}]\n", "tasks[0].late_per_day", "reads seconds_late"),
        ("leaderboards: [{name: B}]\n", "leaderboards", "keyed by name"),
        ("leaderboards: {b: {name: B}}\n", "leaderboards.b.name", "written as blocks"),
        ("leaderboards: {b: {select: best}}\n", "leaderboards.b.select", "written as blocks"),
        ("leaderboards: {b: {order: [points]}}\n", "leaderboards.b.order", "written as blocks"),
        ("leaderboards: {b: {who: everyone}}\n", "leaderboards.b.who", "written as blocks"),
        ("leaderboards: {b: {rows: all}}\n", "leaderboards.b.rows", "written as blocks"),
        (
            "leaderboards: {b: {freeze_at: 2026-06-01T13:00:00Z}}\n",
            "leaderboards.b.freeze_at",
            "written as blocks",
        ),
        ("freeze_at: 2026-06-01T13:00:00Z\n", "freeze_at", "give the contest a hold"),
    ],
)
def test_a_retired_contest_key_is_refused_at_its_path_with_what_replaced_it(
    extra: str, path: str, sentence: str
) -> None:
    problems = contest_problems(HEAD + extra)

    assert paths(problems) == [path]
    assert sentence in problems[0]["message"]


@pytest.mark.parametrize("value", ["last_result", "grading"])
def test_a_stored_contest_still_holding_on_system_error_reads_with_it_ignored(value: str) -> None:
    text = HEAD + f"on_system_error: {value}\n"

    assert parse_contest(text) == parse_contest(HEAD)
    assert paths(contest_problems(text)) == ["on_system_error"]


def test_a_stored_contest_whose_tasks_still_give_worth_reads_with_it_ignored() -> None:
    text = (
        HEAD
        + "tasks:\n"
        + "  - {id: a, worth: 100}\n"
        + "  - {id: b, due: 2026-06-01T12:00:00Z, late_per_day: 0.1}\n"
    )

    assert parse_contest(text) == parse_contest(
        HEAD + "tasks: [{id: a}, {id: b, due: 2026-06-01T12:00:00Z}]\n"
    )
    assert paths(contest_problems(text)) == ["tasks[0].worth", "tasks[1].late_per_day"]


def test_a_stored_contest_with_old_leaderboards_or_freeze_at_reads_with_no_leaderboards() -> None:
    text = (
        HEAD
        + "freeze_at: 2026-06-01T13:00:00Z\n"
        + "leaderboards:\n  - {name: Standings, who: contestants, select: best}\n"
    )

    assert parse_contest(text) == parse_contest(HEAD)
    assert paths(contest_problems(text)) == ["leaderboards", "freeze_at"]


def test_public_visibility_is_refused_naming_everyone() -> None:
    problems = contest_problems(HEAD.replace("everyone", "public"))

    assert problems == [
        {"path": "visibility", "message": 'This is now called "everyone". Choose that instead.'}
    ]


def test_every_retired_key_in_one_file_is_named_together() -> None:
    problems = contest_problems(
        HEAD.replace("everyone", "public")
        + "submissions_closed: false\n"
        + "tasks: [{id: a, label: A, points: 100}]\n"
    )

    assert paths(problems) == [
        "submissions_closed",
        "visibility",
        "tasks[0].label",
        "tasks[0].points",
    ]


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ("tasks: [{id: a}, {id: a}]\n", "tasks[1].id"),
        ("leaderboards: {b: {audience: {who: public}}}\n", "leaderboards.b.audience.who"),
        ("leaderboards: {b: {audience: {rows: 0}}}\n", "leaderboards.b.audience.rows"),
        ("leaderboards: {Big: {}}\n", "leaderboards.Big"),
        ("hold: {from: 2026-06-01T13:00:00Z, until: never}\n", "hold.until"),
        ("tasks: [{id: Big-Task}]\n", "tasks[0].id"),
        ("tasks: [{id: abcdefghijklmnopqrstuvwxy}]\n", "tasks[0].id"),
        ("entrants: {most: 0}\n", "entrants.most"),
        ("entrants: {fewest: 0}\n", "entrants.fewest"),
        ("entrants: {most: 2, fewest: 3}\n", "entrants.fewest"),
        ("entrants: {locks: never}\n", "entrants.locks"),
        ("entrants: {most: '3'}\n", "entrants.most"),
        ("registration: {capacity: 0}\n", "registration.capacity"),
        (
            "registration: {ways: [{signup: {email_pattern: '(unclosed'}}]}\n",
            "registration.ways[0].signup.email_pattern",
        ),
        (
            "registration: {ways: [{signup: {approval: sometimes}}]}\n",
            "registration.ways[0].signup.approval",
        ),
        (
            "registration: {ways: [{invite: {approval: never}}]}\n",
            "registration.ways[0].invite.approval",
        ),
        ("registration: {ways: [{invite: {code: x}}]}\n", "registration.ways[0].invite.code"),
        (
            "registration: {ways: [{signup: {domains: []}}]}\n",
            "registration.ways[0].signup.domains",
        ),
        (
            "registration: {ways: [{signup: {domains: [U.NUS.EDU]}}]}\n",
            "registration.ways[0].signup.domains[0]",
        ),
        (
            "registration: {ways: [{signup: {domains: [localhost]}}]}\n",
            "registration.ways[0].signup.domains[0]",
        ),
        (
            "registration: {ways: [{signup: {domains: [-u.nus.edu]}}]}\n",
            "registration.ways[0].signup.domains[0]",
        ),
        (
            "registration: {ways: [{signup: {domains: [u.nus.edu, u.nus.edu]}}]}\n",
            "registration.ways[0].signup.domains[1]",
        ),
        (
            "registration: {ways: [{signup: {domains: ['*.nus.edu.sg', '*.nus.edu.sg']}}]}\n",
            "registration.ways[0].signup.domains[1]",
        ),
        ("registration: {ways: [{signup: {name: ''}}]}\n", "registration.ways[0].signup.name"),
        ("registration: {ways: [{}]}\n", "registration.ways[0]"),
        ("registration: {ways: [{invite: {}, signup: {}}]}\n", "registration.ways[0]"),
        ("registration: {ways: [invite]}\n", "registration.ways[0]"),
        ("registration: {ways: [{door: {}}]}\n", "registration.ways[0].door"),
        ("registration: {ways: [{invite: {}}, {invite: {}}]}\n", "registration.ways[1].invite"),
        (
            "registration: {ways: [{signup: {name: A}}, {signup: {name: A, code: x}}]}\n",
            "registration.ways[1].signup.name",
        ),
        (
            "registration: {opens: 2026-05-02T00:00:00Z, closes: 2026-05-01T00:00:00Z}\n",
            "registration.closes",
        ),
        ("colour: red\n", "colour"),
    ],
)
def test_a_malformed_contest_is_refused_at_the_path_that_is_wrong(extra: str, path: str) -> None:
    assert path in paths(contest_problems(HEAD + extra))


@pytest.mark.parametrize(
    "domain", ["'*nus.edu.sg'", "'a.*.nus.edu.sg'", "'*.*.nus.edu.sg'", "'*.sg'", "'*'", "'*.'"]
)
def test_star_stands_only_as_a_domains_whole_first_label(domain: str) -> None:
    problems = contest_problems(
        HEAD + f"registration: {{ways: [{{signup: {{domains: [{domain}]}}}}]}}\n"
    )

    assert at(problems, "registration.ways[0].signup.domains[0]") == (
        "Give a domain such as u.nus.edu, or *.nus.edu.sg to take nus.edu.sg and every domain "
        "under it: lower case letters, digits, hyphens and dots, with at least one dot, no dot "
        "or hyphen at either end, and * only as the whole first part."
    )


def test_a_domain_and_every_domain_under_it_is_written_with_star() -> None:
    contest = parse_contest(
        HEAD + "registration: {ways: [{signup: {domains: ['*.nus.edu.sg', u.nus.edu]}}]}\n",
        saved=True,
    )

    (way,) = contest.registration.ways
    assert way.signup is not None
    assert way.signup.domains == ("*.nus.edu.sg", "u.nus.edu")


def test_a_contest_ends_after_it_starts_and_its_times_carry_a_timezone() -> None:
    assert paths(contest_problems(HEAD.replace("14:00:00Z", "09:00:00Z"))) == ["end"]
    bare = contest_problems(HEAD.replace("2026-06-01T09:00:00Z", "2026-06-01"))
    assert at(bare, "start").startswith("Give a time and a time zone as well as the date")
    naive = contest_problems(HEAD.replace("2026-06-01T09:00:00Z", "'2026-06-01T09:00:00'"))
    assert at(naive, "start").startswith("Give the time zone with the time")


def test_the_contest_admin_keys_leave_start_and_end_to_the_managers() -> None:
    assert CONTEST_ADMIN_KEYS == ("name", "description", "state", "visibility", "registration")
    assert "start" not in CONTEST_ADMIN_KEYS and "end" not in CONTEST_ADMIN_KEYS


# C1 within contest.yaml


def entry_problems(entry: str, leaderboards: str = "") -> list[Problem]:
    return contest_problems(HEAD + f"tasks: [{entry}]\n" + leaderboards)


def test_a_full_timeline_in_order_is_valid() -> None:
    contest = parse_contest(
        HEAD
        + "tasks:\n"
        + "  - id: a\n"
        + "    release_at: 2026-06-01T09:00:00Z\n"
        + "    due: 2026-06-01T12:00:00Z\n"
        + "    closes: 2026-06-01T14:00:00Z\n"
        + "    marks: 10\n"
    )

    entry = contest.tasks[0]
    assert (entry.due, entry.marks) == (datetime(2026, 6, 1, 12, tzinfo=UTC), 10)


def test_equal_times_are_in_order_and_a_task_may_never_take_a_submission() -> None:
    parse_contest(
        HEAD + "tasks: [{id: a, release_at: 2026-06-01T14:00:00Z, closes: 2026-06-01T14:00:00Z}]\n"
    )


@pytest.mark.parametrize(
    ("entry", "path", "sentence"),
    [
        (
            "{id: a, release_at: 2026-06-01T08:59:59Z}",
            "tasks[0].release_at",
            "The release time is before the contest's start.",
        ),
        (
            "{id: a, release_at: 2026-06-01T11:00:00Z, due: 2026-06-01T10:00:00Z}",
            "tasks[0].due",
            "The due time is before the release time.",
        ),
        (
            "{id: a, due: 2026-06-01T13:00:00Z, closes: 2026-06-01T12:00:00Z}",
            "tasks[0].closes",
            "The closing time is before the due time.",
        ),
        (
            "{id: a, due: 2026-06-01T14:30:00Z}",
            "tasks[0].due",
            "The due time is after the contest's end.",
        ),
        (
            "{id: a, closes: 2026-06-01T14:00:01Z}",
            "tasks[0].closes",
            "The closing time is after the contest's end.",
        ),
        (
            "{id: a, release_at: 2026-06-01T14:30:00Z}",
            "tasks[0].release_at",
            "The release time is after the contest's end.",
        ),
        (
            "{id: a, due: 2026-06-01T08:00:00Z}",
            "tasks[0].due",
            "The due time is before the contest's start.",
        ),
    ],
)
def test_an_entry_whose_times_are_out_of_order_is_refused_at_the_late_one(
    entry: str, path: str, sentence: str
) -> None:
    assert sentence in at(entry_problems(entry), path)


def test_a_release_after_a_close_is_refused_naming_the_close() -> None:
    problems = entry_problems(
        "{id: a, release_at: 2026-06-01T12:00:00Z, closes: 2026-06-01T11:00:00Z}"
    )

    assert "The closing time is before the release time." in at(problems, "tasks[0].closes")


@pytest.mark.parametrize(
    ("marks", "sentence"), [("0", "This must be at least 1."), ("11", "This must be at most 10.")]
)
def test_marks_are_1_to_10(marks: str, sentence: str) -> None:
    problems = entry_problems(f"{{id: a, marks: {marks}}}")

    assert at(problems, "tasks[0].marks") == sentence


# task.yaml


def test_the_format_documents_example_task_parses() -> None:
    task = parse_task(TASK_EXAMPLE)

    assert task.workflow == WorkflowRef("unicon", "checked", "v1")
    assert task.inputs["checker"] == "checker/checker.cpp"
    assert task.inputs["code"] == {"entrant": {"label": "Your solution", "max_size": "1MB"}}
    assert [axis.inputs for axis in task.repeat] == [["input", "answer"]]
    assert task.scoring.groups == {"samples": {"input": ["samples/1.in"]}}
    assert list(task.scoring.values) == ["score"]
    assert (task.scoring.show.now, task.scoring.show.after_close) == (["samples"], ["score"])
    assert (task.submissions.max, task.submissions.rate.count) == (50, 1)


def test_a_task_leaves_inputs_and_submissions_to_their_defaults() -> None:
    task = parse_task(task_text())

    assert task.inputs == {}
    assert task.submissions.max == DEFAULT_SUBMISSIONS == 50
    assert task.submissions.rate == DEFAULT_RATE
    assert (task.submissions.rate.count, task.submissions.rate.per) == (1, 30)
    assert task.submissions.rate.window == timedelta(seconds=30)


def test_submissions_take_a_count_and_a_rate_of_their_own() -> None:
    task = parse_task(task_text("submissions: {max: 5, rate: {count: 2, per: 60}}\n"))

    assert (task.submissions.max, task.submissions.rate.count, task.submissions.rate.per) == (
        5,
        2,
        60,
    )


@pytest.mark.parametrize(
    ("extra", "path", "sentence"),
    [
        ("test_groups: {main: {each: 1}}\n", "test_groups", "scoring's groups"),
        ("credit: fraction\n", "credit", "scoring's values"),
        ("release_at: 2026-06-01T10:00:00Z\n", "release_at", "on the task's timeline"),
        ("hidden: true\n", "hidden", "scoring's show after_close"),
        ("limits: {submissions: 50}\n", "limits", "the most submissions"),
        ("subtasks: [{id: s1}]\n", "subtasks", "groups of runs"),
        ("stages: [{id: final}]\n", "stages", "set on each leaderboard"),
        ("inputs: {contestant: []}\n", "inputs.contestant", "List each input"),
        ("inputs: {setter: []}\n", "inputs.setter", "List each input"),
        ("submissions: {rate: 1 per 30s}\n", "submissions.rate", "1 every 30 seconds"),
    ],
)
def test_a_retired_task_key_is_refused_at_its_path_with_what_replaced_it(
    extra: str, path: str, sentence: str
) -> None:
    problems = task_problems(task_text(extra))

    assert paths(problems) == [path]
    assert sentence in problems[0]["message"]


def test_an_input_named_contestant_in_the_new_mapping_is_still_the_old_shape() -> None:
    # `inputs` is keyed by the workflow's input ids; `contestant` and `setter`
    # were the old two lists, so either key is refused whatever it holds.
    problems = task_problems(task_text("inputs: {setter: 3}\n"))

    assert paths(problems) == ["inputs.setter"]


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("workflow: unicon/classic@v3\nscoring: {}\n", "name"),
        ("name: T\nscoring: {}\n", "workflow"),
        ("name: T\nworkflow: unicon/classic@v3\n", "scoring"),
        ("name: '  '\nworkflow: unicon/classic@v3\nscoring: {}\n", "name"),
        ("name: T\nworkflow: unicon/classic\nscoring: {}\n", "workflow"),
        (task_text("colour: red\n"), "colour"),
        (task_text("submissions: {max: 0}\n"), "submissions.max"),
        (task_text("submissions: {rate: {count: 1, per: 0}}\n"), "submissions.rate.per"),
        (task_text("submissions: {rate: {count: 1}}\n"), "submissions.rate.per"),
    ],
)
def test_a_malformed_task_is_refused_at_the_path_that_is_wrong(text: str, path: str) -> None:
    assert path in paths(task_problems(text))


def test_a_typo_says_so_and_a_key_given_twice_is_refused() -> None:
    problems = task_problems(task_text("submision: {max: 3}\n"))
    assert problems == [
        {
            "path": "submision",
            "message": "Unicon does not know this setting. Check its spelling, or remove it.",
        }
    ]

    twice = task_problems(task_text() + "name: U\n")
    assert paths(twice) == [""]
    assert "the setting 'name' is given twice" in twice[0]["message"]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [(b"- a\n- b\n", "write them as named settings"), (b"name: [\n", "a mistake on line")],
)
def test_a_file_that_is_not_a_mapping_is_one_problem_at_the_top(text: bytes, fragment: str) -> None:
    with pytest.raises(InvalidDefinition) as refused:
        parse_task(text)
    assert paths(refused.value.errors) == [""]
    assert fragment in refused.value.errors[0]["message"]
    assert isinstance(refused.value, ServiceError)


def test_every_problem_in_one_task_is_reported_together() -> None:
    problems = task_problems(task_text("hidden: false\nsubmissions: {max: 0}\n"))

    assert sorted(paths(problems)) == ["hidden", "submissions.max"]


# Form details


def test_form_details_fit_the_inputs_type_and_the_workflows_options() -> None:
    options = ("c", "cpp", "python")

    assert form_problems({"options": ["cpp"], "default": "cpp"}, "enum", options) == []
    assert form_problems({"options": ["rust"]}, "enum", options) == [
        (("options",), "rust is not one of the workflow's options, which are c, cpp, python.")
    ]
    assert form_problems({"options": ["cpp"], "default": "c"}, "enum", options) == [
        (("default",), "The default must be one of cpp.")
    ]
    assert form_problems({"min": 2, "max": 1}, "number", None) == [
        (("max",), "The maximum must be at least the minimum.")
    ]
    assert form_problems({"min": 0, "max": 1, "default": 2}, "number", None) == [
        (("default",), "The default must be at most the maximum.")
    ]
    assert form_problems({"default": "yes"}, "boolean", None) == [
        (("default",), "The default must be true or false.")
    ]
    assert form_problems({"max_size": "3GB"}, "file", None) == [
        (("max_size",), "This must be at most 2GB, the largest file Unicon takes.")
    ]
    assert form_problems({"max_size": "2GB", "label": "Data"}, "folder", None) == []
    assert form_problems({"max_size": "1MB"}, "text", None) == [
        (("max_size",), "A text input does not take a largest size. Remove it.")
    ]
    assert form_problems({"colour": "red"}, "text", None) == [
        (("colour",), "Unicon does not know this setting. Check its spelling, or remove it.")
    ]
    assert parse_size("2GB") == 2 * 1024**3


# The starters, and the admin's keys


def test_the_starter_contest_is_the_formats_and_starts_on_the_hour_a_week_on() -> None:
    contest = parse_contest(starter_contest('Spring: "open"\n round', NOW))

    assert contest.name == 'Spring: "open" round'
    assert (contest.state, contest.visibility) == (State.DRAFT, ContestVisibility.SIGNED_IN)
    assert contest.start == datetime(2026, 10, 6, 11, tzinfo=UTC)
    assert contest.end - contest.start == timedelta(hours=5)
    assert contest.description is None and contest.entrants == Entrants()
    assert contest.registration == Registration()
    assert contest.registration.ways[0].approval is Approval.MANUAL
    assert contest.leaderboards == {}
    assert contest.tasks == ()
    assert b"tasks" not in starter_contest("x", NOW)


def test_the_starter_contest_on_the_hour_starts_exactly_a_week_on() -> None:
    on_the_hour = datetime(2026, 9, 29, 10, tzinfo=UTC)
    assert parse_contest(starter_contest("x", on_the_hour)).start == on_the_hour + timedelta(days=7)


def test_the_starter_task_is_the_formats() -> None:
    files = starter_task("Sum of two")
    task = parse_task(files["task.yaml"])

    assert task.name == "Sum of two"
    assert task.workflow == WorkflowRef("unicon", "classic", "v3")
    assert task.inputs == {
        "code": {"entrant": {"label": "Your solution"}},
        "language": {"entrant": {"label": "Language", "options": ["python"]}},
        "time_limit": 2,
        "memory_limit": 256,
    }
    assert files["statement.md"] == b"Write the statement entrants read here.\n"
    assert [axis.rows for axis in task.repeat] == [[("tests/1.in", "tests/1.ans")]]
    assert list(task.scoring.values) == ["score"]
    assert task.scoring.show.now == ["score", "all", "public/"]
    assert set(files) == {"task.yaml", "statement.md", "tests/1.in", "tests/1.ans"}
    assert files["tests/1.in"] == b"1 2\n"
    assert files["tests/1.ans"] == b"3\n"


def test_a_title_is_the_one_asked_for_or_the_name() -> None:
    assert title_of("Sum of Two", "sum") == "Sum of Two"
    assert title_of("   ", "sum") == "sum"
    assert title_of(None, "sum") == "sum"


def test_the_task_admin_keys_are_the_name_and_the_submissions() -> None:
    assert TASK_ADMIN_KEYS == ("name", "submissions")
    before = task_text().encode()
    after = before.replace(b"name: T", b"name: U") + b"submissions: {max: 3}\n"

    assert admin_only_changes("task", before, after) == ["name", "submissions"]
    managers = before.replace(b"scoring: {}", b"scoring: {values: {}}") + b"inputs: {x: 1}\n"
    assert admin_only_changes("task", before, managers) == []


def contest_yaml(**changes: Any) -> bytes:
    document = load_yaml(CONTEST_EXAMPLE)
    assert isinstance(document, dict)
    document.update(changes)
    return one_line(document).encode()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("name", "Renamed"),
        ("description", "Another paragraph"),
        ("state", "archived"),
        ("visibility", "hidden"),
        ("registration", {"ways": [{"invite": {}}]}),
    ],
)
def test_changing_an_admin_only_contest_key_is_named(key: str, value: Any) -> None:
    assert admin_only_changes("contest", CONTEST_EXAMPLE, contest_yaml(**{key: value})) == [key]


def test_the_managers_contest_keys_change_nothing_admin_only() -> None:
    changed = contest_yaml(
        start="2026-06-01T08:00:00Z",
        end="2026-06-01T15:00:00Z",
        entrants={"most": 4, "locks": "first_submission"},
        leaderboards=[],
        tasks=[{"id": "sum-of-two"}],
    )

    assert admin_only_changes("contest", CONTEST_EXAMPLE, changed) == []


def test_the_same_settings_written_differently_change_nothing() -> None:
    assert admin_only_changes("contest", CONTEST_EXAMPLE, contest_yaml()) == []


def test_a_time_written_another_way_is_the_same_time() -> None:
    other_zone = CONTEST_EXAMPLE.replace(
        b"opens: 2026-05-01T00:00:00Z", b'opens: "2026-05-01T08:00:00+08:00"'
    )
    later = CONTEST_EXAMPLE.replace(b"opens: 2026-05-01T00:00:00Z", b"opens: 2026-05-01T00:00:01Z")

    assert admin_only_changes("contest", CONTEST_EXAMPLE, other_zone) == []
    assert admin_only_changes("contest", CONTEST_EXAMPLE, later) == ["registration"]


def test_a_new_file_names_every_admin_key_it_sets_and_an_unparsable_side_compares_as_empty() -> (
    None
):
    assert admin_only_changes("contest", None, CONTEST_EXAMPLE) == list(CONTEST_ADMIN_KEYS)
    assert admin_only_changes("task", b"name: [\n", b"name: T\n") == ["name"]


def test_a_way_written_bare_takes_its_defaults_and_none_at_all_lets_nobody_in() -> None:
    contest = parse_contest(HEAD + "registration:\n  ways:\n    - invite:\n    - signup:\n")
    nobody = parse_contest(HEAD + "registration: {ways: []}\n")

    assert [way.approval for way in contest.registration.ways] == [Approval.AUTO, Approval.MANUAL]
    assert nobody.registration.ways == ()
