"""Leaderboards written as block trees (SIMPLER-CORE.md 10 and 10.1, the
leaderboards note's 5.3 to 5.10): SIMPLER-CORE.md 10's gradebook, olympiad
and ICPC leaderboards parse, pass the check and work out their rows; each leaderboard
block works out as 5.3 and 5.4 say, ties to the oldest and missing
skipped; each refusal of the leaderboard check; a column's bounds and its
self-reported parts; and the hold.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import pytest

from forge.domain.blocks.bounds import column_bounds, self_reported_parts, value_bounds
from forge.domain.blocks.catalogue import CATALOGUE, Scope
from forge.domain.blocks.check import check_values
from forge.domain.blocks.evaluate import Column, RunResult
from forge.domain.blocks.formula_words import UNREADABLE
from forge.domain.blocks.leaderboard_evaluate import Cell, Submitted, work_out_row
from forge.domain.blocks.leaderboard_shape_check import (
    MOST_COLUMNS,
    MOST_LEADERBOARDS,
    LeaderboardsCheck,
    check_leaderboards,
)
from forge.domain.blocks.numbers import Better
from forge.domain.definitions import (
    ContestDefinition,
    Hold,
    InvalidDefinition,
    Leaderboard,
    parse_contest,
    parse_task,
)
from forge.domain.leaderboard_task_check import (
    Scored,
    across,
    check_contest,
    hold_problems,
    marks_of,
)
from forge.domain.leaderboards import (
    STAFF,
    Counted,
    Header,
    LeaderboardCell,
    LeaderboardView,
    Loaded,
    Row,
    Viewer,
    counted_in,
    entries_open,
    grading_seen,
    headers_of,
    held_back,
    holding,
    leaderboards_read,
    listed_now,
    read_leaderboards,
    results_seen,
    seen_bounds,
    still_grading,
)
from forge.domain.names import EntrantOwner
from forge.domain.run_showing import ValueShown
from forge.domain.yaml_models import one_line
from tests.test_run_task import ISLANDS as ISLANDS_TASK

HEAD = """\
name: Leaderboards
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


def checked(settings: ContestDefinition) -> LeaderboardsCheck:
    return check_leaderboards(settings.leaderboards, [task.id for task in settings.tasks])


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


def column(leaderboard: str, name: str, results: dict[str, list[Submitted]], tree: object) -> Cell:
    """One column `name` of a leaderboard over tasks a and b, worked out."""
    settings = contest("tasks: [{id: a}, {id: b}]\n")
    defined = Leaderboard.model_validate({"columns": {name: tree}})
    leaderboards = {leaderboard: defined}
    found = check_leaderboards(leaderboards, [task.id for task in settings.tasks])
    assert not found.problems, found.problems
    return work_out_row(leaderboards, found, results, {})[(leaderboard, name)]


def results(task: str) -> dict[str, str]:
    return {"results": task}


def read(of: object, name: str) -> dict[str, list[object]]:
    return {"value": [of, name]}


# SIMPLER-CORE.md 10's leaderboards


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


def test_the_icpc_leaderboard_counts_wrong_tries_before_the_first_solve() -> None:
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
        GRADEBOOK,
        {"search": [sub(1, score=0.9, marked=0), sub(2, marked=0, waiting=("score", "compiled"))]},
    )
    grading = {"a": [sub(1, total=50, score=5), sub(2, score=6, waiting=("total",))]}
    found = cells(LAST, grading)

    # The latest submission's score still waits, so the one before counts.
    mark = gradebook[("gradebook", "search_mark")]
    assert mark.number == Fraction(9, 10)
    assert mark.counted == {("search", 1)}
    assert mark.waiting == {("search", 2)}
    # A pick column counts as reading what every column reads through it,
    # another leaderboard's among them, so both leaderboards pass over submission 2.
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


def test_a_column_read_from_another_leaderboard_works_out_and_names_the_task() -> None:
    text = (
        "tasks: [{id: a}, {id: b}]\nleaderboards:\n"
        "  one:\n    columns:\n"
        "      best: {block: max, over: {results: a}, of: {value: [a, total]}}\n"
        "      other: {block: count, over: {results: b}}\n"
        "  two:\n    columns:\n      half: {block: mul, of: [0.5, {column: [one, best]}]}\n"
        "  three:\n    columns:\n      tries: {column: [one, other]}\n"
    )
    found = cells(text, {"a": [sub(1, total=40), sub(2, total=80)]})
    leaderboards = checked(contest(text))

    assert found[("two", "half")].number == 40
    assert found[("two", "half")].counted == {("a", 2)}
    # A leaderboard reading a task only through another leaderboard's column reads it.
    assert leaderboards.reading("a") == ["one", "two"]
    assert leaderboards.reads("a", "total") == ["one", "two"]
    assert leaderboards.reading("b") == ["one", "three"]
    # A read of two works out only its columns and those they read.
    assert leaderboards.needed(["two"]).order == (("one", "best"), ("two", "half"))


def owner(entrant: int) -> EntrantOwner:
    return EntrantOwner(uuid.UUID(int=entrant))


def read_as(
    text: str,
    key: str,
    rows: list[tuple[int, str, dict[str, list[Submitted]]]],
    viewer: Viewer = STAFF,
    *,
    released: list[str] | None = None,
    held: Mapping[int, Mapping[str, int]] | None = None,
) -> tuple[LeaderboardView, ...]:
    """The leaderboard `key` as `viewer` reads it, each row given by its entrant,
    name and results as the viewer reads them; empty when they see none."""
    settings = contest(text)
    found = checked(settings)
    assert not found.problems, found.problems
    worked = [
        Row(
            owner(entrant),
            name,
            work_out_row(settings.leaderboards, found, lists, {}),
            (held or {}).get(entrant, {}),
            still_grading(lists),
        )
        for entrant, name, lists in rows
    ]
    return read_leaderboards(
        settings.leaderboards,
        found,
        [key],
        worked,
        viewer,
        released=[task.id for task in settings.tasks] if released is None else released,
        headers={key: headers_of(key, each, {}, {}) for key, each in settings.leaderboards.items()},
    )


def one(leaderboards: tuple[LeaderboardView, ...]) -> LeaderboardView:
    assert len(leaderboards) == 1
    return leaderboards[0]


def test_the_three_leaderboards_rank_rows_with_shared_places() -> None:
    solved = [sub(1, attempt=1, seconds=1200, compiled=1, solved=1)]
    standings = one(
        read_as(
            ICPC, "standings", [(1, "cy", {}), (2, "bo", {"a": solved}), (3, "al", {"a": solved})]
        )
    )

    # Guarantee 7: ties share a place, 1, 1, 3, listed by name.
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


def rewards(entrant: int, *found: int) -> tuple[int, str, dict[str, list[Submitted]]]:
    return entrant, f"r{entrant}", {"cartpole": [sub(n, reward=r) for n, r in enumerate(found, 1)]}


def test_a_missing_sort_key_sorts_last_whichever_way() -> None:
    ranking = one(
        read_as(GRADEBOOK, "cartpole_ranking", [rewards(1), rewards(2, 10), rewards(3, 20)])
    )

    assert [(each.name, each.place) for each in ranking.rows] == [
        ("r3", 1),
        ("r2", 2),
        ("r1", 3),
    ]


# Who sees what (SIMPLER-CORE.md 10.2, the leaderboards note's 5.1)


def test_a_row_reads_only_its_own_entrant() -> None:
    # Guarantee 1: another row submitting moves no cell of this one.
    alone = one(read_as(GRADEBOOK, "cartpole_ranking", [rewards(1, 10), rewards(2)]))
    beaten = one(read_as(GRADEBOOK, "cartpole_ranking", [rewards(1, 10), rewards(2, 500)]))

    assert alone.rows[0].cells == beaten.rows[1].cells


def hidden_sub(number: int, *names: str, **numbers: Any) -> Submitted:
    found = sub(number, **numbers)
    return Submitted(found.number, found.numbers, found.waiting, frozenset(names))


def test_a_cell_is_hidden_when_a_pick_compared_a_value_not_shown() -> None:
    # Guarantee 2: the highest reward is picked by comparing every reward,
    # so one hidden reward hides the cell, and what it counted with it.
    lists = {"cartpole": [sub(1, reward=10), hidden_sub(2, "reward")]}
    seen = one(read_as(GRADEBOOK, "cartpole_ranking", [(1, "r1", lists)], Viewer()))
    staff = one(read_as(GRADEBOOK, "cartpole_ranking", [(1, "r1", lists)]))

    # Why it has no number is told on no row but the viewer's own (item 127).
    assert seen.rows[0].cells == (LeaderboardCell("reward", None),)
    assert staff.rows[0].cells[0].number == 10


def test_a_fold_is_hidden_when_its_where_or_its_of_read_a_value_not_shown() -> None:
    # Guarantee 2: a fold tests its where and folds its of over each
    # submission, so one hidden value hides it.
    lists = {"a": [sub(1, score=3), hidden_sub(2, "score")]}
    where = {"block": "gt", "of": [read("a", "score"), 1]}
    count = column("p", "c", lists, {"block": "count", "over": results("a"), "where": where})
    most = column("p", "c", lists, {"block": "max", "over": results("a"), "of": read("a", "score")})
    shown = column("p", "c", {"a": [sub(1, score=3)]}, {"block": "count", "over": results("a")})

    assert count.hidden and most.hidden
    assert not shown.hidden


def test_best_each_is_hidden_when_a_value_it_reads_is_not_shown_on_any_submission() -> None:
    # Guarantee 2: best_each compares each value its value reads.
    tree = {
        "block": "best_each",
        "from": results("a"),
        "value": {"name": "total"},
        "directions": {"st1": "desc", "st2": "desc"},
    }
    leaderboards = {"p": Leaderboard.model_validate({"columns": {"c": tree}})}
    found = check_leaderboards(leaderboards, ["a"])

    def best(*listed: Submitted) -> Cell:
        return work_out_row(leaderboards, found, {"a": list(listed)}, {"a": ISLANDS_VALUES})[
            ("p", "c")
        ]

    assert best(sub(1, st1=1, st2=0), hidden_sub(2, "st2", st1=0)).hidden
    assert not best(sub(1, st1=1, st2=0), sub(2, st1=0, st2=1)).hidden


def test_a_pick_is_hidden_by_a_value_read_through_it_on_a_submission_it_did_not_take() -> None:
    # Item 85: the latest submission is taken, but the total read through
    # the pick is hidden on the one before, so the cell is hidden.
    latest = read({"block": "latest", "from": results("a")}, "total")
    hidden = column("p", "c", {"a": [hidden_sub(1, "total"), sub(2, total=50)]}, latest)
    shown = column("p", "c", {"a": [sub(1, total=40), sub(2, total=50)]}, latest)

    assert hidden.hidden and hidden.counted == {("a", 2)}
    assert (shown.number, shown.hidden) == (Fraction(50), False)


KAGGLE = """tasks: [{id: k}]
leaderboards:
  public:
    audience: {who: everyone}
    columns:
      score: {block: max, over: {results: k}, of: {value: [k, public]}}
    sort: [{score: desc}]
    display: [score]
  private:
    audience: {who: everyone}
    columns:
      score:
        value:
          - {block: highest, from: {results: k}, by: {value: [k, public]}}
          - private
    sort: [{score: desc}]
    display: [score]
"""


def kaggle(number: int, public: int, private: int, *, shown: bool) -> Submitted:
    values = [ValueShown("public", Fraction(public), False, False)]
    if shown:
        values.append(ValueShown("private", Fraction(private), False, False))
    listed = [value.name for value in values]
    return results_seen(number, {}, values, 1, ["public", "private"], listed, True)


def test_the_kaggle_private_leaderboard_is_hidden_until_private_is_shown() -> None:
    def private(shown: bool) -> LeaderboardView:
        lists = {"k": [kaggle(1, 80, 70, shown=shown), kaggle(2, 90, 60, shown=shown)]}
        return one(read_as(KAGGLE, "private", [(1, "r1", lists), (2, "r2", {})], Viewer()))

    before, after = private(False), private(True)
    public = one(
        read_as(KAGGLE, "public", [(1, "r1", {"k": [kaggle(1, 80, 70, shown=False)]})], Viewer())
    )

    assert before.rows[0].cells == (LeaderboardCell("score", None),)
    assert after.rows[0].cell("score") == LeaderboardCell("score", Fraction(60))
    # A hidden key is no sort key: no places until it is shown.
    assert before.dropped == ("score",) and before.sort == ()
    assert [row.place for row in before.rows] == [None, None]
    assert [row.place for row in after.rows] == [1, 2]
    assert public.rows[0].cells[0].number == 80


def test_a_value_listed_now_but_not_yet_shown_waits_and_one_not_listed_is_hidden() -> None:
    # A late run still running or faulted leaves a value listed now out of
    # what its entrant is shown: it waits, so no viewer learns which rows
    # still grade, as `compiled` does while a group is listed.
    shown = [ValueShown("public", Fraction(5), False, False)]
    late = results_seen(1, {}, shown, None, ["public", "private"], ["public", "private"], True)
    early = results_seen(1, {}, shown, None, ["public", "private"], ["public"], False)

    assert late.waiting == {"private", "compiled"} and not late.hidden
    assert not early.waiting and early.hidden == {"private", "compiled"}
    rows = [(1, "r1", {"k": [kaggle(1, 80, 70, shown=True)]}), (2, "r2", {"k": [late]})]
    leaderboard = one(read_as(KAGGLE, "private", rows, Viewer()))
    assert leaderboard.dropped == ()
    assert [(row.name, row.place) for row in leaderboard.rows] == [("r1", 1), ("r2", 2)]
    assert leaderboard.rows[1].cells == (LeaderboardCell("score", None),)


def test_a_sort_key_hidden_on_one_row_drops_places_for_every_row() -> None:
    # Guarantee 3: rows are never ordered by what the viewer is not shown.
    rows = [
        rewards(1, 10),
        (2, "r2", {"cartpole": [hidden_sub(1, "reward")]}),
        rewards(3, 20),
    ]
    seen = one(read_as(GRADEBOOK, "cartpole_ranking", rows, Viewer()))
    own = one(read_as(GRADEBOOK, "cartpole_ranking", rows, Viewer(own=owner(1))))

    assert seen.dropped == ("reward",)
    assert [(row.name, row.place) for row in seen.rows] == [
        ("r1", None),
        ("r2", None),
        ("r3", None),
    ]
    assert own.dropped == ("reward",)


def test_a_sort_key_hidden_on_a_row_past_a_top_n_still_drops_it() -> None:
    # Item 92: which rows are served depends on the sort, so a key is
    # judged on every row before the cut.
    text = GRADEBOOK.replace("audience: {who: everyone}", "audience: {who: everyone, rows: 1}")
    rows = [rewards(1, 10), (2, "r2", {"cartpole": [hidden_sub(1, "reward")]})]
    seen = one(read_as(text, "cartpole_ranking", rows, Viewer()))

    assert seen.dropped == ("reward",)
    assert [(row.name, row.place) for row in seen.rows] == [("r1", None)]


def test_a_value_reading_an_entered_mark_waits_by_show_and_compiled_with_no_group_is_hidden() -> (
    None
):
    # Guarantee 6: a submission still grading waits only on what its
    # entrant may be shown, so a waiting count never tells of the rest. A
    # value reading a mark is read by `show` alone: a cell reads it only on
    # a submission it counted (item 117).
    text = ISLANDS_TASK.replace(
        "    total:\n",
        "    review: {block: entered, at_least: 0, at_most: 10}\n"
        "    reviewed: {block: add, of: [{value: sample_score}, {value: review}]}\n"
        "    total:\n",
    ).replace("now: [sample_score,", "now: [review, reviewed, sample_score,")
    scoring = parse_task(text).scoring
    assert scoring is not None
    values, runs = listed_now(scoring, ("now",))
    grading = grading_seen(1, {}, scoring.values, values, runs)
    no_group = grading_seen(1, {}, scoring.values, {"sample_score"}, False)

    assert values == {"review", "reviewed", "sample_score", "sample_time"} and runs
    assert grading.waiting == {"review", "reviewed", "sample_score", "sample_time", "compiled"}
    assert {"st1", "st2", "total"} <= grading.hidden
    assert "compiled" in no_group.hidden and "compiled" not in no_group.waiting
    tries = {"block": "count", "over": results("a"), "where": read("a", "compiled")}
    assert column("p", "c", {"a": [no_group]}, tries).hidden
    assert column("p", "c", {"a": [grading]}, tries).waiting == {("a", 1)}


def test_compiled_listed_by_its_name_reads_on_a_leaderboard_with_no_group_shown() -> None:
    # Item 124: show lists compiled by its name, so a leaderboard reads it, and
    # it waits while the submission grades, with no group shown.
    text = ISLANDS_TASK.replace(
        "now: [sample_score, sample_time, sample_tests, samples/]", "now: [compiled, sample_score]"
    )
    scoring = parse_task(text).scoring
    assert scoring is not None
    values, compiled = listed_now(scoring, ("now",))
    grading = grading_seen(1, {}, scoring.values, values, compiled)
    graded = results_seen(1, {}, [], 0, scoring.values, values, compiled)

    assert compiled and "compiled" in grading.waiting
    assert graded.numbers["compiled"] == 0 and "compiled" not in graded.hidden
    tries = {"block": "count", "over": results("a"), "where": read("a", "compiled")}
    assert column("p", "c", {"a": [graded]}, tries).number == 0


def test_what_the_cells_counted_is_listed_by_row_number_leaderboard_and_column() -> None:
    # What the grading queue lists, and when an entry may show (#505).
    text = GRADEBOOK + (
        "  latest:\n    audience: {who: everyone}\n"
        "    columns:\n"
        "      score: {value: [{block: latest, from: {results: search}}, score]}\n"
        "      tries: {block: count, over: {results: search}}\n"
        "    display: [score]\n"
    )
    marked = [sub(1, score=0.9, marked=1), sub(2, score=0.5, marked=0)]
    rows = [(1, "r1", {"search": marked}), (2, "r2", {"search": [sub(1, score=0.4)]})]
    staff = read_as(text, "gradebook", rows) + read_as(text, "latest", rows)
    entrant = read_as(text, "gradebook", rows, Viewer(own=owner(1))) + read_as(
        text, "latest", rows, Viewer(own=owner(1))
    )

    # Only displayed cells count, `tries` among none, and only the task's.
    assert counted_in(staff, "search") == {
        owner(1): {
            1: (("gradebook", "search_mark"), ("gradebook", "total")),
            2: (("latest", "score"),),
        },
        owner(2): {1: (("gradebook", "search_mark"), ("gradebook", "total"), ("latest", "score"))},
    }
    assert counted_in(staff, "cartpole") == {}
    # A row whose counts are not told counts nothing.
    assert counted_in(entrant, "search") == {
        owner(1): {
            1: (("gradebook", "search_mark"), ("gradebook", "total")),
            2: (("latest", "score"),),
        }
    }


def test_a_hidden_cell_counts_nothing() -> None:
    text = "tasks: [{id: a}]\nleaderboards:\n  s:\n    audience: {who: everyone}\n" + (
        "    columns:\n"
        "      best:\n"
        "        value: [{block: highest, from: {results: a}, by: {value: [a, private]}}, x]\n"
        "    display: [best]\n"
    )
    rows = [(1, "r1", {"a": [hidden_sub(1, "private", x=1), hidden_sub(2, "private", x=2)]})]

    staff = [(1, "r1", {"a": [sub(1, private=5, x=1), sub(2, private=3, x=2)]})]

    entrant = read_as(text, "s", rows, Viewer(own=owner(1)))
    assert entrant[0].rows[0].cells[0].hidden
    assert counted_in(entrant, "a") == {}
    assert counted_in(read_as(text, "s", staff), "a") == {owner(1): {1: (("s", "best"),)}}


@pytest.mark.parametrize(("number", "served"), [(1, True), (2, False), (3, False)])
def test_an_entry_shows_only_once_a_shown_cell_counted_it(number: int, served: bool) -> None:
    # 1 a shown cell counted, 2 only a hidden or staff's cell, 3 no cell:
    # neither opens, after the reveal too, so the one with no mark tells
    # nothing of which a hidden cell chose.
    assert entries_open(number, {1}) is served


def test_a_cell_is_an_exact_fraction() -> None:
    # Guarantee 4.
    text = (
        "tasks: [{id: a}]\nleaderboards:\n  s:\n    audience: {who: everyone}\n"
        "    columns:\n      third: {block: div, of: [{block: count, over: {results: a}}, 3]}\n"
        "    display: [third]\n"
    )
    found = one(read_as(text, "s", [(1, "r1", {"a": [sub(1)]})], Viewer()))

    assert found.rows[0].cells[0].number == Fraction(1, 3)


THROUGH_PUBLIC = """tasks: [{id: k}]
leaderboards:
  public:
    audience: {who: everyone}
    columns:
      last: {block: latest, from: {results: k}}
      score: {value: [last, public]}
    sort: [{score: desc}]
    display: [score]
  staffs:
    columns:
      theirs: {column: [public, last]}
      secret: {value: [theirs, private]}
    display: [secret]
"""
"""A leaderboard for everyone reading its latest submission's public score, and
staff's reading the private score through that pick."""


def test_a_staff_leaderboard_reading_through_a_public_leaderboards_pick_hides_nothing_of_it() -> (
    None
):
    # Item 98: only what the leaderboards read for a viewer read through a pick
    # hides it, so staff's private read hides no public cell, while the
    # pick takes the same submission on every leaderboard.
    settings = contest(THROUGH_PUBLIC)
    found = checked(settings)
    assert not found.problems, found.problems
    lists = [{"k": [kaggle(1, 80, 70, shown=False)]}, {"k": [kaggle(1, 70, 60, shown=False)]}]

    def public(leaderboards: LeaderboardsCheck) -> LeaderboardView:
        worked = [
            Row(
                owner(entrant),
                f"r{entrant}",
                work_out_row(settings.leaderboards, leaderboards, each, {}),
            )
            for entrant, each in enumerate(lists, 1)
        ]
        return one(
            read_leaderboards(
                settings.leaderboards,
                leaderboards,
                ["public"],
                worked,
                Viewer(),
                released=["k"],
                headers={},
            )
        )

    seen = public(found.needed(leaderboards_read(found, ["public"])))
    assert found.through[("public", "last")] == {"public", "private"}
    assert seen.dropped == ()
    assert [(row.name, row.place, row.cells[0].number) for row in seen.rows] == [
        ("r1", 1, Fraction(80)),
        ("r2", 2, Fraction(70)),
    ]
    # Read with staff's leaderboard worked out too, its private read hides the pick.
    assert public(found).dropped == ("score",)
    # Once private is listed, a newest submission whose private still waits
    # is passed over on the public leaderboard as on staff's.
    newest = results_seen(
        2,
        {},
        [ValueShown("public", Fraction(90), False, False)],
        1,
        ["public", "private"],
        ["public", "private"],
        True,
    )
    lists[0] = {"k": [kaggle(1, 80, 70, shown=True), newest]}
    assert public(found.needed(leaderboards_read(found, ["public"]))).rows[0].cells[0].number == 80


def test_a_hidden_value_still_waiting_changes_no_cell_reading_shown_ones() -> None:
    # Guarantee 5.
    lists = {"k": [kaggle(1, 80, 70, shown=False), sub(2, public=90, waiting=("private",))]}
    found = one(read_as(KAGGLE, "public", [(1, "r1", lists)], Viewer()))

    assert found.rows[0].cells[0].number == 90


def test_waiting_is_told_only_to_the_rows_own_entrant_and_staff() -> None:
    # Guarantee 6.
    lists = {"cartpole": [sub(1, reward=10), sub(2, waiting=("reward",))]}
    rows = [(1, "r1", lists), (2, "r2", lists)]
    entrant = one(read_as(GRADEBOOK, "cartpole_ranking", rows, Viewer(own=owner(1))))
    staff = one(read_as(GRADEBOOK, "cartpole_ranking", rows))

    mine, other = entrant.rows
    assert mine.told and mine.cells[0].waiting == (Counted("cartpole", 2),)
    assert mine.cells[0].counted == (Counted("cartpole", 1),)
    assert not other.told and other.cells == (LeaderboardCell("reward", Fraction(10)),)
    assert all(row.cells[0].waiting == (Counted("cartpole", 2),) for row in staff.rows)


def test_the_own_row_and_staff_are_told_how_many_submissions_still_grade() -> None:
    # Plan step 8: a count with no where reads no value, so no cell says
    # a submission waits; the row's count of those still grading does, over
    # the tasks the leaderboard reads.
    text = (
        "tasks: [{id: a}, {id: b}]\nleaderboards:\n  s:\n    audience: {who: everyone}\n"
        "    columns:\n      tries: {block: count, over: {results: a}}\n    display: [tries]\n"
    )
    lists = {"a": [sub(1, score=1), sub(2, waiting=("score",))], "b": [sub(1, waiting=("x",))]}
    rows = [(1, "r1", lists), (2, "r2", lists)]
    entrant = one(read_as(text, "s", rows, Viewer(own=owner(1))))
    staff = one(read_as(text, "s", rows))

    assert [(row.grading, row.cells[0].waiting) for row in entrant.rows] == [(1, ()), (0, ())]
    assert [row.grading for row in staff.rows] == [1, 1]


def test_a_column_reading_only_tasks_not_released_is_not_listed() -> None:
    # Guarantee 8: neither its header nor its cells tell of the task.
    text = GRADEBOOK.replace(
        "    sort: [{reward: desc}]\n    display: [reward]\n",
        "      tries: 3\n    sort: [{reward: desc}]\n    display: [reward, tries]\n",
    )
    rows = [rewards(1, 10)]
    seen = one(read_as(text, "cartpole_ranking", rows, Viewer(), released=["search"]))
    staff = one(read_as(text, "cartpole_ranking", rows, released=["search"]))

    assert [header.name for header in seen.headers] == ["tries"]
    assert [cell.column for cell in seen.rows[0].cells] == ["tries"]
    assert seen.dropped == ("reward",)
    assert [header.name for header in staff.headers] == ["reward", "tries"]


HOLD_FROM = datetime(2026, 6, 1, 13, tzinfo=UTC)


def made(hour: int, minute: int, results: Submitted) -> Loaded:
    return Loaded(datetime(2026, 6, 1, hour, minute, tzinfo=UTC), results)


def test_the_hold_keeps_what_was_made_before_from_and_counts_the_rest_held() -> None:
    # Guarantee 9: others read the row as it stood at `from`, told how
    # many it holds, compile errors among them; its own entrant reads it
    # whole.
    lists = {
        "search": [
            made(12, 0, sub(1, score=0.5, marked=1)),
            made(13, 5, sub(2, score=0.9, marked=0)),
            made(13, 10, sub(3, compiled=0, marked=0)),
        ]
    }
    open_text = GRADEBOOK.replace("{who: entrants, rows: own}", "{who: everyone}")
    held, counts = held_back(lists, HOLD_FROM, {})
    whole, none = held_back(lists, None, {})
    others = one(read_as(open_text, "gradebook", [(1, "r1", held)], Viewer(), held={1: counts}))
    own = one(read_as(open_text, "gradebook", [(1, "r1", whole)], Viewer(own=owner(1))))

    assert counts == {"search": 2} and none == {}
    assert others.rows[0].held == 2
    # The search mark reads `marked`, which may change under the hold.
    assert others.rows[0].cell("search_mark") == LeaderboardCell("search_mark", None)
    assert others.rows[0].cell("cartpole_best") == LeaderboardCell("cartpole_best", Fraction(0))
    assert own.rows[0].held == 0
    assert own.rows[0].cell("search_mark") == LeaderboardCell(
        "search_mark", Fraction(1, 2), (Counted("search", 1),)
    )


def test_a_rows_held_count_on_a_leaderboard_counts_only_the_tasks_it_reads() -> None:
    # Item 91: the ranking reads cartpole alone, so search's held
    # submissions are not counted on it.
    rows = [rewards(1, 10)]
    held = {1: {"cartpole": 1, "search": 2}}
    ranking = one(read_as(GRADEBOOK, "cartpole_ranking", rows, Viewer(), held=held))

    assert ranking.rows[0].held == 1


def test_the_hold_holds_from_its_from_until_its_until_or_an_early_end() -> None:
    by_time = Hold.model_validate(
        {"from": HOLD_FROM, "until": datetime(2026, 6, 1, 15, tzinfo=UTC)}
    )
    by_staff = Hold.model_validate({"from": HOLD_FROM, "until": "staff"})

    assert holding(by_time, datetime(2026, 6, 1, 12, 59, tzinfo=UTC), False) is None
    assert holding(by_time, HOLD_FROM, False) == HOLD_FROM
    assert holding(by_time, datetime(2026, 6, 1, 15, tzinfo=UTC), False) is None
    assert holding(by_staff, datetime(2026, 6, 2, tzinfo=UTC), False) == HOLD_FROM
    assert holding(by_staff, datetime(2026, 6, 2, tzinfo=UTC), True) is None
    assert holding(None, HOLD_FROM, False) is None


def test_the_gradebook_shows_an_entrant_only_their_own_row_with_no_place() -> None:
    rows = [rewards(1, 10), rewards(2, 20)]
    entrant = one(read_as(GRADEBOOK, "gradebook", rows, Viewer(own=owner(1))))
    staff = one(read_as(GRADEBOOK, "gradebook", rows))

    assert [(row.name, row.place) for row in entrant.rows] == [("r1", None)]
    assert read_as(GRADEBOOK, "gradebook", rows, Viewer()) == ()
    assert [(row.name, row.place) for row in staff.rows] == [("r2", 1), ("r1", 2)]


def test_top_3_keeps_the_rows_tied_at_3_and_the_viewers_own() -> None:
    text = GRADEBOOK.replace("audience: {who: everyone}", "audience: {who: everyone, rows: 3}")
    rows = [rewards(1, 50), rewards(2, 40), rewards(3, 30), rewards(4, 30), rewards(5, 20)]
    rows.append(rewards(6, 10))
    found = one(read_as(text, "cartpole_ranking", rows, Viewer(own=owner(6))))
    visitor = one(read_as(text, "cartpole_ranking", rows, Viewer()))

    assert [(row.name, row.place) for row in found.rows] == [
        ("r1", 1),
        ("r2", 2),
        ("r3", 3),
        ("r4", 3),
        ("r6", 6),
    ]
    assert [row.name for row in visitor.rows] == ["r1", "r2", "r3", "r4"]


def test_a_public_column_reading_the_gradebook_shows_only_on_the_viewers_own_row() -> None:
    # Rule 5: a column is shown no wider than the leaderboard it reads.
    text = GRADEBOOK + (
        "  mine:\n    audience: {who: everyone}\n"
        "    columns:\n      total: {column: [gradebook, total]}\n"
        "    display: [total]\n"
    )
    rows = [rewards(1, 475), rewards(2, 0)]
    entrant = one(read_as(text, "mine", rows, Viewer(own=owner(1))))
    staff = one(read_as(text, "mine", rows))

    assert entrant.rows[0].cells[0].number == Fraction(1, 2)
    assert entrant.rows[1].cells[0] == LeaderboardCell("total", None)
    assert [row.cells[0].number for row in staff.rows] == [Fraction(1, 2), Fraction(0)]


def test_leaderboards_reading_each_other_read_a_leaderboard_not_read_yet_as_serving_no_row() -> (
    None
):
    # Item 89: neither leaderboard can be read first, so the first in the file
    # reads the other's column hidden on every row; the second reads the
    # first once it is read.
    text = (
        "tasks: [{id: a}]\nleaderboards:\n"
        "  one:\n    audience: {who: everyone}\n"
        "    columns: {n: {block: count, over: {results: a}}, x: {column: [two, m]}}\n"
        "    display: [n, x]\n"
        "  two:\n    audience: {who: everyone}\n"
        "    columns: {m: {block: count, over: {results: a}}, y: {column: [one, n]}}\n"
        "    display: [m, y]\n"
    )
    rows = [(1, "r1", {"a": [sub(1)]})]
    first = one(read_as(text, "one", rows, Viewer()))
    second = one(read_as(text, "two", rows, Viewer()))
    staff = one(read_as(text, "one", rows))

    assert first.rows[0].cells == (LeaderboardCell("n", Fraction(1)), LeaderboardCell("x", None))
    assert second.rows[0].cells == (
        LeaderboardCell("m", Fraction(1)),
        LeaderboardCell("y", Fraction(1)),
    )
    assert [cell.number for cell in staff.rows[0].cells] == [1, 1]


def test_the_gradebooks_headers_carry_bounds_and_the_self_reported_mark_no_cell_does() -> None:
    settings = contest(GRADEBOOK)
    found = checked(settings)
    tasks = {
        "search": {"score": (Fraction(0), Fraction(1))},
        "cartpole": {"reward": (Fraction(0), Fraction(500))},
    }
    bounds = column_bounds(settings.leaderboards, found, tasks, {"search": 50, "cartpole": 50}, {})
    parts = self_reported_parts(found, {"search": frozenset(), "cartpole": frozenset({"reward"})})
    headers = headers_of("gradebook", settings.leaderboards["gradebook"], bounds, parts)
    worked = work_out_row(settings.leaderboards, found, {"cartpole": [sub(1, reward=475)]}, {})
    leaderboard = one(
        read_leaderboards(
            settings.leaderboards,
            found,
            ["gradebook"],
            [Row(owner(1), "r1", worked)],
            Viewer(own=owner(1)),
            released=["search", "cartpole"],
            headers={"gradebook": headers},
        )
    )

    assert leaderboard.headers == (
        Header("search_mark", None, Fraction(0), Fraction(1)),
        Header("cartpole_best", None, Fraction(0), Fraction(500), (("cartpole", "reward"),)),
        Header("total", 2, Fraction(0), Fraction(1), (("cartpole", "reward"),)),
    )
    assert "self_reported" not in LeaderboardCell.__dataclass_fields__


def test_a_header_names_no_self_reported_value_of_a_task_not_released() -> None:
    # Rule 8: a column over a released task and one not yet released keeps
    # its header, which names no value of the task not released.
    text = (
        "tasks: [{id: a}, {id: b}]\nleaderboards:\n  s:\n    audience: {who: everyone}\n"
        "    columns:\n      both:\n        block: add\n        of:\n"
        "          - {block: max, over: {results: a}, of: {value: [a, score]}}\n"
        "          - {block: max, over: {results: b}, of: {value: [b, reward]}}\n"
        "    display: [both]\n"
    )
    settings = contest(text)
    found = checked(settings)
    parts = self_reported_parts(found, {"a": frozenset({"score"}), "b": frozenset({"reward"})})
    headers = {"s": headers_of("s", settings.leaderboards["s"], {}, parts)}
    worked = [Row(owner(1), "r1", work_out_row(settings.leaderboards, found, {}, {}))]

    def header(viewer: Viewer) -> Header:
        leaderboard = one(
            read_leaderboards(
                settings.leaderboards, found, ["s"], worked, viewer, released=["a"], headers=headers
            )
        )
        return leaderboard.headers[0]

    assert header(Viewer()).self_reported == (("a", "score"),)
    assert header(STAFF).self_reported == (("a", "score"), ("b", "reward"))


def test_a_text_column_is_shown_and_not_sorted_folded_or_given_places() -> None:
    assert column("p", "medal", {}, {"text": "gold"}).text == "gold"
    submitted = Submitted(1, texts={"medal": "gold"})
    shown = column(
        "p",
        "medal",
        {"a": [submitted]},
        {"value": [{"block": "latest", "from": {"results": "a"}}, "medal"]},
    )
    assert shown.text == "gold"

    def messages(text: str) -> str:
        return " ".join(message for _, message in problems(text))

    body = "tasks: [{id: a}]\nleaderboards:\n  s:\n    columns:\n      medal: {text: gold}\n"
    assert "sorts on numbers" in messages(
        body + "    sort: [{medal: desc}]\n    display: [medal]\n"
    )
    assert "decimal places" in messages(body + "    display: [{medal: {places: 2}}]\n")
    folded = messages(
        body + "      more: {block: add, of: [{column: medal}, 1]}\n" + "    display: [medal]\n"
    )
    assert "is text" in folded


def test_a_leaderboard_compares_a_text_value_to_text() -> None:
    medal = {"value": [{"block": "latest", "from": {"results": "a"}}, "medal"]}

    def gold(name: str, texts: dict[str, str]) -> Cell:
        tree = {
            "block": "if",
            "test": {"block": name, "of": [medal, {"text": "gold"}]},
            "then": 1,
            "else": 0,
        }
        return column("p", "gold", {"a": [Submitted(1, texts=texts)]}, tree)

    assert gold("eq", {"medal": "gold"}).number == 1
    assert gold("eq", {"medal": "silver"}).number == 0
    assert gold("ne", {"medal": "silver"}).number == 1
    assert gold("eq", {}).nothing


# Each leaderboard block


def test_every_leaderboard_block_has_a_test() -> None:
    # otherwise is a task value's too (item 128), its leaderboard meaning tested
    # in test_otherwise_and_sum_best_and_missing_spreading.
    leaderboard = {
        name
        for name, block in CATALOGUE.items()
        if any(form.scope is Scope.LEADERBOARD for form in block.forms)
    }
    assert leaderboard == {
        "first",
        "latest",
        "highest",
        "lowest",
        "best_each",
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


def test_a_missing_by_makes_a_pick_missing_unless_its_missing_says_otherwise() -> None:
    # Item 127: one missing state passes through, the way round written.
    highest = {"block": "highest", "from": results("a"), "by": read("a", "score")}
    skip = {**highest, "missing": "skip"}
    as_ten = {**highest, "missing": 10}
    listed = {"a": [sub(1, score=3), sub(2)]}

    missing = column("p", "c", listed, read(highest, "score"))
    skipped = column("p", "c", listed, read(skip, "score"))
    ten = column("p", "c", listed, {"block": "if", "test": as_ten, "then": 1, "else": 0})
    tested = column("p", "c", listed, {"block": "if", "test": highest, "then": 1, "else": 0})
    test = column("p", "c", {}, {"block": "if", "test": highest, "then": 1, "else": 0})

    # The pick counts the submission that made it missing.
    assert (missing.number, missing.counted) == (None, frozenset({("a", 2)}))
    assert (skipped.number, skipped.counted) == (Fraction(3), frozenset({("a", 1)}))
    assert (ten.number, ten.counted) == (Fraction(1), frozenset({("a", 2)}))
    # A missing pick tested is missing; a pick over nothing is none, so as
    # a test it is false.
    assert tested.number is None
    assert test.number == 0


def test_count_over_nothing_is_0_and_a_missing_input_makes_a_fold_missing() -> None:
    count = {"block": "count", "over": results("a")}
    where = {**count, "where": read("a", "score")}

    listed = {"a": [sub(1, score=2), sub(2)]}
    assert column("p", "c", {}, count).number == 0
    # Item 127: a missing where makes the count missing, counting the
    # submission, unless missing says to leave it out or what it counts as.
    missing = column("p", "c", listed, where)
    assert missing.number is None
    assert missing.counted == {("a", 1), ("a", 2)}
    assert column("p", "c", listed, {**where, "missing": "skip"}).number == 1
    assert column("p", "c", listed, {**where, "missing": 0}).number == 1
    for block, skipped, two in (("sum", 2, 4), ("max", 2, 2), ("min", 2, 2)):
        tree = {"block": block, "over": results("a"), "of": read("a", "score")}
        assert column("p", "c", {"a": []}, tree).number is None
        found = column("p", "c", listed, tree)
        assert found.number is None
        assert found.counted == {("a", 1), ("a", 2)}
        assert column("p", "c", listed, {**tree, "missing": "skip"}).number == skipped
        assert column("p", "c", listed, {**tree, "missing": 2}).number == two


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
    leaderboards = {"p": Leaderboard.model_validate({"columns": {"c": tree}})}
    found = check_leaderboards(leaderboards, ["a"])
    cell = work_out_row(leaderboards, found, listed, settings_trees)[("p", "c")]
    # A value reading a run directly cannot be worked out again.
    runs = work_out_row(
        leaderboards, found, listed, {"a": {**ISLANDS_VALUES, "total": ISLANDS_VALUES["st1"]}}
    )

    assert cell.number == 100
    assert cell.counted == {("a", 1), ("a", 2)}
    assert runs[("p", "c")].number is None


def test_a_missing_input_makes_best_each_missing_unless_its_missing_says_otherwise() -> None:
    # Item 127: submission 1 has no st2, so the best of st2 is unknown.
    tree = best_each("total")
    listed = {"a": [sub(1, st1=1), sub(2, st1=0, st2=1)]}

    def cell(given: dict[str, object]) -> Cell:
        leaderboards = {"p": Leaderboard.model_validate({"columns": {"c": given}})}
        found = check_leaderboards(leaderboards, ["a"])
        return work_out_row(leaderboards, found, listed, {"a": ISLANDS_VALUES})[("p", "c")]

    missing = cell(tree)
    assert (missing.number, missing.counted) == (None, frozenset({("a", 1)}))
    assert cell({**tree, "missing": "skip"}).number == 100
    # 40 * 1 + 60 * 5, the missing st2 counted as 5.
    assert cell({**tree, "missing": 5}).number == 340


def test_otherwise_and_sum_best_and_missing_spreading() -> None:
    listed = {"a": [sub(1, score=4)], "b": [sub(1)]}
    a = {"block": "max", "over": results("a"), "of": read("a", "score")}
    b = {"block": "max", "over": results("b"), "of": read("b", "score")}

    assert column("p", "c", listed, {"block": "otherwise", "of": [b, 9]}).number == 9
    assert column("p", "c", listed, {"block": "otherwise", "of": [a, 9]}).number == 4
    assert column("p", "c", listed, {"block": "add", "of": [a, b]}).number is None
    assert column("p", "c", listed, {"block": "sum_best", "k": 2, "of": [a, 3, 1]}).number == 7
    assert column("p", "c", listed, {"block": "sum_best", "k": 2, "of": [a, b]}).number is None


def first_of(task: str, name: str) -> dict[str, object]:
    return {"block": "first", "from": results(task), "where": read(task, name)}


def test_a_missing_pick_stays_missing_through_otherwise_so_an_if_testing_it_is_missing() -> None:
    # a has no solved submission, so none; b's one submission has solved
    # missing, so the pick is missing, given as it is by otherwise.
    listed = {"a": [sub(1, solved=0)], "b": [sub(1)]}
    either = {"block": "otherwise", "of": [first_of("a", "solved"), first_of("b", "solved")]}
    direct = column(
        "p", "c", listed, {"block": "if", "test": first_of("b", "solved"), "then": 1, "else": 0}
    )
    through = column("p", "c", listed, {"block": "if", "test": either, "then": 1, "else": 0})

    assert direct.number is None and direct.counted == {("b", 1)}
    assert through.number is None and through.counted == {("b", 1)}
    assert column("p", "c", listed, either).missing


def test_otherwise_over_a_value_still_grading_leaves_its_submission_out() -> None:
    # A task's otherwise waits when its first waits (item 128); on a leaderboard
    # the block looking at the submission leaves it out, so it is never
    # both counted and waiting, and the fallback is never taken for it.
    listed = {"a": [sub(1, time=5, attempt=1), sub(2, attempt=2, waiting=("time",))]}
    fallback = {"block": "otherwise", "of": [read("a", "time"), 0]}
    least = column("p", "c", listed, {"block": "min", "over": results("a"), "of": fallback})
    lowest = column(
        "p", "c", listed, read({"block": "lowest", "from": results("a"), "by": fallback}, "attempt")
    )
    # A fold that left out every submission as waiting has no number, so
    # otherwise around it gives the fallback, as for a pick passing over
    # them: the submission is left out, not read.
    left_out = column(
        "p",
        "c",
        {"a": [sub(1, waiting=("time",))]},
        {
            "block": "otherwise",
            "of": [{"block": "min", "over": results("a"), "of": read("a", "time")}, 0],
        },
    )

    assert least.number == 5
    assert least.counted == {("a", 1)} and least.waiting == {("a", 2)}
    assert lowest.number == 1
    assert lowest.counted == {("a", 1)} and lowest.waiting == {("a", 2)}
    assert left_out.number == 0 and not left_out.counted and left_out.waiting == {("a", 1)}
    # So the grading queue and the entries list submission 1 alone.
    text = (
        "tasks: [{id: a}]\nleaderboards:\n  fastest:\n    audience: {who: everyone}\n"
        "    columns:\n"
        "      time: {block: min, over: {results: a}, of: {block: otherwise, of: "
        "[{value: [a, time]}, 0]}}\n"
        "    display: [time]\n"
    )
    assert counted_in(read_as(text, "fastest", [(1, "r1", listed)]), "a") == {
        owner(1): {1: (("fastest", "time"),)}
    }


def test_otherwise_over_a_value_held_back_leaves_its_submission_out_and_hides() -> None:
    listed = {"a": [sub(1, time=5), hidden_sub(2, "time")]}
    fallback = {"block": "otherwise", "of": [read("a", "time"), 0]}
    least = column("p", "c", listed, {"block": "min", "over": results("a"), "of": fallback})

    assert least.hidden
    assert least.number == 5 and least.counted == {("a", 1)}


# The leaderboard check


def test_the_three_leaderboards_of_simpler_core_10_pass_the_check() -> None:
    for text in (GRADEBOOK, OLYMPIAD, ICPC):
        assert problems(text) == []


def test_a_leaderboard_with_no_audience_is_staffs() -> None:
    settings = contest("tasks: [{id: a}]\nleaderboards:\n  standings: {}\n")

    assert settings.leaderboards["standings"].audience.who == "staff"
    assert settings.leaderboards["standings"].title_or("standings") == "standings"


def test_own_rows_for_everyone_and_old_leaderboards_are_refused_at_parse() -> None:
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


def leaderboard(columns: Mapping[str, object], **more: object) -> str:
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
        # Names shared with a task, a leaderboard, or alike once - is _.
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
        # missing neither skip nor a number.
        ({"c": {**COUNT, "missing": "foo"}}, {}, "leaderboards.s.columns.c.missing"),
        ({"p": {**PICK, "missing": True}}, {}, "leaderboards.s.columns.p.missing"),
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
def test_each_refusal_of_the_leaderboard_check(
    columns: dict[str, object], more: dict[str, object], path: str
) -> None:
    assert path in [each for each, _ in problems(leaderboard(columns, **more))]


def test_a_loop_across_leaderboards_and_a_column_another_leaderboard_lacks_are_refused() -> None:
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


def test_a_leaderboard_reading_a_refused_leaderboard_is_not_read() -> None:
    text = (
        "tasks: [{id: a}]\nleaderboards:\n"
        "  one: {columns: {x: {block: nope}}}\n"
        "  two: {columns: {y: {column: [one, x]}}}\n"
        "  three: {columns: {z: 1}}\n"
    )
    assert checked(contest(text)).readable == frozenset({"three"})


def test_a_formula_word_cannot_name_a_column_or_a_leaderboard() -> None:
    sentence = "is a word a formula reads as an operator, so it can't name a value."
    for word in UNREADABLE:
        column = [message for _, message in problems(leaderboard({word: 1}))]
        assert f"{word} {sentence} Choose another name." in column
        key = [
            message
            for _, message in problems(
                f'tasks: [{{id: a}}]\nleaderboards:\n  "{word}": {{columns: {{c: 1}}}}\n'
            )
        ]
        assert f"{word} {sentence} Choose another name." in key
    assert [
        message for _, message in problems(leaderboard({"n-ot": 1, "index": 1, "total_in": 1}))
    ] == []
    kept = problems("tasks: [{id: a}]\nleaderboards:\n  n-ot: {columns: {c: 1}}\n")
    assert [message for _, message in kept if "operator" in message] == []


def test_a_leaderboard_of_too_many_columns_is_refused() -> None:
    columns = {f"c{index}": 1 for index in range(MOST_COLUMNS + 1)}
    assert [path for path, _ in problems(leaderboard(columns))] == ["leaderboards.s.columns"]


def test_a_contest_of_more_than_20_leaderboards_is_refused() -> None:
    leaderboards = {
        f"b{index}": Leaderboard(columns={"c": 1}) for index in range(MOST_LEADERBOARDS)
    }
    assert MOST_LEADERBOARDS == 20
    assert check_leaderboards(leaderboards, []).problems == []
    leaderboards["more"] = Leaderboard(columns={"c": 1})
    assert check_leaderboards(leaderboards, []).problems == [
        (
            ("leaderboards",),
            "This contest has 21 leaderboards. A contest has at most 20, so working them out "
            "stays quick.",
        )
    ]


def test_a_task_value_using_a_leaderboard_block_or_leaf_is_refused() -> None:
    for tree in (
        {"block": "first", "from": results("a")},
        {"block": "max", "over": results("a"), "of": 1},
        {"column": "x"},
        {"value": ["a", "x"]},
    ):
        found = check_values({"v": tree}, groups=[], columns={}, keys={})
        assert found.problems, tree


def test_marks_go_with_a_leaderboard_reading_marked() -> None:
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


def test_anyone_but_staff_knows_a_values_bounds_only_from_what_they_are_shown() -> None:
    # Item 126: a value listed now that counts the runs of st2_tests, a
    # group no entry shows until the reveal, would tell its size, 2.
    text = ISLANDS_TASK.replace(
        "    total:\n", "    hard: {block: count, over: {runs: st2_tests}}\n    total:\n"
    ).replace("now: [sample_score,", "now: [hard, sample_score,")
    scoring = parse_task(text).scoring
    assert scoring is not None
    runs = [
        RunResult(f"r{n}", "", {"input": path})
        for n, path in enumerate(["samples/1.in", "samples/2.in", "tests/3.in", "tests/4.in"], 1)
    ]
    groups = {
        "all": runs,
        "sample_tests": runs[:2],
        "st1_tests": runs[2:3],
        "st2_tests": runs[2:],
    }
    columns = {"score": Column(Better.HIGHER, Fraction(0), Fraction(1))}

    staff = value_bounds(scoring.values, groups, columns)
    during = seen_bounds(scoring, groups, columns, ("now",))
    after = seen_bounds(scoring, groups, columns, ("now", "after_close"))

    two, hundred = (Fraction(2), Fraction(2)), (Fraction(0), Fraction(100))
    assert (staff["hard"], staff["total"]) == (two, hundred)
    # During the contest: sample_score's bounds read only sample_tests,
    # shown now; hard's read a hidden group, and total is not listed yet.
    assert during == {"hard": None, "sample_score": (Fraction(0), Fraction(1)), "sample_time": None}
    # From the reveal `all` shows every run, so every group is known.
    assert (after["hard"], after["total"]) == (two, hundred)


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
    bounds = column_bounds(settings.leaderboards, found, tasks, {"search": 50, "cartpole": 50}, {})

    assert bounds[("gradebook", "search_mark")] == (Fraction(0), Fraction(1))
    assert bounds[("gradebook", "cartpole_best")] == (Fraction(0), Fraction(500))
    assert bounds[("gradebook", "total")] == (Fraction(0), Fraction(1))
    assert bounds[("gradebook", "late")] is None


def test_a_division_by_bounds_holding_0_and_a_fold_over_results_by_submissions() -> None:
    settings = contest(
        leaderboard(
            {
                "n": COUNT,
                "sm": {"block": "sum", "over": results("a"), "of": read("a", "score")},
                "d": {"block": "div", "of": [1, {"column": "n"}]},
            }
        )
    )
    found = checked(settings)
    bounds = column_bounds(
        settings.leaderboards, found, {"a": {"score": (Fraction(0), Fraction(2))}}, {"a": 3}, {}
    )

    assert bounds[("s", "n")] == (Fraction(0), Fraction(3))
    assert bounds[("s", "sm")] == (Fraction(0), Fraction(6))
    assert bounds[("s", "d")] is None


def test_a_folds_bounds_over_results_widen_by_its_missing_number() -> None:
    fold = {"block": "max", "over": results("a"), "of": read("a", "score")}
    settings = contest(
        leaderboard({"wide": {**fold, "missing": 5}, "low": {**fold, "missing": -1}, "plain": fold})
    )
    found = checked(settings)
    bounds = column_bounds(
        settings.leaderboards, found, {"a": {"score": (Fraction(0), Fraction(2))}}, {"a": 3}, {}
    )

    assert bounds[("s", "wide")] == (Fraction(0), Fraction(5))
    assert bounds[("s", "low")] == (Fraction(-1), Fraction(2))
    assert bounds[("s", "plain")] == (Fraction(0), Fraction(2))


def test_best_eachs_bounds_are_worked_out_again_from_each_read_widened_by_its_missing() -> None:
    tree = best_each("total")
    settings = contest(
        leaderboard({"wide": {**tree, "missing": 5}, "skip": {**tree, "missing": "skip"}})
    )
    found = checked(settings)
    one = (Fraction(0), Fraction(1))
    tasks = {"a": {"st1": one, "st2": one, "total": (Fraction(0), Fraction(100))}}

    def bounds(given: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], Any]:
        return column_bounds(settings.leaderboards, found, given, {"a": 3}, {"a": ISLANDS_VALUES})

    # A missing st2 counted as 5 gives 40 * 1 + 60 * 5 = 340, within them.
    assert bounds(tasks)[("s", "wide")] == (Fraction(0), Fraction(500))
    assert bounds(tasks)[("s", "skip")] == (Fraction(0), Fraction(100))
    # A value read with no bounds, such as one not shown, gives none.
    assert bounds({"a": {"st1": one, "total": (Fraction(0), Fraction(100))}})[("s", "wide")] is None


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


def _hold(from_: str, start: str = "09:00", end: str = "14:00") -> ContestDefinition:
    head = HEAD.replace("09:00", start).replace("14:00", end)
    return parse_contest(head + f"hold: {{from: 2026-06-01T{from_}:00Z, until: staff}}\n")


EARLY = datetime(2026, 6, 1, 7, tzinfo=UTC)


@pytest.mark.parametrize("from_", ["08:00", "09:00", "14:00", "14:30"])
def test_a_hold_from_outside_the_contest_is_refused_at_its_from(from_: str) -> None:
    found = hold_problems(None, _hold(from_), EARLY)

    assert [path for path, _ in found] == [("hold", "from")]
    assert "after the contest's start" in found[0][1]


def test_moving_the_end_or_the_start_past_the_holds_from_is_refused_naming_the_hold() -> None:
    before = _hold("13:00")

    ended = hold_problems(before, _hold("13:00", end="12:30"), EARLY)
    started = hold_problems(before, _hold("13:00", start="13:30"), EARLY)

    assert ended == [
        (
            ("end",),
            "The leaderboards' hold starts on 1 June 2026 at 13:00 UTC, so the contest must end "
            "after it. Move the hold's from first, or take the hold out.",
        )
    ]
    assert [path for path, _ in started] == [("start",)]
    assert hold_problems(before, _hold("13:00", end="16:00"), EARLY) == []


def test_a_hold_set_or_moved_to_a_time_passed_is_refused_and_one_left_as_it_was_is_not() -> None:
    now = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    standing = _hold("12:00")

    assert hold_problems(standing, _hold("12:00"), now) == []
    assert hold_problems(standing, _hold("13:00"), now) == []
    # A from at this very moment has passed too.
    assert [path for path, _ in hold_problems(None, _hold("12:30"), now)] == [("hold", "from")]
    for before in (None, contest(""), _hold("13:00")):
        found = hold_problems(before, _hold("12:00"), now)
        assert found == [
            (
                ("hold", "from"),
                "1 June 2026 at 12:00 UTC has passed, so a hold from then would hide submissions "
                "others have already seen on the leaderboards. Give a time still to come.",
            )
        ]


# Across files

MARKED_VALUES: dict[str, object] = {
    **ISLANDS_VALUES,
    "review": {"block": "entered", "at_least": 0, "at_most": 10},
    "graded": {"block": "add", "of": [{"value": "total"}, {"value": "review"}]},
    "late": {"block": "sub", "of": [{"value": "total"}, {"fact": "seconds_late"}]},
    "fast": {"block": "min", "over": {"runs": "all"}, "of": {"run": ["all", "time_ms"]}},
}
"""Task a's values: islands' and a mark, one reading it, one reading a fact
and one folding over runs."""

SCORED = {"a": Scored(MARKED_VALUES)}


def across_problems(columns: Mapping[str, object]) -> list[tuple[str, str]]:
    found, _ = check_contest(contest(leaderboard(columns)), SCORED)
    return [(".".join(str(part) for part in path), message) for path, message in found]


def best_each(value: str, **directions: str) -> dict[str, object]:
    return {
        "block": "best_each",
        "from": results("a"),
        "value": {"name": value},
        "directions": directions or {"st1": "desc", "st2": "desc"},
    }


LATEST = {"block": "latest", "from": results("a")}


@pytest.mark.parametrize(
    ("columns", "path", "words"),
    [
        # A name a task does not give, read in a where, through a pick
        # column or through a pick in place.
        (
            {"c": {"block": "count", "over": results("a"), "where": read("a", "nope")}},
            "leaderboards.s.columns.c.where.value.1",
            "a has no value called nope. Its values are fast, graded, late, review, st1, st2 "
            "and total. A leaderboard also reads the facts attempt, seconds, seconds_late, "
            "marked and compiled.",
        ),
        ({"p": PICK, "c": read("p", "nope")}, "leaderboards.s.columns.c.value.1", "nope"),
        ({"c": read(LATEST, "nope")}, "leaderboards.s.columns.c.value.1", "nope"),
        # Through a pick column read as a column, and through otherwise of
        # picks.
        (
            {"p": PICK, "c": read({"column": "p"}, "nope")},
            "leaderboards.s.columns.c.value.1",
            "nope",
        ),
        (
            {"c": read({"block": "otherwise", "of": [LATEST, PICK]}, "nope")},
            "leaderboards.s.columns.c.value.1",
            "nope",
        ),
        # A choice reading a mark: a pick's by and where, directly, through
        # a value or through columns; a fold's where; max's, min's and sum's
        # of.
        (
            {"p": {"block": "highest", "from": results("a"), "by": read("a", "review")}},
            "leaderboards.s.columns.p.by",
            "highest chooses which submission of a counts by review, which reads a mark staff "
            "enter, so the grading queue, which lists the submissions each cell counts for "
            "marking, would wait on the mark itself. Choose by a value that reads no mark.",
        ),
        (
            {
                "p": {
                    "block": "latest",
                    "from": results("a"),
                    "where": {"block": "gt", "of": [read("a", "graded"), 5]},
                }
            },
            "leaderboards.s.columns.p.where",
            "by graded",
        ),
        (
            {
                "g": {"block": "otherwise", "of": [read(LATEST, "graded"), 0]},
                "p": {
                    "block": "first",
                    "from": results("a"),
                    "where": {"block": "gt", "of": [{"column": "g"}, 1]},
                },
            },
            "leaderboards.s.columns.p.where",
            "by graded",
        ),
        # Through a column reading another column that reads the mark.
        (
            {
                "g": {"block": "otherwise", "of": [read(LATEST, "graded"), 0]},
                "h": {"column": "g"},
                "p": {
                    "block": "first",
                    "from": results("a"),
                    "where": {"block": "gt", "of": [{"column": "h"}, 1]},
                },
            },
            "leaderboards.s.columns.p.where",
            "by graded",
        ),
        (
            {"c": {"block": "count", "over": results("a"), "where": read("a", "review")}},
            "leaderboards.s.columns.c.where",
            "count chooses",
        ),
        (
            {"c": {"block": "max", "over": results("a"), "of": read("a", "review")}},
            "leaderboards.s.columns.c.of",
            "max chooses",
        ),
        (
            {"c": {"block": "min", "over": results("a"), "of": read("a", "review")}},
            "leaderboards.s.columns.c.of",
            "min chooses",
        ),
        # A sum told to skip a submission whose of is missing would never
        # count one not yet marked (item 127).
        (
            {
                "c": {
                    "block": "sum",
                    "over": results("a"),
                    "of": read("a", "review"),
                    "missing": "skip",
                }
            },
            "leaderboards.s.columns.c.of",
            "sum chooses",
        ),
        (
            {
                "c": {
                    "block": "sum",
                    "over": results("a"),
                    "of": read("a", "total"),
                    "where": read("a", "review"),
                }
            },
            "leaderboards.s.columns.c.where",
            "sum chooses",
        ),
        # best_each: a value reading a fact, runs, or a mark directly; a
        # direction left out; a value reading a mark through what it reads;
        # names the task does not give.
        (
            {"c": best_each("late", total="desc")},
            "leaderboards.s.columns.c.value",
            "best_each works late out again from the best of each value it reads directly, "
            "but late reads the fact seconds_late directly, so no one best of it would be "
            "right. Give best_each a value that reads only other values and numbers.",
        ),
        ({"c": best_each("fast")}, "leaderboards.s.columns.c.value", "a group's runs"),
        ({"c": best_each("review")}, "leaderboards.s.columns.c.value", "a mark staff enter"),
        (
            {"c": best_each("total", st1="desc")},
            "leaderboards.s.columns.c.directions",
            "total reads st2 directly, so give each of them a direction here, desc for higher "
            "is better or asc for lower.",
        ),
        (
            {"c": best_each("graded", total="desc", review="desc")},
            "leaderboards.s.columns.c.value",
            "and review reads a mark staff enter",
        ),
        ({"c": best_each("nope")}, "leaderboards.s.columns.c.value", "no value called nope"),
        (
            {"c": best_each("total", st1="desc", st2="desc", zz="asc")},
            "leaderboards.s.columns.c.directions.zz",
            "no value called zz",
        ),
    ],
)
def test_each_refusal_across_files(columns: dict[str, object], path: str, words: str) -> None:
    found = across_problems(columns)

    assert [each for each, _ in found] == [path]
    assert words in found[0][1]


def test_best_each_refuses_a_value_using_otherwise_by_its_name_and_works_none_out() -> None:
    fallback = {"block": "otherwise", "of": [{"value": "st2"}, 0]}
    values = {
        **ISLANDS_VALUES,
        "kept": {
            "block": "add",
            "of": [
                {"block": "mul", "of": [10, fallback]},
                {"block": "mul", "of": [40, {"value": "st1"}]},
                {"block": "mul", "of": [60, {"value": "st2"}]},
            ],
        },
    }
    text = leaderboard({"c": best_each("kept")})
    found, _ = check_contest(contest(text), {"a": Scored(values)})
    listed = {"a": [sub(1, st1=1, st2=0.5), sub(2, st1=0.5, st2=1)]}

    assert [(".".join(str(part) for part in path), message) for path, message in found] == [
        (
            "leaderboards.s.columns.c.value",
            "best_each works kept out again from the best of each value it reads directly, "
            "but kept reads a fallback for a missing number, through otherwise directly, so "
            "no one best of it would be right. Give best_each a value that reads only other "
            "values and numbers.",
        )
    ]
    # A version still to be checked gets no number on read either.
    assert cells(text, listed, {"a": values})[("s", "c")].number is None


def test_what_reads_a_mark_without_choosing_by_it_and_a_task_not_resolved_pass() -> None:
    marked_pick = {"block": "latest", "from": results("a"), "where": read("a", "marked")}
    columns = {
        # A mark read on the latest submission, which counts while unmarked.
        "reviewed": {"block": "otherwise", "of": [read(LATEST, "review"), 0]},
        # A mark read through a pick chosen by marked, as the gradebook does.
        "picked": {"block": "otherwise", "of": [read(marked_pick, "graded"), 0]},
        "best": best_each("total"),
        # A sum of marks, missing while one is still to come, every
        # submission counted (item 127).
        "marks": {"block": "sum", "over": results("a"), "of": read("a", "review")},
        # Facts of any task, and b-c, whose live publication is not known.
        "tries": {"block": "count", "over": results("a"), "where": read("a", "compiled")},
        "other": read({"block": "first", "from": results("b-c")}, "anything"),
    }

    assert across_problems(columns) == []


def test_a_leaderboard_whose_own_check_fails_is_not_checked_across_files() -> None:
    columns = {"c": read(LATEST, "nope"), "d": {"block": "nope"}}

    assert [path for path, _ in across_problems(columns)] == ["leaderboards.s.columns.d.block"]


def test_a_refusal_across_files_says_at_a_tasks_save_which_leaderboard_and_column_it_breaks() -> (
    None
):
    settings = contest(leaderboard({"c": {"block": "otherwise", "of": [read(LATEST, "total"), 0]}}))
    values = {name: tree for name, tree in MARKED_VALUES.items() if name != "total"}

    [found] = across(settings, checked(settings), {"a": Scored(values)})

    assert (found.leaderboard, found.column, found.task, found.value) == ("s", "c", "a", "total")
    assert found.saved == (
        "The leaderboard s reads total of a in its column c, so a must keep a value called "
        "total. Take it out of that column first."
    )
