"""Leaderboards written as block trees (SIMPLER-CORE.md 10 and 10.1, the
leaderboards note's 5.3 to 5.10): SIMPLER-CORE.md 10's gradebook, olympiad
and ICPC boards parse, pass the check and work out their rows; each board
block works out as 5.3 and 5.4 say, ties to the oldest and missing
skipped; each refusal of the board check; a column's bounds and its
self-reported parts; and the hold.
"""

import uuid
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

import pytest

from forge.domain.blocks.board_check import MOST_COLUMNS, BoardsCheck, check_boards
from forge.domain.blocks.board_evaluate import Cell, Submitted, work_out_row
from forge.domain.blocks.bounds import column_bounds, self_reported_parts, value_bounds
from forge.domain.blocks.catalogue import CATALOGUE, Scope
from forge.domain.blocks.check import check_values
from forge.domain.blocks.evaluate import Column, RunResult
from forge.domain.blocks.numbers import Better
from forge.domain.board_checks import check_contest, marks_of
from forge.domain.boards import Unranked, headers_of, ranked
from forge.domain.definitions import (
    ContestDefinition,
    InvalidDefinition,
    Leaderboard,
    parse_contest,
)
from forge.domain.names import EntrantOwner
from forge.domain.yaml_models import one_line

HEAD = """\
name: Boards
start: 2026-06-01T09:00:00Z
end: 2026-06-01T14:00:00Z
state: published
visibility: everyone
"""

GRADEBOOK = """\
tasks:
  - id: search
  - id: cartpole
leaderboards:
  gradebook:
    audience: {who: entrants, rows: own}
    columns:
      search_mark:
        block: otherwise
        of:
          - value:
              - block: otherwise
                of:
                  - {block: latest, from: {results: search}, where: {value: [search, marked]}}
                  - {block: latest, from: {results: search}}
              - score
          - 0
      cartpole_best:
        block: otherwise
        of:
          - value:
              - {block: highest, from: {results: cartpole}, by: {value: [cartpole, reward]}}
              - reward
          - 0
      total:
        block: add
        of:
          - {block: mul, of: [0.5, {column: search_mark}]}
          - block: mul
            of:
              - 0.5
              - block: min
                of: [{block: div, of: [{column: cartpole_best}, 475]}, 1]
    sort: [{total: desc}]
    display: [search_mark, cartpole_best, {total: {places: 2}}]
  cartpole_ranking:
    audience: {who: everyone}
    columns:
      reward:
        value:
          - {block: highest, from: {results: cartpole}, by: {value: [cartpole, reward]}}
          - reward
    sort: [{reward: desc}]
    display: [reward]
"""

OLYMPIAD = """\
tasks:
  - id: islands
leaderboards:
  results:
    audience: {who: everyone}
    columns:
      islands_score:
        block: otherwise
        of:
          - block: best_each
            from: {results: islands}
            value: {name: total}
            directions: {st1: desc, st2: desc}
          - 0
    sort: [{islands_score: desc}]
    display: [islands_score]
"""


def _icpc_task(task: str) -> str:
    return f"""\
      {task}_win:
        block: first
        from: {{results: {task}}}
        where: {{block: eq, of: [{{value: [{task}, solved]}}, 1]}}
      {task}_tries:
        block: count
        over: {{results: {task}}}
        where:
          block: and
          of:
            - {{block: eq, of: [{{value: [{task}, compiled]}}, 1]}}
            - {{block: eq, of: [{{value: [{task}, solved]}}, 0]}}
            - block: otherwise
              of:
                - block: lt
                  of: [{{value: [{task}, attempt]}}, {{value: [{task}_win, attempt]}}]
                - 1
      {task}_minute:
        block: if
        test: {{column: {task}_win}}
        then: {{block: floor, of: {{block: div, of: [{{value: [{task}_win, seconds]}}, 60]}}}}
        else: 0
      {task}_time:
        block: if
        test: {{column: {task}_win}}
        then:
          block: add
          of: [{{column: {task}_minute}}, {{block: mul, of: [20, {{column: {task}_tries}}]}}]
        else: 0
"""


ICPC = (
    """\
hold: {from: 2026-06-01T13:00:00Z, until: staff}
tasks:
  - id: a
  - id: b
leaderboards:
  standings:
    audience: {who: everyone}
    columns:
"""
    + _icpc_task("a")
    + _icpc_task("b")
    + """\
      solved:
        block: add
        of:
          - {block: if, test: {column: a_win}, then: 1, else: 0}
          - {block: if, test: {column: b_win}, then: 1, else: 0}
      penalty: {block: add, of: [{column: a_time}, {column: b_time}]}
      last_solve: {block: max, of: [{column: a_minute}, {column: b_minute}]}
    sort: [{solved: desc}, {penalty: asc}, {last_solve: asc}]
    display: [solved, penalty, last_solve, a_tries, a_time, b_tries, b_time]
"""
)

ISLANDS_VALUES = {
    "st1": {"block": "min", "over": {"runs": "st1_tests"}, "of": {"run": ["st1_tests", "score"]}},
    "st2": {"block": "min", "over": {"runs": "st2_tests"}, "of": {"run": ["st2_tests", "score"]}},
    "total": {
        "block": "add",
        "of": [
            {"block": "mul", "of": [40, {"value": "st1"}]},
            {"block": "mul", "of": [60, {"value": "st2"}]},
        ],
    },
}


def contest(text: str) -> ContestDefinition:
    return parse_contest(HEAD + text, saved=True)


def checked(settings: ContestDefinition) -> BoardsCheck:
    return check_boards(settings.leaderboards, [task.id for task in settings.tasks])


def problems(text: str) -> list[tuple[str, str]]:
    found, _ = check_contest(contest(text))
    return [(".".join(str(part) for part in path), message) for path, message in found]


def sub(number: int, *, waiting: tuple[str, ...] = (), **numbers: Any) -> Submitted:
    return Submitted(
        number, {name: Fraction(str(value)) for name, value in numbers.items()}, frozenset(waiting)
    )


def cells(
    text: str,
    results: dict[str, list[Submitted]],
    trees: dict[str, dict[str, object]] | None = None,
) -> dict[tuple[str, str], Cell]:
    settings = contest(text)
    found = checked(settings)
    assert not found.problems, found.problems
    return work_out_row(settings.leaderboards, found, results, trees or {})


def column(board: str, name: str, results: dict[str, list[Submitted]], tree: object) -> Cell:
    """One column `name` of a board over tasks a and b, worked out."""
    settings = contest("tasks: [{id: a}, {id: b}]\n")
    leaderboard = Leaderboard.model_validate({"columns": {name: tree}})
    boards = {board: leaderboard}
    found = check_boards(boards, [task.id for task in settings.tasks])
    assert not found.problems, found.problems
    return work_out_row(boards, found, results, {})[(board, name)]


def results(task: str) -> dict[str, str]:
    return {"results": task}


def read(of: object, name: str) -> dict[str, list[object]]:
    return {"value": [of, name]}


# SIMPLER-CORE.md 10's boards


def test_the_gradebook_works_out_a_marked_or_latest_score_and_the_best_reward() -> None:
    found = cells(
        GRADEBOOK,
        {
            "search": [sub(1, score=0.9, marked=1), sub(2, score=0.5, marked=0)],
            "cartpole": [sub(1, reward=300), sub(2, reward=475), sub(3, reward=475)],
        },
    )

    assert found[("gradebook", "search_mark")].number == Fraction(9, 10)
    assert found[("gradebook", "search_mark")].counted == {("search", 1)}
    assert found[("gradebook", "cartpole_best")].number == 475
    # Two rewards tie at the best, so the oldest counts.
    assert found[("gradebook", "cartpole_best")].counted == {("cartpole", 2)}
    assert found[("gradebook", "total")].number == Fraction(95, 100)
    assert found[("gradebook", "total")].counted == {("search", 1), ("cartpole", 2)}
    assert found[("cartpole_ranking", "reward")].number == 475


def test_the_gradebook_falls_back_to_the_latest_then_to_0() -> None:
    latest = cells(GRADEBOOK, {"search": [sub(1, score=0.9, marked=0), sub(2, score=0.5)]})
    nothing = cells(GRADEBOOK, {})

    assert latest[("gradebook", "search_mark")].number == Fraction(1, 2)
    assert nothing[("gradebook", "search_mark")].number == 0
    assert nothing[("gradebook", "total")].number == 0
    # The ranking has no fallback, so a row that never submitted is missing.
    assert nothing[("cartpole_ranking", "reward")].number is None


def test_the_olympiad_takes_the_best_of_each_subtask_over_every_submission() -> None:
    found = cells(
        OLYMPIAD,
        {"islands": [sub(1, st1=1, st2=0, total=40), sub(2, st1=0, st2=1, total=60)]},
        {"islands": ISLANDS_VALUES},
    )

    best = found[("results", "islands_score")]
    assert best.number == 100
    # One submission for each value it took the best of.
    assert best.counted == {("islands", 1), ("islands", 2)}


def test_the_icpc_board_counts_wrong_tries_before_the_first_solve() -> None:
    # Ana: a wrong at 0:10, a compile error at 0:15, accepted at 0:21; b
    # wrong and never solved.
    ana = {
        "a": [
            sub(1, attempt=1, seconds=600, compiled=1, solved=0),
            sub(2, attempt=2, seconds=900, compiled=0, solved=0),
            sub(3, attempt=3, seconds=1260, compiled=1, solved=1),
            sub(4, attempt=4, seconds=1500, compiled=1, solved=0),
        ],
        "b": [sub(1, attempt=1, seconds=60, compiled=1, solved=0)],
    }
    found = cells(ICPC, ana)

    def at(*names: str) -> list[Fraction | None]:
        return [found[("standings", name)].number for name in names]

    assert at("a_tries", "a_minute", "a_time") == [1, 21, 41]
    # With no solve, every judged, compiled submission is a failed try.
    assert at("b_tries", "b_minute", "b_time") == [1, 0, 0]
    assert at("solved", "penalty", "last_solve") == [1, 41, 21]
    assert found[("standings", "a_win")].counted == {("a", 3)}


def test_a_submission_still_grading_is_left_out_and_said_waiting() -> None:
    found = cells(
        ICPC,
        {
            "a": [
                sub(1, attempt=1, seconds=60, waiting=("solved", "compiled")),
                sub(2, attempt=2, seconds=120, compiled=1, solved=1),
            ]
        },
    )

    assert found[("standings", "a_win")].counted == {("a", 2)}
    assert found[("standings", "a_tries")].number == 0
    assert found[("standings", "a_tries")].waiting == {("a", 1)}
    assert ("a", 1) in found[("standings", "solved")].waiting


LAST = """\
tasks: [{id: a}]
leaderboards:
  one:
    columns:
      last: {block: latest, from: {results: a}}
      total: {block: otherwise, of: [{value: [last, total]}, -1]}
    display: [total]
  two:
    columns:
      total: {block: otherwise, of: [{value: [{column: [one, last]}, score]}, -1]}
    display: [total]
"""


def test_a_pick_passes_over_a_newest_submission_whose_value_read_through_it_waits() -> None:
    gradebook = cells(
        GRADEBOOK, {"search": [sub(1, score=0.9), sub(2, waiting=("score", "compiled"))]}
    )
    grading = {"a": [sub(1, total=50, score=5), sub(2, score=6, waiting=("total",))]}
    found = cells(LAST, grading)

    # The latest submission's score still waits, so the one before counts.
    mark = gradebook[("gradebook", "search_mark")]
    assert mark.number == Fraction(9, 10)
    assert mark.counted == {("search", 1)}
    assert mark.waiting == {("search", 2)}
    # A pick column counts as reading what every column reads through it,
    # another board's among them, so both boards pass over submission 2.
    assert found[("one", "total")].number == 50
    assert found[("one", "total")].waiting == {("a", 2)}
    assert found[("two", "total")].number == 5
    assert found[("two", "total")].counted == {("a", 1)}
    assert checked(contest(LAST)).through == {("one", "last"): {"total", "score"}}


def test_a_pick_takes_a_newest_submission_whose_waiting_value_it_does_not_read() -> None:
    reads_score = column(
        "b",
        "c",
        {"a": [sub(1, score=1), sub(2, score=2, waiting=("total",))]},
        read({"block": "latest", "from": results("a")}, "score"),
    )
    reads_total = column(
        "b",
        "c",
        {"a": [sub(1, total=1), sub(2, waiting=("total",))]},
        read({"block": "latest", "from": results("a")}, "total"),
    )

    assert reads_score.number == 2
    assert reads_score.counted == {("a", 2)}
    assert not reads_score.waiting
    assert reads_total.number == 1
    assert reads_total.counted == {("a", 1)}
    assert reads_total.waiting == {("a", 2)}


def test_a_column_read_from_another_board_works_out_and_names_the_task() -> None:
    text = (
        "tasks: [{id: a}, {id: b}]\nleaderboards:\n"
        "  one:\n    columns:\n"
        "      best: {block: max, over: {results: a}, of: {value: [a, total]}}\n"
        "      other: {block: count, over: {results: b}}\n"
        "  two:\n    columns:\n      half: {block: mul, of: [0.5, {column: [one, best]}]}\n"
        "  three:\n    columns:\n      tries: {column: [one, other]}\n"
    )
    found = cells(text, {"a": [sub(1, total=40), sub(2, total=80)]})
    boards = checked(contest(text))

    assert found[("two", "half")].number == 40
    assert found[("two", "half")].counted == {("a", 2)}
    # A board reading a task only through another board's column reads it.
    assert boards.reading("a") == ["one", "two"]
    assert boards.reads("a", "total") == ["one", "two"]
    assert boards.reading("b") == ["one", "three"]
    # A read of two works out only its columns and those they read.
    assert boards.needed(["two"]).order == (("one", "best"), ("two", "half"))


def test_the_three_boards_rank_rows_with_shared_places() -> None:
    settings = contest(ICPC)
    found = checked(settings)
    board = settings.leaderboards["standings"]

    def row(entrant: int, name: str, a: list[Submitted]) -> Unranked:
        worked = work_out_row(settings.leaderboards, found, {"a": a}, {})
        owner = EntrantOwner(uuid.UUID(int=entrant))
        return Unranked(owner, name, {key: worked[("standings", key)] for key in board.columns})

    solved = [sub(1, attempt=1, seconds=1200, compiled=1, solved=1)]
    standings = ranked(
        "standings",
        board,
        headers_of("standings", board, {}, {}),
        [row(1, "cy", []), row(2, "bo", solved), row(3, "al", solved)],
    )

    assert [(each.name, each.place) for each in standings.rows] == [
        ("al", 1),
        ("bo", 1),
        ("cy", 3),
    ]
    assert [cell.column for cell in standings.rows[0].cells] == [
        "solved",
        "penalty",
        "last_solve",
        "a_tries",
        "a_time",
        "b_tries",
        "b_time",
    ]


def test_a_missing_sort_key_sorts_last_whichever_way() -> None:
    settings = contest(GRADEBOOK)
    found = checked(settings)
    board = settings.leaderboards["cartpole_ranking"]

    def row(entrant: int, rewards: list[int]) -> Unranked:
        worked = work_out_row(
            settings.leaderboards,
            found,
            {"cartpole": [sub(n, reward=r) for n, r in enumerate(rewards, 1)]},
            {},
        )
        owner = EntrantOwner(uuid.UUID(int=entrant))
        return Unranked(owner, f"r{entrant}", {"reward": worked[("cartpole_ranking", "reward")]})

    ranking = ranked("cartpole_ranking", board, (), [row(1, []), row(2, [10]), row(3, [20])])

    assert [(each.name, each.place) for each in ranking.rows] == [
        ("r3", 1),
        ("r2", 2),
        ("r1", 3),
    ]


# Each board block


def test_every_board_block_has_a_test() -> None:
    board = {
        name
        for name, block in CATALOGUE.items()
        if any(form.scope is Scope.BOARD for form in block.forms)
    }
    assert board == {
        "first",
        "latest",
        "highest",
        "lowest",
        "best_each",
        "otherwise",
        "sum_best",
        "min",
        "max",
        "sum",
        "count",
    }


@pytest.mark.parametrize(
    ("block", "expected"),
    [("first", 2), ("latest", 4), ("highest", 2), ("lowest", 1)],
)
def test_each_pick_takes_its_submission_ties_to_the_oldest(block: str, expected: int) -> None:
    tree: dict[str, object] = {"block": block, "from": results("a")}
    if block in ("highest", "lowest"):
        tree["by"] = read("a", "score")
    else:
        tree["where"] = {"block": "gt", "of": [read("a", "score"), 1]}
    picked = column(
        "p",
        "pick",
        {"a": [sub(1, score=1), sub(2, score=5), sub(3, score=5), sub(4, score=2)]},
        read(tree, "score"),
    )
    assert picked.counted == {("a", expected)}


def test_a_missing_by_is_skipped_and_a_pick_over_nothing_is_none() -> None:
    highest = {"block": "highest", "from": results("a"), "by": read("a", "score")}

    skipped = column("p", "c", {"a": [sub(1, score=3), sub(2)]}, read(highest, "score"))
    none = column("p", "c", {"a": [sub(1)]}, read(highest, "score"))
    test = column("p", "c", {}, {"block": "if", "test": highest, "then": 1, "else": 0})

    assert (skipped.number, skipped.counted) == (Fraction(3), frozenset({("a", 1)}))
    assert (none.number, none.counted) == (None, frozenset())
    # A pick over nothing is none, so as a test it is false.
    assert test.number == 0


def test_count_over_nothing_is_0_and_the_other_folds_missing() -> None:
    count = {"block": "count", "over": results("a")}
    where = {**count, "where": read("a", "score")}

    assert column("p", "c", {}, count).number == 0
    # A missing where does not meet it.
    assert column("p", "c", {"a": [sub(1)]}, where).number == 0
    for block in ("sum", "max", "min"):
        # A missing of is left out, leaving nothing.
        tree = {"block": block, "over": results("a"), "of": read("a", "score")}
        assert column("p", "c", {"a": [sub(1)]}, tree).number is None


def test_max_given_a_tasks_results_and_max_given_two_numbers_in_one_column() -> None:
    tree = {
        "block": "max",
        "of": [
            {"block": "max", "over": results("a"), "of": read("a", "score")},
            {"block": "max", "over": results("b"), "of": read("b", "score")},
        ],
    }
    found = column(
        "p",
        "c",
        {"a": [sub(1, score=2), sub(2, score=7), sub(3, score=7)], "b": [sub(1, score=5)]},
        tree,
    )
    assert found.number == 7
    # max over results counts the oldest that reached the result.
    assert found.counted == {("a", 2), ("b", 1)}


def test_sum_and_count_count_every_submission_they_keep() -> None:
    where = {"block": "ge", "of": [read("a", "score"), 2]}
    listed = {"a": [sub(1, score=1), sub(2, score=2), sub(3, score=3)]}

    summed = column(
        "p",
        "c",
        listed,
        {"block": "sum", "over": results("a"), "of": read("a", "score"), "where": where},
    )
    counted = column("p", "c", listed, {"block": "count", "over": results("a"), "where": where})

    assert (summed.number, summed.counted) == (Fraction(5), frozenset({("a", 2), ("a", 3)}))
    assert (counted.number, counted.counted) == (Fraction(2), frozenset({("a", 2), ("a", 3)}))


def test_best_each_works_the_value_out_again_from_the_best_of_each_it_reads() -> None:
    tree = {
        "block": "best_each",
        "from": results("a"),
        "value": {"name": "total"},
        "directions": {"st1": "desc", "st2": "desc"},
    }
    listed = {"a": [sub(1, st1=1, st2=0), sub(2, st1=0, st2=1), sub(3, st1=1, st2=1)]}

    settings_trees = {"a": ISLANDS_VALUES}
    boards = {"p": Leaderboard.model_validate({"columns": {"c": tree}})}
    found = check_boards(boards, ["a"])
    cell = work_out_row(boards, found, listed, settings_trees)[("p", "c")]
    # A value reading a run directly cannot be worked out again.
    runs = work_out_row(
        boards, found, listed, {"a": {**ISLANDS_VALUES, "total": ISLANDS_VALUES["st1"]}}
    )

    assert cell.number == 100
    assert cell.counted == {("a", 1), ("a", 2)}
    assert runs[("p", "c")].number is None


def test_otherwise_and_sum_best_and_missing_spreading() -> None:
    listed = {"a": [sub(1, score=4)], "b": [sub(1)]}
    a = {"block": "max", "over": results("a"), "of": read("a", "score")}
    b = {"block": "max", "over": results("b"), "of": read("b", "score")}

    assert column("p", "c", listed, {"block": "otherwise", "of": [b, 9]}).number == 9
    assert column("p", "c", listed, {"block": "otherwise", "of": [a, 9]}).number == 4
    assert column("p", "c", listed, {"block": "add", "of": [a, b]}).number is None
    assert column("p", "c", listed, {"block": "sum_best", "k": 2, "of": [a, 3, 1]}).number == 7
    assert column("p", "c", listed, {"block": "sum_best", "k": 2, "of": [a, b]}).number is None


# The board check


def test_the_three_boards_of_simpler_core_10_pass_the_check() -> None:
    for text in (GRADEBOOK, OLYMPIAD, ICPC):
        assert problems(text) == []


def test_a_board_with_no_audience_is_staffs() -> None:
    settings = contest("tasks: [{id: a}]\nleaderboards:\n  standings: {}\n")

    assert settings.leaderboards["standings"].audience.who == "staff"
    assert settings.leaderboards["standings"].title_or("standings") == "standings"


def test_own_rows_for_everyone_and_old_boards_are_refused_at_parse() -> None:
    with pytest.raises(InvalidDefinition) as own:
        contest("leaderboards:\n  s: {audience: {who: everyone, rows: own}}\n")
    with pytest.raises(InvalidDefinition) as old:
        contest("leaderboards:\n  - {name: Standings, who: everyone}\n")
    with pytest.raises(InvalidDefinition) as keyed:
        contest("leaderboards:\n  s: {name: Standings, select: best}\n")
    with pytest.raises(InvalidDefinition) as task:
        contest("tasks: [{id: s}]\nleaderboards:\n  s: {}\n")

    assert [error["path"] for error in own.value.errors] == ["leaderboards.s.audience.rows"]
    assert [error["path"] for error in old.value.errors] == ["leaderboards"]
    assert {error["path"] for error in keyed.value.errors} == {
        "leaderboards.s.name",
        "leaderboards.s.select",
    }
    assert [error["path"] for error in task.value.errors] == ["leaderboards.s"]


def board(columns: Mapping[str, object], **more: object) -> str:
    document: dict[str, object] = {"columns": columns, **more}
    return "tasks: [{id: a}, {id: b-c}]\nleaderboards:\n  s: " + one_line(document) + "\n"


PICK = {"block": "first", "from": results("a")}
COUNT = {"block": "count", "over": results("a")}


@pytest.mark.parametrize(
    ("columns", "more", "path"),
    [
        # A submission's results where a number is taken.
        ({"c": {"block": "add", "of": [PICK, 1]}}, {}, "leaderboards.s.columns.c.of.0"),
        # A number where a submission's results are read.
        ({"n": COUNT, "c": read("n", "score")}, {}, "leaderboards.s.columns.c.value.0"),
        # otherwise given one of each.
        ({"c": {"block": "otherwise", "of": [PICK, 0]}}, {}, "leaderboards.s.columns.c.of"),
        # t.v outside a where, a by or a fold's of.
        ({"c": read("a", "score")}, {}, "leaderboards.s.columns.c"),
        (
            {"c": {"block": "count", "over": results("a"), "where": read("b-c", "x")}},
            {},
            "leaderboards.s.columns.c.where",
        ),
        # A task the contest does not have.
        ({"c": {"block": "count", "over": results("z")}}, {}, "leaderboards.s.columns.c.over"),
        # A task's value's blocks.
        ({"c": {"block": "count", "over": {"runs": "all"}}}, {}, "leaderboards.s.columns.c"),
        ({"c": {"block": "entered", "at_least": 0, "at_most": 1}}, {}, "leaderboards.s.columns.c"),
        # Names shared with a task, a board, or alike once - is _.
        ({"a": COUNT}, {}, "leaderboards.s.columns.a"),
        ({"s": COUNT}, {}, "leaderboards.s.columns.s"),
        ({"b_c": COUNT}, {}, "leaderboards.s.columns.b_c"),
        # A loop.
        (
            {"x": {"column": "y"}, "y": {"block": "add", "of": [{"column": "x"}, 1]}},
            {},
            "leaderboards.s.columns.x",
        ),
        # A pick column displayed or sorted on, a sort key not displayed.
        ({"p": PICK}, {"display": ["p"]}, "leaderboards.s.display.0"),
        ({"p": PICK}, {"sort": [{"p": "desc"}]}, "leaderboards.s.sort.0"),
        ({"n": COUNT}, {"sort": [{"n": "desc"}]}, "leaderboards.s.sort.0"),
        ({"n": COUNT}, {"display": ["n", "n"]}, "leaderboards.s.display.1"),
        ({"n": COUNT}, {"display": ["m"]}, "leaderboards.s.display.0"),
        # sum_best's k a whole number written out.
        ({"c": {"block": "sum_best", "k": 0, "of": [1]}}, {}, "leaderboards.s.columns.c.k"),
        # best_each's directions.
        (
            {
                "c": {
                    "block": "best_each",
                    "from": results("a"),
                    "value": {"name": "t"},
                    "directions": {"x": "up"},
                }
            },
            {},
            "leaderboards.s.columns.c.directions",
        ),
    ],
)
def test_each_refusal_of_the_board_check(
    columns: dict[str, object], more: dict[str, object], path: str
) -> None:
    assert path in [each for each, _ in problems(board(columns, **more))]


def test_a_loop_across_boards_and_a_column_another_board_lacks_are_refused() -> None:
    text = (
        "tasks: [{id: a}]\nleaderboards:\n"
        "  one: {columns: {x: {column: [two, y]}}}\n"
        "  two: {columns: {y: {column: [one, x]}, z: {column: [one, w]}}}\n"
    )
    found = [path for path, _ in problems(text)]

    assert "leaderboards.one.columns.x" in found
    assert "leaderboards.two.columns.y" in found
    assert "leaderboards.two.columns.z" in found
    assert checked(contest(text)).readable == frozenset()


def test_a_board_reading_a_refused_board_is_not_read() -> None:
    text = (
        "tasks: [{id: a}]\nleaderboards:\n"
        "  one: {columns: {x: {block: nope}}}\n"
        "  two: {columns: {y: {column: [one, x]}}}\n"
        "  three: {columns: {z: 1}}\n"
    )
    assert checked(contest(text)).readable == frozenset({"three"})


def test_a_board_of_too_many_columns_is_refused() -> None:
    columns = {f"c{index}": 1 for index in range(MOST_COLUMNS + 1)}
    assert [path for path, _ in problems(board(columns))] == ["leaderboards.s.columns"]


def test_a_task_value_using_a_board_block_or_leaf_is_refused() -> None:
    for tree in (
        {"block": "first", "from": results("a")},
        {"block": "max", "over": results("a"), "of": 1},
        {"block": "otherwise", "of": [1, 2]},
        {"column": "x"},
        {"value": ["a", "x"]},
    ):
        found = check_values({"v": tree}, groups=[], columns={}, keys={})
        assert found.problems, tree


def test_marks_go_with_a_board_reading_marked() -> None:
    unread = "tasks: [{id: search, marks: 2}]\n"
    read_marked = (
        "tasks: [{id: search, marks: 2}, {id: other}]\nleaderboards:\n  g:\n    columns:\n"
        "      m:\n        value:\n"
        "          - {block: latest, from: {results: search}, where: {value: [search, marked]}}\n"
        "          - score\n"
    )

    assert [path for path, _ in problems(unread)] == ["tasks.0.marks"]
    assert problems(read_marked) == []
    settings = contest(read_marked.replace("{id: search, marks: 2}", "{id: search}"))
    assert marks_of(settings, settings.tasks[0]) == 1
    assert marks_of(settings, settings.tasks[1]) is None


# Bounds and self-reported parts


def _islands_bounds() -> dict[str, Any]:
    runs = [
        RunResult("r3", "", {"input": "tests/3.in"}),
        RunResult("r4", "", {"input": "tests/4.in"}),
    ]
    columns = {
        "score": Column(Better.HIGHER, Fraction(0), Fraction(1)),
        "time": Column(Better.LOWER, Fraction(0), None),
    }
    return value_bounds(
        {
            **ISLANDS_VALUES,
            "slow": {"block": "max", "over": {"runs": "all"}, "of": {"run": ["all", "time"]}},
        },
        {"all": runs, "st1_tests": runs[:1], "st2_tests": runs},
        columns,
    )


def test_a_tasks_values_carry_their_bounds_where_every_part_has_one() -> None:
    found = _islands_bounds()

    assert found["total"] == (0, 100)
    assert found["slow"] is None


def test_the_gradebooks_total_carries_its_bounds_and_seconds_late_none() -> None:
    text = GRADEBOOK.replace(
        "    sort: [{total: desc}]",
        "      late:\n        value:\n          - {block: latest, from: {results: search}}\n"
        "          - seconds_late\n    sort: [{total: desc}]",
        1,
    )
    settings = contest(text)
    found = checked(settings)
    tasks = {
        "search": {"score": (Fraction(0), Fraction(1))},
        "cartpole": {"reward": (Fraction(0), Fraction(500))},
    }
    bounds = column_bounds(settings.leaderboards, found, tasks, {"search": 50, "cartpole": 50})

    assert bounds[("gradebook", "search_mark")] == (Fraction(0), Fraction(1))
    assert bounds[("gradebook", "cartpole_best")] == (Fraction(0), Fraction(500))
    assert bounds[("gradebook", "total")] == (Fraction(0), Fraction(1))
    assert bounds[("gradebook", "late")] is None


def test_a_division_by_bounds_holding_0_and_a_fold_over_results_by_submissions() -> None:
    settings = contest(
        board(
            {
                "n": COUNT,
                "sm": {"block": "sum", "over": results("a"), "of": read("a", "score")},
                "d": {"block": "div", "of": [1, {"column": "n"}]},
            }
        )
    )
    found = checked(settings)
    bounds = column_bounds(
        settings.leaderboards, found, {"a": {"score": (Fraction(0), Fraction(2))}}, {"a": 3}
    )

    assert bounds[("s", "n")] == (Fraction(0), Fraction(3))
    assert bounds[("s", "sm")] == (Fraction(0), Fraction(6))
    assert bounds[("s", "d")] is None


def test_a_column_over_two_tasks_names_the_one_value_that_is_self_reported() -> None:
    text = (
        "tasks: [{id: a}, {id: b}]\nleaderboards:\n  s:\n    columns:\n"
        "      both:\n        block: add\n        of:\n"
        "          - {block: max, over: {results: a}, of: {value: [a, score]}}\n"
        "          - {block: max, over: {results: b}, of: {value: [b, score]}}\n"
        "      later: {block: mul, of: [2, {column: both}]}\n"
        "    display: [both, later]\n"
    )
    settings = contest(text)
    found = checked(settings)
    parts = self_reported_parts(found, {"a": frozenset(), "b": frozenset({"score"})})
    headers = headers_of("s", settings.leaderboards["s"], {}, parts)

    assert [header.self_reported for header in headers] == [(("b", "score"),), (("b", "score"),)]


# The hold


def test_a_hold_is_from_a_time_until_a_time_or_staff() -> None:
    held = contest("hold: {from: 2026-06-01T13:00:00Z, until: staff}\n")
    assert held.hold is not None and held.hold.until == "staff"
    for wrong in (
        "hold: {from: 1h before close, until: staff}\n",
        "hold: {from: 2026-06-01T13:00:00Z, until: 2026-06-01T12:00:00Z}\n",
        "hold: {from: 2026-06-01T13:00:00Z, until: later}\n",
        "freeze_at: 2026-06-01T13:00:00Z\n",
    ):
        with pytest.raises(InvalidDefinition):
            contest(wrong)
