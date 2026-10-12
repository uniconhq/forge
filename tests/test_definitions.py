"""The two definition files, `contest.yaml` and `task.yaml`: the format's own
examples and the starters validate, every key the format no longer has is
refused at that key with the sentence that says what replaced it, the checks
each file makes of itself (a task entry's timeline, C1; a board's rules, C3;
a test group's rule weights, test weights and `show`, T1, T2 and T6; credit
on a task that gives no points, T3) refuse at the path that is wrong, and a
save names the admin-only keys it changes.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    Over,
    Penalty,
    Registration,
    Relative,
    Select,
    Show,
    SignupWay,
    State,
    Way,
    Who,
    admin_only_changes,
    form_problems,
    letters,
    parse_contest,
    parse_task,
    starter_contest,
    starter_task,
    title_of,
    worth_of,
)
from forge.domain.errors import ServiceError
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
  - name: Standings
    tasks: [sum-of-two, shortest]
    over: all
    select: best
    order: [points, {by: penalty, per_attempt: 20}]
    who: contestants
    rows: all
  - name: Final
    tasks: [shortest]
    over: after_close
    select: marked
    who: contestants
tasks:
  - {id: sum-of-two, worth: 100}
  - id: shortest
    worth: 100
    release_at: 2026-06-01T10:00:00Z
    due: 2026-06-01T12:00:00Z
    late_per_day: 0.1
    closes: 2026-06-01T13:00:00Z
    marks: 2
"""

TASK_EXAMPLE = b"""\
name: Shortest Path
workflow: unicon/checked@v1
inputs:
  submission: {label: Your solution, max_size: 1MB}
  language: {options: [cpp, python], default: cpp}
  checker: checker/checker.cpp
  checker_language: cpp
  time_limit: 2
  memory_limit: 256
credit: fraction
test_groups:
  samples: {}
  small: {pass: 30, show: verdict}
  large: {worst: 70, show: after_close}
  main:
    each: 80
    pass: 20
    pass_at: 0.8
    test_weights: {"7": 3, "8": 3, "9": 5}
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


def task_text(groups: str, *, credit: str | None = None, extra: str = "") -> str:
    text = f"name: T\nworkflow: unicon/classic@v2\ntest_groups: {groups}\n"
    if credit is not None:
        text += f"credit: {credit}\n"
    return text + extra


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
    standings, final = contest.leaderboards
    assert standings.order == ("points", Penalty(by="penalty", per_attempt=20))
    assert (standings.who, standings.rows) == (Who.CONTESTANTS, "all")
    assert (final.over, final.select, final.order) == (Over.AFTER_CLOSE, Select.MARKED, ("points",))
    shortest = contest.tasks[1]
    assert shortest.late_per_day == Decimal("0.1")
    assert contest.marks_of(shortest) == 2
    assert contest.marks_of(contest.tasks[0]) is None
    assert [contest.label_of(task.id) for task in contest.tasks] == ["A", "B"]


def test_a_contest_leaves_the_optional_blocks_to_their_defaults() -> None:
    contest = parse_contest(HEAD + "leaderboards: [{name: Standings}]\ntasks: [{id: a}]\n")

    assert contest.description is None
    assert contest.entrants == Entrants(most=1, fewest=1, locks=Locks.START)
    assert contest.registration.ways == (Way(signup=SignupWay(approval=Approval.MANUAL)),)
    assert contest.registration.invite_way is None
    assert contest.registration.capacity is None
    board = contest.leaderboards[0]
    assert (board.tasks, board.over, board.select, board.order, board.who, board.rows) == (
        None,
        Over.ALL,
        Select.BEST,
        ("points",),
        Who.ORGANISERS,
        "all",
    )
    entry = contest.tasks[0]
    assert (entry.worth, entry.release_at, entry.due, entry.late_per_day, entry.closes) == (
        None,
        None,
        None,
        None,
        None,
    )
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
        ("tasks: [{id: a, points: 100}]\n", "tasks[0].points", "This is now called worth"),
        (
            "leaderboards: [{name: B, stage: default}]\n",
            "leaderboards[0].stage",
            "counts test groups",
        ),
        (
            "leaderboards: [{name: B, combine: sum}]\n",
            "leaderboards[0].combine",
            "adds up its tasks",
        ),
        (
            "leaderboards: [{name: B, visibility: public}]\n",
            "leaderboards[0].visibility",
            "who the leaderboard is shown to",
        ),
        (
            "leaderboards: [{name: B, freeze_at: 2026-06-01T13:00:00Z}]\n",
            "leaderboards[0].freeze_at",
            "no longer freeze",
        ),
        (
            "leaderboards: [{name: B, team_only: true}]\n",
            "leaderboards[0].team_only",
            "the contest's entrants",
        ),
        ("leaderboards: [{name: B, select: latest}]\n", "leaderboards[0].select", "on each group"),
        ("leaderboards: [{name: B, select: selected}]\n", "leaderboards[0].select", "marks"),
        (
            "leaderboards: [{name: B, select: first_accepted}]\n",
            "leaderboards[0].select",
            "the best submission, the best submission on each group, or the best of the",
        ),
        (
            "leaderboards: [{name: B, order: [{metric: points, direction: desc}]}]\n",
            "leaderboards[0].order[0]",
            "set by its workflow",
        ),
        (
            "leaderboards: [{name: B, order: [points, {key: penalty, per_rejected: 20}]}]\n",
            "leaderboards[0].order[1]",
            "penalty minutes",
        ),
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
    ("order", "path", "sentence"),
    [
        ("[penalty]", "leaderboards[0].order[0]", "Penalty minutes cannot come first"),
        (
            "[{by: penalty}, points]",
            "leaderboards[0].order[0]",
            "Penalty minutes cannot come first",
        ),
        ("[points, points]", "leaderboards[0].order[1]", "already ranks by points above."),
        (
            "[points, penalty, {by: penalty, per_attempt: 5}]",
            "leaderboards[0].order[2]",
            "already ranks by penalty above.",
        ),
        ("[time_ms, time_ms]", "leaderboards[0].order[1]", "already ranks by time_ms above."),
        ("[]", "leaderboards[0].order", "Give at least one."),
        ("[Points]", "leaderboards[0].order[0]", "by a value the tasks report"),
        (
            "[{by: penalty, per_attempt: -1}]",
            "leaderboards[0].order[0].per_attempt",
            "This must be at least 0.",
        ),
    ],
)
def test_a_boards_order_breaking_c3_is_refused_at_the_key(
    order: str, path: str, sentence: str
) -> None:
    problems = contest_problems(HEAD + f"leaderboards: [{{name: B, order: {order}}}]\n")

    assert sentence in at(problems, path)


def test_penalty_ranks_after_points_or_a_value() -> None:
    contest = parse_contest(
        HEAD
        + "leaderboards:\n"
        + "  - {name: A, order: [points, penalty]}\n"
        + "  - {name: B, order: [time_ms, {by: penalty, per_attempt: 0}]}\n"
    )

    assert contest.leaderboards[1].order == ("time_ms", Penalty(by="penalty", per_attempt=0))


def test_best_per_group_ranks_points_first_and_no_value() -> None:
    value_first = contest_problems(
        HEAD + "leaderboards: [{name: B, select: best_per_group, order: [time_ms]}]\n"
    )
    with_value = contest_problems(
        HEAD + "leaderboards: [{name: B, select: best_per_group, order: [points, time_ms]}]\n"
    )

    assert (
        "Counting the best submission on each group ranks by points first. Put points first, "
        "or count another submission."
    ) in [problem["message"] for problem in value_first]
    assert at(with_value, "leaderboards[0].select") == (
        "The best submission on each group adds up groups from different submissions, so it "
        "cannot rank by time_ms, which belongs to one submission. Count another submission, or "
        "stop ranking by time_ms."
    )
    parse_contest(
        HEAD + "leaderboards: [{name: B, select: best_per_group, order: [points, penalty]}]\n"
    )


def test_own_rows_are_refused_on_a_board_everyone_sees() -> None:
    problems = contest_problems(HEAD + "leaderboards: [{name: B, rows: own, who: everyone}]\n")

    assert at(problems, "leaderboards[0].rows") == (
        "A leaderboard shown to everyone cannot show each entrant only their own row, since a "
        "guest has none. Show every row or the top rows."
    )
    assert parse_contest(HEAD + "leaderboards: [{name: B, rows: own, who: contestants}]\n")
    assert parse_contest(HEAD + "leaderboards: [{name: B, rows: 10, who: everyone}]\n")


@pytest.mark.parametrize("rows", ["0", "some", "true", "-3"])
def test_rows_is_all_own_or_a_count_of_at_least_one(rows: str) -> None:
    problems = contest_problems(HEAD + f"leaderboards: [{{name: B, rows: {rows}}}]\n")

    assert paths(problems) == ["leaderboards[0].rows"]


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ("leaderboards: [{name: B}, {name: B}]\n", "leaderboards[1].name"),
        ("tasks: [{id: a}, {id: a}]\n", "tasks[1].id"),
        (
            "tasks: [{id: a}]\nleaderboards: [{name: B, tasks: [a, b]}]\n",
            "leaderboards[0].tasks[1]",
        ),
        ("leaderboards: [{name: B, over: live_only}]\n", "leaderboards[0].over"),
        ("leaderboards: [{name: B, who: public}]\n", "leaderboards[0].who"),
        ("tasks: [{id: Big-Task}]\n", "tasks[0].id"),
        ("tasks: [{id: abcdefghijklmnopqrstuvwxy}]\n", "tasks[0].id"),
        ("tasks: [{id: a, worth: -1}]\n", "tasks[0].worth"),
        ("tasks: [{id: a, worth: true}]\n", "tasks[0].worth"),
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


def entry_problems(entry: str, boards: str = "") -> list[Problem]:
    return contest_problems(HEAD + f"tasks: [{entry}]\n" + boards)


def test_a_full_timeline_in_order_is_valid() -> None:
    contest = parse_contest(
        HEAD
        + "tasks:\n"
        + "  - id: a\n"
        + "    worth: 50\n"
        + "    release_at: 2026-06-01T09:00:00Z\n"
        + "    due: 2026-06-01T12:00:00Z\n"
        + "    late_per_day: 1\n"
        + "    closes: 2026-06-01T14:00:00Z\n"
        + "    marks: 10\n"
        + "leaderboards: [{name: F, select: marked, tasks: [a]}]\n"
    )

    entry = contest.tasks[0]
    assert (entry.worth, entry.late_per_day, entry.marks) == (50, 1, 10)
    assert contest.marks_of(entry) == 10


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


def test_a_late_fraction_needs_a_due() -> None:
    problems = entry_problems("{id: a, late_per_day: 0.5}")

    assert at(problems, "tasks[0].late_per_day") == (
        "This applies only to a task with a due time. Give the task a due time, or leave this "
        "empty."
    )


@pytest.mark.parametrize("fraction", ["0", "-0.1", "1.5"])
def test_a_late_fraction_is_more_than_0_and_at_most_1(fraction: str) -> None:
    problems = entry_problems(f"{{id: a, due: 2026-06-01T12:00:00Z, late_per_day: {fraction}}}")

    assert at(problems, "tasks[0].late_per_day") == (
        "This must be more than 0 and at most 1: the share of the points taken off for each "
        "late day."
    )


def test_marks_need_a_marked_board_covering_the_task() -> None:
    nothing_marked = entry_problems("{id: a, marks: 2}", "leaderboards: [{name: B}]\n")
    other_task = contest_problems(
        HEAD
        + "tasks: [{id: a, marks: 2}, {id: b}]\n"
        + "leaderboards: [{name: F, select: marked, tasks: [b]}]\n"
    )

    for problems in (nothing_marked, other_task):
        assert at(problems, "tasks[0].marks") == (
            "This applies only when a leaderboard covering this task counts the submissions an "
            "entrant marks."
        )
    covering = parse_contest(HEAD + "tasks: [{id: a}]\nleaderboards: [{name: F, select: marked}]\n")
    assert covering.marks_of(covering.tasks[0]) == 1


@pytest.mark.parametrize(
    ("marks", "sentence"), [("0", "This must be at least 1."), ("11", "This must be at most 10.")]
)
def test_marks_are_1_to_10(marks: str, sentence: str) -> None:
    problems = entry_problems(
        f"{{id: a, marks: {marks}}}", "leaderboards: [{name: F, select: marked}]\n"
    )

    assert at(problems, "tasks[0].marks") == sentence


# task.yaml


def test_the_format_documents_example_task_parses() -> None:
    task = parse_task(TASK_EXAMPLE)

    assert task.workflow == WorkflowRef("unicon", "checked", "v1")
    assert task.credit == "fraction"
    assert task.inputs["checker"] == "checker/checker.cpp"
    assert task.inputs["submission"] == {"label": "Your solution", "max_size": "1MB"}
    assert list(task.test_groups) == ["samples", "small", "large", "main"]
    samples, small, large, main = task.test_groups.values()
    assert (samples.weight, samples.shown) == (0, Show.ALWAYS)
    assert (small.pass_, small.shown) == (30, Show.VERDICT)
    assert (large.worst, large.shown) == (70, Show.AFTER_CLOSE)
    assert (main.each, main.pass_, main.pass_at, main.weight) == (80, 20, Decimal("0.8"), 100)
    assert main.test_weights == {"7": 3, "8": 3, "9": 5}
    assert task.gives_points


def test_a_task_leaves_inputs_credit_and_submissions_to_their_defaults() -> None:
    task = parse_task(task_text("{main: {each: 100}}"))

    assert task.inputs == {}
    assert task.credit is None
    assert task.submissions.max == DEFAULT_SUBMISSIONS == 50
    assert task.submissions.rate == DEFAULT_RATE
    assert (task.submissions.rate.count, task.submissions.rate.per) == (1, 30)
    assert task.submissions.rate.window == timedelta(seconds=30)
    assert task.test_groups["main"].show is None
    assert task.test_groups["main"].shown is Show.ALWAYS


def test_submissions_take_a_count_and_a_rate_of_their_own() -> None:
    task = parse_task(
        task_text("{main: {each: 1}}", extra="submissions: {max: 5, rate: {count: 2, per: 60}}\n")
    )

    assert (task.submissions.max, task.submissions.rate.count, task.submissions.rate.per) == (
        5,
        2,
        60,
    )


@pytest.mark.parametrize(
    ("extra", "path", "sentence"),
    [
        ("release_at: 2026-06-01T10:00:00Z\n", "release_at", "on the task's timeline"),
        ("hidden: true\n", "hidden", "When entrants see results"),
        ("limits: {submissions: 50}\n", "limits", "the most submissions"),
        ("subtasks: [{id: s1}]\n", "subtasks", "test groups"),
        ("stages: [{id: final}]\n", "stages", "set on each leaderboard"),
        ("inputs: {contestant: []}\n", "inputs.contestant", "List each input"),
        ("inputs: {setter: []}\n", "inputs.setter", "List each input"),
        ("submissions: {rate: 1 per 30s}\n", "submissions.rate", "1 every 30 seconds"),
    ],
)
def test_a_retired_task_key_is_refused_at_its_path_with_what_replaced_it(
    extra: str, path: str, sentence: str
) -> None:
    problems = task_problems(task_text("{main: {each: 1}}", extra=extra))

    assert paths(problems) == [path]
    assert sentence in problems[0]["message"]


def test_an_input_named_contestant_in_the_new_mapping_is_still_the_old_shape() -> None:
    # `inputs` is keyed by the workflow's input ids; `contestant` and `setter`
    # were the old two lists, so either key is refused whatever it holds.
    problems = task_problems(task_text("{main: {each: 1}}", extra="inputs: {setter: 3}\n"))

    assert paths(problems) == ["inputs.setter"]


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("workflow: unicon/classic@v2\ntest_groups: {main: {}}\n", "name"),
        ("name: T\ntest_groups: {main: {}}\n", "workflow"),
        ("name: T\nworkflow: unicon/classic@v2\n", "test_groups"),
        ("name: T\nworkflow: unicon/classic@v2\ntest_groups: {}\n", "test_groups"),
        ("name: '  '\nworkflow: unicon/classic@v2\ntest_groups: {main: {}}\n", "name"),
        ("name: T\nworkflow: unicon/classic\ntest_groups: {main: {}}\n", "workflow"),
        ("name: T\nworkflow: unicon/classic@v2\ntest_groups: {main: {}}\ncolour: red\n", "colour"),
        (task_text("{main: {each: -1}}"), "test_groups.main.each"),
        (task_text("{main: {pass: yes}}"), "test_groups.main.pass"),
        (task_text("{main: {show: hidden}}"), "test_groups.main.show"),
        (task_text("{main: {weight: 3}}"), "test_groups.main.weight"),
        (task_text("{main.1: {each: 1}}"), "test_groups.main.1"),
        (task_text("{main: {each: 1}}", credit="Fraction"), "credit"),
        (task_text("{main: {each: 1}}", credit="{relative: x, better: lower}"), "credit.better"),
        (task_text("{main: {each: 1}}", extra="submissions: {max: 0}\n"), "submissions.max"),
        (
            task_text("{main: {each: 1}}", extra="submissions: {rate: {count: 1, per: 0}}\n"),
            "submissions.rate.per",
        ),
        (
            task_text("{main: {each: 1}}", extra="submissions: {rate: {count: 1}}\n"),
            "submissions.rate.per",
        ),
    ],
)
def test_a_malformed_task_is_refused_at_the_path_that_is_wrong(text: str, path: str) -> None:
    assert path in paths(task_problems(text))


def test_a_typo_says_so_and_a_key_given_twice_is_refused() -> None:
    problems = task_problems(task_text("{main: {each: 1}}", extra="submision: {max: 3}\n"))
    assert problems == [
        {
            "path": "submision",
            "message": "Unicon does not know this setting. Check its spelling, or remove it.",
        }
    ]

    twice = task_problems(task_text("{main: {each: 1}}") + "name: U\n")
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
    problems = task_problems(task_text("{main: {each: -1, show: hidden}}", extra="hidden: false\n"))

    assert sorted(paths(problems)) == [
        "hidden",
        "test_groups.main.each",
        "test_groups.main.show",
    ]


# T1, T2 and T6 within task.yaml


def test_pass_at_applies_only_beside_pass() -> None:
    problems = task_problems(task_text("{main: {each: 1, pass_at: 0.5}}"))

    assert at(problems, "test_groups.main.pass_at") == (
        'This applies only when the group gives points under "at least a share of its tests '
        'must pass". Give those points, or leave this empty.'
    )


@pytest.mark.parametrize("share", ["0", "-0.5", "1.01"])
def test_pass_at_is_more_than_0_and_at_most_1(share: str) -> None:
    problems = task_problems(task_text(f"{{main: {{pass: 1, pass_at: {share}}}}}"))

    assert at(problems, "test_groups.main.pass_at") == (
        "This must be more than 0 and at most 1, such as 0.5 for half the tests."
    )


def test_pass_at_of_exactly_1_is_the_default_written_out() -> None:
    task = parse_task(task_text("{main: {pass: 1, pass_at: 1}}"))

    assert task.test_groups["main"].pass_at == 1


def test_worst_needs_a_credit() -> None:
    problems = task_problems(task_text("{main: {worst: 1}}"))

    assert at(problems, "test_groups.main.worst") == (
        "Each test here either passes or fails, so the lowest test score is the same as "
        '"every test must pass". Give these points there instead.'
    )
    assert parse_task(task_text("{main: {worst: 1}}", credit="fraction"))


@pytest.mark.parametrize(
    "group",
    [
        "{pass: 1, test_weights: {'1': 2}}",
        "{pass: 1, pass_at: 1, test_weights: {'1': 2}}",
        "{worst: 1, test_weights: {'1': 2}}",
        "{test_weights: {'1': 2}}",
    ],
)
def test_test_weights_are_refused_where_nothing_reads_them(group: str) -> None:
    problems = task_problems(task_text(f"{{main: {group}}}", credit="fraction"))

    assert at(problems, "test_groups.main.test_weights") == (
        "The test weights on main change nothing. They count only towards points for each test "
        "passed, or when the share of tests that must pass is below 1."
    )


@pytest.mark.parametrize(
    "group",
    ["{each: 1, test_weights: {'1': 2}}", "{pass: 1, pass_at: 0.5, test_weights: {'1': 2}}"],
)
def test_test_weights_are_read_by_each_and_by_pass_below_all(group: str) -> None:
    task = parse_task(task_text(f"{{main: {group}}}"))

    assert task.test_groups["main"].test_weights == {"1": 2}


@pytest.mark.parametrize("weight", ["0", "-1"])
def test_a_test_weight_is_more_than_0(weight: str) -> None:
    problems = task_problems(task_text(f"{{main: {{each: 1, test_weights: {{'20': {weight}}}}}}}"))

    assert at(problems, "test_groups.main.test_weights.20") == (
        f"main/20 has a weight of {weight}, and a weight must be more than 0. Put a test that "
        "should count for nothing in a test group that gives no points."
    )


def test_a_test_weights_key_is_a_test_name() -> None:
    problems = task_problems(task_text("{main: {each: 1, test_weights: {'a.b': 1}}}"))

    assert at(problems, "test_groups.main.test_weights.a.b") == (
        "A test's name may use only letters, digits, hyphens (-) and underscores (_)."
    )


def test_credit_on_a_task_with_no_rule_weight_is_refused() -> None:
    problems = task_problems(task_text("{samples: {}, main: {each: 0}}", credit="fraction"))

    assert problems == [
        {
            "path": "credit",
            "message": "No test group gives points, so what an accepted test earns changes "
            "nothing. Give a test group points, or let an accepted test earn 1.",
        }
    ]
    assert not parse_task(task_text("{samples: {}}")).gives_points


def test_a_task_entry_with_no_worth_is_worth_its_groups_total() -> None:
    task = parse_task(task_text("{samples: {}, small: {pass: 3}, large: {each: 2, pass: 5}}"))
    contest = parse_contest(HEAD + "tasks: [{id: a}, {id: b, worth: 50}]\n")
    plain, scaled = contest.tasks

    assert task.total_weight == 10
    assert (worth_of(plain, task), worth_of(scaled, task)) == (10, 50)
    assert worth_of(plain, parse_task(task_text("{samples: {}}"))) is None
    assert worth_of(plain, parse_task(task_text("{main: {each: 0.5}}"))) == Decimal("0.5")


def test_a_groups_total_keeps_every_digit_of_its_weights() -> None:
    long = "0.1234567890123456789012345678"
    task = parse_task(task_text(f"{{small: {{pass: {long}, each: {long}}}, large: {{pass: 100}}}}"))

    assert task.test_groups["small"].weight == Decimal("0.2469135780246913578024691356")
    assert task.total_weight == Decimal("100.2469135780246913578024691356")


def test_credit_is_a_value_name_or_relative_to_one() -> None:
    assert parse_task(task_text("{main: {each: 1}}", credit="fraction")).credit == "fraction"
    assert parse_task(task_text("{main: {each: 1}}", credit="{relative: time_ms}")).credit == (
        Relative(relative="time_ms")
    )


EACH_TELLS = (
    "main shows entrants only its outcome until the reveal, but points for each test passed "
    'would tell them how many of its tests passed. Give its points under "every test must pass" '
    'instead, or change "When entrants see results".'
)


@pytest.mark.parametrize(
    ("group", "credit", "sentence"),
    [
        (
            "{each: 1, show: verdict}",
            None,
            EACH_TELLS,
        ),
        (
            "{pass: 1, each: 1, show: verdict}",
            None,
            EACH_TELLS,
        ),
        (
            "{worst: 1, show: verdict}",
            "fraction",
            "main shows entrants only its outcome until the reveal, but points for the lowest "
            "test score would tell them how its worst test did. Give its points under "
            '"every test must pass" instead, or change "When entrants see results".',
        ),
        (
            "{pass: 1, show: verdict}",
            "{relative: time_ms}",
            "This test group shows entrants only its outcome until the reveal, but its points "
            "would change as other entrants do better on its hidden tests. Show its results to "
            "entrants at once or only at the reveal.",
        ),
    ],
)
def test_a_verdict_is_refused_naming_the_rule_weight_that_would_tell(
    group: str, credit: str | None, sentence: str
) -> None:
    problems = task_problems(task_text(f"{{main: {group}}}", credit=credit))

    assert at(problems, "test_groups.main.show") == sentence


@pytest.mark.parametrize(
    ("groups", "credit"),
    [
        ("{main: {pass: 1, show: verdict}}", None),
        ("{main: {pass: 1, pass_at: 0.5, show: verdict}}", None),
        ("{main: {each: 1}, extra: {show: verdict}}", None),
        ("{main: {pass: 1, show: verdict}}", "fraction"),
    ],
)
def test_a_verdict_is_allowed_on_pass_alone_or_on_no_rule_weight(
    groups: str, credit: str | None
) -> None:
    parse_task(task_text(groups, credit=credit))


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
    assert [(board.name, board.who) for board in contest.leaderboards] == [
        ("Standings", Who.CONTESTANTS)
    ]
    assert contest.tasks == ()
    assert b"tasks" not in starter_contest("x", NOW)


def test_the_starter_contest_on_the_hour_starts_exactly_a_week_on() -> None:
    on_the_hour = datetime(2026, 9, 29, 10, tzinfo=UTC)
    assert parse_contest(starter_contest("x", on_the_hour)).start == on_the_hour + timedelta(days=7)


def test_the_starter_task_is_the_formats() -> None:
    files = starter_task("Sum of two")
    task = parse_task(files["task.yaml"])

    assert task.name == "Sum of two"
    assert task.workflow == WorkflowRef("unicon", "classic", "v2")
    assert task.inputs == {
        "submission": {"label": "Your solution"},
        "language": {"label": "Language", "options": ["python"]},
        "time_limit": 2,
        "memory_limit": 256,
    }
    assert files["statement.md"] == b"Write the statement entrants read here.\n"
    assert list(task.test_groups) == ["main"]
    assert task.test_groups["main"].each == 100
    assert task.credit is None
    assert set(files) == {
        "task.yaml",
        "statement.md",
        "public/.gitkeep",
        "tests/main/1/input",
        "tests/main/1/answer",
    }
    assert files["tests/main/1/input"] == b"1 2\n"
    assert files["tests/main/1/answer"] == b"3\n"


def test_a_title_is_the_one_asked_for_or_the_name() -> None:
    assert title_of("Sum of Two", "sum") == "Sum of Two"
    assert title_of("   ", "sum") == "sum"
    assert title_of(None, "sum") == "sum"


def test_the_task_admin_keys_are_the_name_and_the_submissions() -> None:
    assert TASK_ADMIN_KEYS == ("name", "submissions")
    before = task_text("{main: {each: 1}}").encode()
    after = before.replace(b"name: T", b"name: U") + b"submissions: {max: 3}\n"

    assert admin_only_changes("task", before, after) == ["name", "submissions"]
    managers = before.replace(b"{main: {each: 1}}", b"{main: {each: 2}}") + b"credit: x\n"
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
