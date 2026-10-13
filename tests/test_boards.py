"""Boards computed on read, and what a board asks of the tasks it covers,
with the numbers of TASK-FORMAT.md section 3.7's worked examples.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from forge.domain.board_checks import (
    breaches,
    counts_nothing,
    counts_nothing_message,
    covered,
    group_max,
)
from forge.domain.boards import (
    Audience,
    Column,
    Entry,
    Key,
    NotInView,
    Row,
    State,
    for_viewer,
    keys_of,
    rank,
    sees,
    task_points,
)
from forge.domain.definitions import Group, Leaderboard, Relative, Select, Show
from forge.domain.meanings import ValueMeaning
from forge.domain.names import EntrantOwner
from forge.domain.scoring import Better, Measure, Points, Scored, score
from forge.domain.workflow_definition import Fold

START = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
CLOSE = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)


def at(hours: int, minutes: int) -> datetime:
    return START.replace(hour=hours, minute=minutes)


def minute(n: int) -> datetime:
    return START + timedelta(minutes=n)


def group(**rules: Any) -> Group:
    return Group.model_validate(
        {("pass" if key == "pass_" else key): value for key, value in rules.items()}
    )


def named(name: str, count: int) -> list[str]:
    return [f"{name}/{number}" for number in range(1, count + 1)]


def board(**fields: Any) -> Leaderboard:
    return Leaderboard.model_validate({"name": "Standings", **fields})


def column(
    name: str,
    groups: Mapping[str, Group],
    *,
    worth: int | None = 100,
    revealed: bool = False,
    released: bool = True,
) -> Column:
    return Column(
        name=name,
        label=name,
        worth=Fraction(worth) if worth is not None else None,
        shows={key: value.shown for key, value in groups.items()},
        release_at=START,
        released=released,
        reveal_at=CLOSE,
        revealed=revealed,
    )


def graded(
    ids: Sequence[str],
    groups: Mapping[str, Group],
    failing: Sequence[str] = (),
    *,
    worth: int | None = 100,
    values: Mapping[str, Mapping[str, str]] | None = None,
    measures: Mapping[str, Measure] | None = None,
) -> Scored:
    rows = []
    for test in ids:
        found = {name: Decimal(by[test]) for name, by in (values or {}).items() if test in by}
        outcome = "wrong_answer" if test in failing else "accepted"
        rows.append({"test": test, "outcome": outcome, "values": found})
    return score({"tests": rows}, ids, groups, None, measures or {}, worth=worth)


def candidate(number: int, when: datetime, scored: Scored, *, marked: bool = False) -> Entry:
    return Entry(number, when, State.CANDIDATE, scored, marked)


def owner(user: int) -> EntrantOwner:
    """The entrant a test names by the number of its one member."""
    return EntrantOwner(uuid.UUID(int=user))


def row(user: int, name: str, **entries: Sequence[Entry]) -> Row:
    return Row(
        owner(user),
        name,
        {task.replace("_", "-"): tuple(e) for task, e in entries.items()},
    )


def standings(
    on: Leaderboard,
    columns: Sequence[Column],
    rows: Sequence[Row],
    *,
    final: bool = False,
    directions: Mapping[str, Better | None] | None = None,
) -> Any:
    return rank(on, keys_of(on, directions or {}), START, columns, rows, final=final)


IOI_TESTS = [*named("samples", 2), *named("small", 10), *named("large", 20)]


def ioi(show: Show = Show.ALWAYS) -> dict[str, Group]:
    return {
        "samples": group(),
        "small": group(pass_=30, show=show),
        "large": group(pass_=70, show=show),
    }


def test_ioi_rows_are_100_and_70() -> None:
    groups = ioi()
    ana = row(1, "ana", islands=[candidate(1, at(11, 10), graded(IOI_TESTS, groups))])
    bo = row(2, "bo", islands=[candidate(1, at(11, 0), graded(IOI_TESTS, groups, ["small/4"]))])
    found = standings(board(), [column("islands", groups)], [bo, ana])
    assert [(r.name, r.rank, r.keys) for r in found.rows] == [("ana", 1, (100,)), ("bo", 2, (70,))]


def test_hidden_subtasks_leave_every_row_at_0_and_say_when_they_join() -> None:
    groups = ioi(Show.AFTER_CLOSE)
    ana = row(1, "ana", islands=[candidate(1, at(11, 10), graded(IOI_TESTS, groups))])
    bo = row(2, "bo", islands=[candidate(1, at(11, 0), graded(IOI_TESTS, groups, ["small/4"]))])
    found = standings(board(), [column("islands", groups)], [ana, bo])
    assert [(r.name, r.rank, r.keys) for r in found.rows] == [("ana", 1, (0,)), ("bo", 1, (0,))]
    assert not any(cell.counting for r in found.rows for cell in r.cells.values())
    assert found.not_in_view == (NotInView("islands", ("small", "large"), (), CLOSE),)

    after = standings(board(), [column("islands", groups, revealed=True)], [ana, bo])
    assert [r.keys for r in after.rows] == [(100,), (70,)]
    assert after.not_in_view == ()


def test_best_per_group_adds_each_groups_best_from_two_submissions() -> None:
    groups = ioi()
    large = graded(IOI_TESTS, groups, ["small/4"])
    small = graded(IOI_TESTS, groups, ["large/1"])
    bo = row(2, "bo", islands=[candidate(1, at(10, 45), large), candidate(2, at(12, 5), small)])
    per_group = standings(board(select="best_per_group"), [column("islands", groups)], [bo])
    cell = per_group.rows[0].cells["islands"]
    assert per_group.rows[0].keys == (100,)
    assert cell.submissions == (1, 2)
    best = standings(board(), [column("islands", groups)], [bo])
    assert best.rows[0].keys == (70,)
    assert best.rows[0].cells["islands"].submissions == (1,)


ICPC = {"judge": group(pass_=1, show=Show.VERDICT)}
ICPC_TESTS = named("judge", 5)
ICPC_BOARD = {"order": ["points", {"by": "penalty", "per_attempt": 20}], "who": "everyone"}


def test_icpc_penalty_is_25_plus_20_for_the_wrong_answer_and_not_the_compile_error() -> None:
    solved = graded(ICPC_TESTS, ICPC, worth=1)
    wrong = graded(ICPC_TESTS, ICPC, ["judge/3"], worth=1)
    team = row(
        1,
        "x",
        a=[
            Entry(1, minute(5), State.VOID),
            candidate(2, minute(10), wrong),
            candidate(3, minute(25), solved),
            candidate(4, minute(120), solved),
        ],
        b=[candidate(5, minute(30), wrong), candidate(6, minute(40), wrong)],
    )
    columns = [column("a", ICPC, worth=1), column("b", ICPC, worth=1)]
    found = standings(board(**ICPC_BOARD), columns, [team])
    x = found.rows[0]
    assert x.keys == (1, 45)
    assert x.cells["a"].submissions == (3,)
    assert x.cells["a"].attempts == 1
    assert x.cells["a"].numbers == {"points": 1, "penalty": 45}
    assert not x.cells["b"].counting
    assert x.cells["b"].attempts == 2
    assert sees(board(**ICPC_BOARD), Audience.EVERYONE)


def test_a_submission_still_grading_shows_only_in_its_own_rows_count() -> None:
    solved = graded(ICPC_TESTS, ICPC, worth=1)
    x = row(1, "x", a=[candidate(1, minute(25), solved), Entry(2, minute(30), State.GRADING)])
    y = row(2, "y", a=[Entry(3, minute(31), State.GRADING)])
    found = standings(board(**ICPC_BOARD), [column("a", ICPC, worth=1)], [x, y])
    mine = for_viewer(found, "all", owner(1))
    assert [(r.name, r.cells["a"].grading) for r in mine.rows] == [("x", 1), ("y", None)]
    assert mine.rows[1].cells["a"].submissions is None
    organiser = for_viewer(found, "all", None, every_row=True)
    assert [r.cells["a"].grading for r in organiser.rows] == [1, 1]
    assert organiser.rows[0].cells["a"].submissions == (1,)


METRIC = {"metric": Measure(Fold.MEAN, Better.LOWER)}
KAGGLE = {"public_split": group(), "private_split": group(show=Show.AFTER_CLOSE)}
KAGGLE_TESTS = ["public_split/1", "private_split/1"]


def kaggle(public: str | None, private: str | None) -> Scored:
    by = {
        test: value
        for test, value in (("public_split/1", public), ("private_split/1", private))
        if value is not None
    }
    failing = KAGGLE_TESTS if public is None else ()
    return graded(KAGGLE_TESTS, KAGGLE, failing, worth=None, values={"metric": by}, measures=METRIC)


def kaggle_rows() -> list[Row]:
    subs = [
        (1, at(10, 0), kaggle("0.5", "0.9")),
        (2, at(11, 0), kaggle("0.7", "0.6")),
        (3, at(12, 0), kaggle("0.4", "1.2")),
    ]
    marked = row(
        1,
        "marker",
        house_prices=[candidate(n, when, s, marked=n in (1, 2)) for n, when, s in subs],
    )
    unmarked = row(2, "unmarked", house_prices=[candidate(n, when, s) for n, when, s in subs])
    broken = row(3, "broken", house_prices=[candidate(9, at(10, 0), kaggle(None, None))])
    return [broken, unmarked, marked]


KAGGLE_ORDER = ["metric", "penalty"]


def test_kaggle_public_reads_only_the_public_split_and_the_earlier_wins_ties() -> None:
    public = board(name="Public", over="live", order=KAGGLE_ORDER, who="everyone")
    found = standings(
        public,
        [column("house-prices", KAGGLE, worth=None)],
        kaggle_rows(),
        directions={"metric": Better.LOWER},
    )
    assert [(r.name, r.rank, r.keys) for r in found.rows] == [
        ("marker", 1, (Fraction(2, 5), 180)),
        ("unmarked", 1, (Fraction(2, 5), 180)),
        ("broken", 3, (None, None)),
    ]


def test_kaggle_private_is_shown_from_the_reveal_then_counts_marks_or_the_public_best() -> None:
    private = board(
        name="Private", over="after_close", select="marked", order=KAGGLE_ORDER, who="everyone"
    )
    directions = {"metric": Better.LOWER}
    before = standings(
        private, [column("house-prices", KAGGLE, worth=None)], kaggle_rows(), directions=directions
    )
    assert before.nothing_shown
    assert before.shown_at == CLOSE
    assert before.rows == ()

    unreleased = standings(
        private,
        [column("house-prices", KAGGLE, worth=None, released=False)],
        kaggle_rows(),
        directions=directions,
    )
    assert (unreleased.nothing_shown, unreleased.shown_at, unreleased.tasks) == (True, None, ())

    after = standings(
        private,
        [column("house-prices", KAGGLE, worth=None, revealed=True)],
        kaggle_rows(),
        directions=directions,
    )
    assert [(r.name, r.rank, r.keys) for r in after.rows] == [
        ("marker", 1, (Fraction(3, 5), 120)),
        ("unmarked", 2, (Fraction(6, 5), 180)),
        ("broken", 3, (None, None)),
    ]
    assert after.rows[0].cells["house-prices"].submissions == (2,)
    assert after.rows[1].cells["house-prices"].submissions == (3,)

    final = standings(
        private,
        [column("house-prices", KAGGLE, worth=None)],
        kaggle_rows(),
        final=True,
        directions=directions,
    )
    assert [r.keys for r in final.rows] == [r.keys for r in after.rows]


SCORE = {"score": Measure(Fold.SUM, Better.HIGHER, Fraction(0))}


def test_raw_scores_of_ten_to_the_eighth_that_differ_by_1_rank_apart() -> None:
    groups = {"main": group()}
    ids = named("main", 3)

    def sums(values: Mapping[str, str]) -> Scored:
        return graded(ids, groups, worth=None, values={"score": values}, measures=SCORE)

    one = sums({"main/1": "100000000", "main/2": "100000001", "main/3": "100000000"})
    two = sums({"main/1": "100000000", "main/2": "100000000", "main/3": "100000000"})
    on = board(order=["score", "penalty"])
    found = standings(
        on,
        [column("opt", groups, worth=None)],
        [
            row(1, "a", opt=[candidate(1, at(10, 0), two)]),
            row(2, "b", opt=[candidate(2, at(11, 0), one)]),
        ],
        directions={"score": Better.HIGHER},
    )
    assert [(r.name, r.rank, r.keys[0]) for r in found.rows] == [
        ("b", 1, 300000001),
        ("a", 2, 300000000),
    ]


TIME = {"time_ms": Measure(Fold.MAX, Better.LOWER, Fraction(0))}


def test_a_points_tie_is_broken_on_the_sum_of_each_tasks_slowest_time() -> None:
    groups = {"main": group(pass_=100)}
    ids = named("main", 2)

    def timed(first: str, second: str) -> Scored:
        values = {"time_ms": {"main/1": first, "main/2": second}}
        return graded(ids, groups, values=values, measures=TIME)

    fast = row(
        1,
        "fast",
        a=[candidate(1, at(10, 0), timed("10", "30"))],
        b=[candidate(2, at(10, 0), timed("5", "5"))],
    )
    slow = row(
        2,
        "slow",
        a=[candidate(3, at(10, 0), timed("40", "20"))],
        b=[candidate(4, at(10, 0), timed("1", "2"))],
    )
    lone = row(3, "lone", a=[candidate(5, at(10, 0), timed("100", "100"))])
    on = board(order=["points", "time_ms"])
    columns = [column("a", groups), column("b", groups)]
    found = standings(on, columns, [slow, lone, fast], directions={"time_ms": Better.LOWER})
    assert [(r.name, r.rank, r.keys) for r in found.rows] == [
        ("fast", 1, (200, 35)),
        ("slow", 2, (200, 42)),
        ("lone", 3, (100, 100)),
    ]


def test_a_fast_wrong_answer_has_no_time_and_ranks_after_every_row_with_one() -> None:
    # A test that was not accepted has no time, and a time declares no worst
    # bound, so the wrong submission has no time: its cell does not count on a
    # board ranked by time first, and its row ranks after every row with one.
    groups = {"main": group(pass_=100)}
    ids = named("main", 2)
    values = {"time_ms": {"main/1": "1", "main/2": "1"}}
    garbage = graded(ids, groups, ids, values=values, measures=TIME)
    right = graded(ids, groups, values={"time_ms": {"main/1": "40", "main/2": "30"}}, measures=TIME)
    on = board(order=["time_ms"])
    rows = [row(1, "garbage", a=[candidate(1, at(10, 0), garbage)])]
    rows.append(row(2, "right", a=[candidate(2, at(10, 0), right)]))
    found = standings(on, [column("a", groups)], rows, directions={"time_ms": Better.LOWER})
    assert [(r.name, r.rank, r.keys) for r in found.rows] == [
        ("right", 1, (40,)),
        ("garbage", 2, (None,)),
    ]
    assert not found.rows[1].cells["a"].counting


def test_a_course_board_counts_the_public_30_and_shows_only_ones_own_row() -> None:
    groups = {"public": group(each=30), "hidden": group(each=70, show=Show.AFTER_CLOSE)}
    ids = [*named("public", 5), *named("hidden", 14)]
    mei = row(1, "mei", hw1=[candidate(1, at(10, 0), graded(ids, groups))])
    zoe = row(2, "zoe", hw1=[candidate(2, at(10, 0), graded(ids, groups, ["public/1"]))])
    grades = board(name="Grades", who="contestants", rows="own")
    found = standings(grades, [column("hw1", groups)], [mei, zoe])
    assert [r.keys for r in found.rows] == [(30,), (24,)]
    mine = for_viewer(found, grades.rows, owner(1))
    assert [(r.name, r.keys) for r in mine.rows] == [("mei", (Fraction(30),))]
    assert not sees(grades, Audience.EVERYONE)
    assert sees(grades, Audience.CONTESTANTS)


def test_a_practice_task_giving_no_points_never_counts_and_costs_no_penalty() -> None:
    practice = {"main": group()}
    final = {"main": group(each=100)}
    ids = named("main", 4)
    entries = [candidate(n, minute(n), graded(ids, practice, worth=None)) for n in range(1, 13)]
    ana = row(
        1,
        "ana",
        cartpole_practice=entries,
        cartpole_final=[candidate(20, at(13, 30), graded(ids, final))],
    )
    on = board(order=["points", "penalty"])
    columns = [column("cartpole-practice", practice, worth=None), column("cartpole-final", final)]
    found = standings(on, columns, [ana])
    assert found.rows[0].keys == (100, 270)
    assert not found.rows[0].cells["cartpole-practice"].counting
    tasks = {
        "cartpole-practice": covered({}, practice),
        "cartpole-final": covered({}, final),
    }
    assert counts_nothing(on, tasks) == ["cartpole-practice"]
    assert counts_nothing_message(on, "cartpole-practice") == (
        "The leaderboard Standings counts nothing from cartpole-practice."
    )


def test_a_task_not_released_is_not_on_the_view() -> None:
    groups = {"main": group(each=100)}
    columns = [column("a", groups), column("b", groups, released=False)]
    found = standings(board(), columns, [row(1, "ana")])
    assert [task.name for task in found.tasks] == ["a"]
    assert [task.name for task in standings(board(), columns, [], final=True).tasks] == ["a", "b"]


def tied() -> Any:
    groups = {"main": group(each=100)}
    ids = named("main", 10)
    scores = {"ana": 0, "bo": 3, "cy": 3, "di": 5, "ed": 9}
    rows = [
        row(n, name, a=[candidate(n, at(10, 0), graded(ids, groups, ids[10 - wrong :]))])
        for n, (name, wrong) in enumerate(scores.items(), start=1)
    ]
    return standings(board(), [column("a", groups)], rows)


def test_rows_equal_on_every_key_share_a_rank_listed_by_name() -> None:
    found = tied()
    assert [(r.name, r.rank) for r in found.rows] == [
        ("ana", 1),
        ("bo", 2),
        ("cy", 2),
        ("di", 4),
        ("ed", 5),
    ]


def test_rows_n_keeps_every_row_tied_with_the_nth_and_the_viewers_own_below() -> None:
    found = tied()
    assert [r.name for r in for_viewer(found, 2, owner(1)).rows] == [
        "ana",
        "bo",
        "cy",
    ]
    assert [r.name for r in for_viewer(found, 2, owner(5)).rows] == [
        "ana",
        "bo",
        "cy",
        "ed",
    ]
    assert [r.name for r in for_viewer(found, 9, None).rows] == ["ana", "bo", "cy", "di", "ed"]
    assert [r.name for r in for_viewer(found, "own", owner(4)).rows] == ["di"]
    assert for_viewer(found, "own", None).rows == ()


def test_keys_read_penalty_and_values_with_their_direction() -> None:
    on = board(order=["points", "time_ms", {"by": "penalty", "per_attempt": 20}])
    assert keys_of(on, {"time_ms": Better.LOWER}) == (
        Key("points", Better.HIGHER),
        Key("time_ms", Better.LOWER),
        Key("penalty", Better.LOWER, 20),
    )


REWARD = {
    "fraction": Measure(Fold.MEAN, Better.HIGHER, Fraction(0), Fraction(1)),
    "reward": Measure(Fold.MEAN, Better.HIGHER),
}
RL = {"easy": group(each=40), "hard": group(each=60, show=Show.AFTER_CLOSE)}


def test_raw_reward_is_ranked_on_one_task_and_refused_across_several() -> None:
    one = board(name="Reward", tasks=["lander"], order=["reward"])
    assert breaches(one, {"lander": covered(REWARD, RL)}, many=False) == []
    across = board(name="Reward", order=["reward"])
    found = breaches(
        across, {"lander": covered(REWARD, RL), "walker": covered(REWARD, RL)}, many=True
    )
    assert [(b.task, b.key, b.mended_in) for b in found] == [
        ("lander", 0, "workflow"),
        ("walker", 0, "workflow"),
    ]
    assert found[0].message == (
        "reward has no lowest value, so on the leaderboard Reward an entrant who skips a task "
        "would beat one who scored below 0 on it. Rank by reward on a leaderboard of one task."
    )


def test_a_lower_is_better_value_first_over_several_tasks_is_refused() -> None:
    public = board(name="Public", over="live", order=["metric", "penalty"])
    tasks = {"house-prices": covered(METRIC, KAGGLE), "rents": covered(METRIC, KAGGLE)}
    found = breaches(public, tasks, many=True, only="rents")
    assert [b.message for b in found] == [
        "The leaderboard Public ranks first by metric, for which lower is better, over more than "
        "one task. Rank by metric on a leaderboard of one task."
    ]
    assert breaches(public, {"house-prices": tasks["house-prices"]}, many=False) == []


def test_a_board_counting_what_is_shown_at_the_reveal_says_so_of_each_task() -> None:
    final = board(name="Final", over="after_close", order=["time_ms"])

    found = breaches(final, {"hw1": covered(TIME, {"main": group(pass_=1)})}, many=False)

    assert [b.message for b in found] == [
        "The leaderboard Final ranks by time_ms, counting the results shown when each task "
        "closes, so hw1 needs a test group whose results entrants see at the reveal."
    ]


def test_a_value_ranked_is_reported_with_a_fold_and_the_same_direction_everywhere() -> None:
    on = board(order=["points", "time_ms"])
    groups = {"main": group(pass_=1)}
    lower = covered(TIME, groups)
    higher = covered({"time_ms": Measure(Fold.MAX, Better.HIGHER, Fraction(0))}, groups)
    plain = covered({}, groups)
    unsure = covered({"time_ms": Measure(Fold.MAX, None, Fraction(0))}, groups)
    found = breaches(on, {"a": lower, "b": higher, "c": plain, "d": unsure}, many=True)
    assert [(b.task, b.key, b.message) for b in found] == [
        (
            "b",
            1,
            "The leaderboard Standings ranks by time_ms, but a higher time_ms is better in b and "
            "a lower one in a.",
        ),
        (
            "c",
            1,
            "The leaderboard Standings ranks by time_ms, but c's workflow does not report time_ms "
            "for each test, combined across its tests.",
        ),
        (
            "d",
            1,
            "The leaderboard Standings ranks by time_ms, but d's workflow does not say whether a "
            "higher or a lower time_ms is better, so it cannot be ranked.",
        ),
    ]


RUN_TIME = (ValueMeaning("time_ms", "Run time", "ms"),)


def test_a_value_is_named_by_its_title_where_a_covered_task_gives_one() -> None:
    on = board(order=["points", "time_ms"])
    groups = {"main": group(pass_=1)}
    titled = covered(TIME, groups, RUN_TIME)
    higher = covered({"time_ms": Measure(Fold.MAX, Better.HIGHER, Fraction(0))}, groups)
    plain = covered({}, groups)
    found = breaches(on, {"a": titled, "b": higher, "c": plain}, many=True)
    assert [b.message for b in found] == [
        "The leaderboard Standings ranks by run time, but a higher run time is better in b and "
        "a lower one in a.",
        "The leaderboard Standings ranks by run time, but c's workflow does not report run time "
        "for each test, combined across its tests.",
    ]


def test_a_value_title_opens_a_sentence_capitalised_and_keeps_an_abbreviation() -> None:
    across = board(name="Reward", order=["reward"])
    meanings = (ValueMeaning("reward", "Episode reward"),)
    found = breaches(
        across,
        {"lander": covered(REWARD, RL, meanings), "walker": covered(REWARD, RL)},
        many=True,
    )
    assert found[0].message == (
        "Episode reward has no lowest value, so on the leaderboard Reward an entrant who skips "
        "a task would beat one who scored below 0 on it. Rank by episode reward on a "
        "leaderboard of one task."
    )
    cpu = (ValueMeaning("time_ms", "CPU time", "ms"),)
    final = board(name="Final", over="after_close", order=["time_ms"])
    found = breaches(final, {"hw1": covered(TIME, {"main": group(pass_=1)}, cpu)}, many=False)
    assert found[0].message.startswith("The leaderboard Final ranks by CPU time, counting")


def test_a_board_cannot_rank_by_the_value_a_task_uses_as_its_credit() -> None:
    on = board(order=["points", "fraction"])
    groups = {"main": group(each=1)}
    islands = covered(REWARD, groups, credit="fraction")
    found = breaches(on, {"islands": islands}, many=False)
    assert [(b.task, b.key, b.mended_in, b.message) for b in found] == [
        (
            "islands",
            1,
            "credit",
            "The leaderboard Standings ranks by fraction, but islands already turns fraction "
            "into points through its test groups. Rank by points instead.",
        )
    ]
    found = breaches(on, {"islands": islands}, many=False, only="islands")
    assert [b.message for b in found] == [
        "Standings ranks by fraction, so islands cannot also use fraction as its credit: the "
        "board would rank a plain average that ignores test weights and groups. Change the "
        "board, or remove credit."
    ]


def test_a_board_may_rank_the_value_behind_relative_credit_or_with_no_credit() -> None:
    on = board(order=["points", "reward"])
    groups = {"main": group(each=1)}
    relative = covered(REWARD, groups, credit=Relative(relative="reward"))
    assert breaches(on, {"tour": relative}, many=False) == []
    assert breaches(on, {"tour": covered(REWARD, groups)}, many=False) == []


def test_a_value_first_needs_a_group_whose_tests_its_scope_reads() -> None:
    public = board(name="Public", over="live", tasks=["opt"], order=["score"])
    verdict_only = covered(SCORE, {"main": group(show=Show.VERDICT)})
    found = breaches(public, {"opt": verdict_only}, many=False)
    assert [(b.mended_in, b.message) for b in found] == [
        (
            "test_groups",
            "The leaderboard Public ranks by score, counting the results shown during the "
            "contest, so opt needs a test group whose results entrants see as soon as a grading "
            "ends, since a group shown as an outcome shows its tests only at the reveal.",
        )
    ]


def test_a_marked_board_needs_its_fallback_to_carry_its_first_key() -> None:
    marked = board(name="Final", over="after_close", select="marked")
    hidden = covered({}, {"samples": group(), "main": group(each=100, show=Show.AFTER_CLOSE)})
    found = breaches(marked, {"t": hidden}, many=False)
    assert [b.message for b in found] == [
        "For an entrant who marked nothing, the leaderboard Final counts what they saw before "
        "the reveal, so t needs a test group that gives points and shows its results before the "
        "reveal."
    ]
    fine = covered({}, {"public": group(each=30), "hidden": group(each=70, show=Show.AFTER_CLOSE)})
    assert breaches(marked, {"t": fine}, many=False) == []


def test_each_groups_most_is_worth_times_its_rule_weight_over_the_total() -> None:
    assert group_max(ioi(), Fraction(100)) == {"samples": 0, "small": 30, "large": 70}
    assert group_max({"main": group(each=80, pass_=20)}, Fraction(100)) == {"main": 100}
    assert group_max({"main": group()}, Fraction(100)) == {}


HIDDEN_LARGE = {
    "small": group(pass_=30, show=Show.VERDICT),
    "large": group(pass_=70, show=Show.AFTER_CLOSE),
}
SMALL_AND_LARGE = [*named("small", 2), *named("large", 2)]


def points_on(
    on: Column,
    entries: Sequence[Entry],
    select: Select = Select.BEST,
    *,
    directions: Mapping[str, Better | None] | None = None,
    **fields: Any,
) -> Points | None:
    """A row's points on the task `on` as a board over it counts them."""
    counting = board(select=select.value, **fields)
    keys = keys_of(counting, directions or {})
    return task_points(counting, keys, START, on, entries)


def test_a_tasks_points_are_what_a_board_over_it_counts_a_hidden_group_pending() -> None:
    # The first passes small and fails large, the second the other way: before
    # the reveal only small is shown, so the first's 30 counts, and large's 70
    # may still come, by the second; after it the second's 70 counts.
    first = candidate(1, minute(1), graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["large/1"]))
    second = candidate(2, minute(2), graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["small/1"]))
    entries = [first, second]
    for revealed, shown, pending in ((False, 30, 70), (True, 70, 0)):
        on = column("sum", HIDDEN_LARGE, revealed=revealed)
        (ranked,) = standings(board(), [on], [row(1, "ana", sum=entries)]).rows

        points = points_on(on, entries)

        assert points == Points(Fraction(shown), Fraction(pending), CLOSE if pending else None)
        assert ranked.keys[0] == points.shown


def test_a_tasks_points_hold_back_a_hidden_groups_points_until_the_reveal() -> None:
    passed_large = candidate(1, minute(1), graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["small/1"]))

    before = points_on(column("sum", HIDDEN_LARGE), [passed_large])
    after = points_on(column("sum", HIDDEN_LARGE, revealed=True), [passed_large])

    assert before == Points(Fraction(0), Fraction(70), CLOSE)
    assert after == Points(Fraction(70), Fraction(0), None)


def test_a_run_a_sealed_step_stopped_reads_as_every_group_hidden_until_the_reveal() -> None:
    hidden = {"main": group(pass_=100, show=Show.AFTER_CLOSE)}
    failed = graded(named("main", 1), hidden, ["main/1"])
    held = Entry(1, minute(1), State.ATTEMPT, failed)
    unheld = candidate(1, minute(1), failed)

    assert points_on(column("sum", hidden), [held]) == points_on(column("sum", hidden), [unheld])
    assert points_on(column("sum", hidden), [held]) == Points(None, Fraction(100), CLOSE)
    assert points_on(column("sum", hidden, revealed=True), [held]) == Points(
        Fraction(0), Fraction(0), None
    )


def test_a_held_runs_group_shown_at_once_is_pending_too_not_shown_as_what_it_earned() -> None:
    # The run failed `open`, shown at once on any other run: held, it reads
    # as every group hidden, so `open`'s 40 is pending beside main's 60,
    # never a 0 that tells its stop.
    groups = {
        "open": group(pass_=40, show=Show.ALWAYS),
        "main": group(pass_=60, show=Show.AFTER_CLOSE),
    }
    failed = graded([*named("open", 1), *named("main", 1)], groups, ["open/1", "main/1"])
    held = Entry(1, minute(1), State.ATTEMPT, failed)
    unheld = candidate(1, minute(1), failed)

    assert points_on(column("sum", groups), [held]) == Points(Fraction(0), Fraction(100), CLOSE)
    assert points_on(column("sum", groups), [unheld]) == Points(Fraction(0), Fraction(60), CLOSE)
    # On a board over the live groups, all shown, the held run's 40 is still
    # to come until the reveal; the unheld one's 0 is final.
    assert points_on(column("sum", groups), [held], over="live") == Points(
        Fraction(0), Fraction(40), CLOSE
    )
    assert points_on(column("sum", groups), [unheld], over="live") == Points(
        Fraction(0), Fraction(0), None
    )


def test_a_tasks_points_are_none_shown_while_no_group_is_shown_and_a_shown_0_stays_0() -> None:
    hidden = {"main": group(pass_=100, show=Show.AFTER_CLOSE)}
    passed = candidate(1, minute(1), graded(named("main", 1), hidden))
    zero = candidate(1, minute(1), graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["small/1", "large/1"]))

    assert points_on(column("sum", hidden), [passed]) == Points(None, Fraction(100), CLOSE)
    assert points_on(column("sum", hidden), []) == Points(None, Fraction(0), None)
    assert points_on(column("sum", HIDDEN_LARGE), [zero]) == Points(
        Fraction(0), Fraction(70), CLOSE
    )


SPLIT = {"a": group(pass_=50, show=Show.VERDICT), "b": group(pass_=50, show=Show.VERDICT)}


def test_a_tasks_points_count_the_submissions_its_board_counts_by_group_or_marked() -> None:
    # The first passes a, the second b and is marked: by group the row has
    # 100, marked 50, and best 50, as each board ranks it.
    on = column("sum", SPLIT)
    first = candidate(1, minute(1), graded(["a/1", "b/1"], SPLIT, ["b/1"]))
    second = candidate(2, minute(2), graded(["a/1", "b/1"], SPLIT, ["a/1"]), marked=True)
    for select, shown in (("best_per_group", 100), ("marked", 50), ("best", 50)):
        rows = [row(1, "ana", sum=[first, second])]
        (ranked,) = standings(board(select=select), [on], rows).rows

        points = points_on(on, [first, second], Select(select))

        assert points == Points(Fraction(shown), Fraction(0), None), select
        assert ranked.keys[0] == points.shown

    # A marked submission that scored nothing counts nothing, as its board.
    nothing = candidate(2, minute(2), graded(["a/1", "b/1"], SPLIT, ["a/1", "b/1"]), marked=True)
    (ranked,) = standings(board(select="marked"), [on], [row(1, "ana", sum=[first, nothing])]).rows
    assert points_on(on, [first, nothing], Select.MARKED) == Points(Fraction(0), Fraction(0), None)
    assert ranked.keys[0] == 0


def test_a_tasks_points_pending_by_group_add_each_groups_most_still_to_come() -> None:
    # a and b are shown, c only after the close: by group, the first's a and
    # the second's b give 80 now, and c's 20 may still come; counting one
    # submission, 40 now and 20 to come.
    groups = {
        "a": group(pass_=40, show=Show.VERDICT),
        "b": group(pass_=40, show=Show.VERDICT),
        "c": group(pass_=20, show=Show.AFTER_CLOSE),
    }
    tests = ["a/1", "b/1", "c/1"]
    first = candidate(1, minute(1), graded(tests, groups, ["b/1"]))
    second = candidate(2, minute(2), graded(tests, groups, ["a/1", "c/1"]))
    entries = [first, second]

    assert points_on(column("sum", groups), entries, Select.BEST_PER_GROUP) == Points(
        Fraction(80), Fraction(20), CLOSE
    )
    assert points_on(column("sum", groups), entries) == Points(Fraction(40), Fraction(20), CLOSE)
    assert points_on(
        column("sum", groups, revealed=True), entries, Select.BEST_PER_GROUP
    ) == Points(Fraction(100), Fraction(0), None)


def test_a_tasks_points_pending_when_marked_come_from_the_marked_or_the_one_it_counts() -> None:
    # The first, marked, failed small; the second passed it: marked counts
    # the first, 0 now and large's 70 to come, where best counts the
    # second's 30. With nothing marked, the board counts the second, the
    # best on what was shown before the reveal.
    failed_small = graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["small/1"])
    first = candidate(1, minute(1), failed_small, marked=True)
    second = candidate(2, minute(2), graded(SMALL_AND_LARGE, HIDDEN_LARGE))
    on = column("sum", HIDDEN_LARGE)

    assert points_on(on, [first, second], Select.MARKED) == Points(Fraction(0), Fraction(70), CLOSE)
    unmarked = candidate(1, minute(1), failed_small)
    assert points_on(on, [unmarked, second], Select.MARKED) == Points(
        Fraction(30), Fraction(70), CLOSE
    )


def test_with_nothing_marked_pending_comes_from_the_one_a_value_led_board_counts() -> None:
    # Ranked by score, nothing marked: the board counts the first (score 9,
    # 15 of small's 30), not the second, which shows more points (30) but
    # scores less; what may still come is the first's large, 70.
    groups = {
        "small": group(each=30, show=Show.ALWAYS),
        "large": group(pass_=70, show=Show.AFTER_CLOSE),
    }
    ids = [*named("small", 2), "large/1"]
    nine = dict.fromkeys(ids, "9")
    first = candidate(
        1, minute(1), graded(ids, groups, ["small/2"], values={"score": nine}, measures=SCORE)
    )
    one = dict.fromkeys(ids, "1")
    second = candidate(2, minute(2), graded(ids, groups, values={"score": one}, measures=SCORE))
    on = column("opt", groups)

    found = points_on(
        on, [first, second], Select.MARKED, order=["score"], directions={"score": Better.HIGHER}
    )

    assert found == Points(Fraction(15), Fraction(70), CLOSE)


def test_a_task_with_nothing_submitted_has_nothing_pending_and_one_giving_no_points_none() -> None:
    assert points_on(column("sum", HIDDEN_LARGE), []) == Points(Fraction(0), Fraction(0), None)
    assert points_on(column("sum", HIDDEN_LARGE, worth=None), []) is None


def test_a_tasks_points_on_a_board_count_only_its_scope_and_hold_back_what_it_does() -> None:
    # The first passes small, shown as a verdict, and fails large, shown at
    # the close; the second the other way. A board over the live groups
    # counts small's 30 and has nothing to come; one over the close shows
    # nothing until the reveal, with large's 70 to come, then the second's.
    first = candidate(1, minute(1), graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["large/1"]))
    second = candidate(2, minute(2), graded(SMALL_AND_LARGE, HIDDEN_LARGE, ["small/1"]))
    entries = [first, second]
    before, after = column("sum", HIDDEN_LARGE), column("sum", HIDDEN_LARGE, revealed=True)

    assert points_on(before, entries, over="live") == Points(Fraction(30), Fraction(0), None)
    assert points_on(before, entries, over="after_close") == Points(None, Fraction(70), CLOSE)
    assert points_on(after, entries, over="after_close") == Points(Fraction(70), Fraction(0), None)
    for over in ("live", "after_close"):
        on = before if over == "live" else after
        (ranked,) = standings(board(over=over), [on], [row(1, "ana", sum=entries)]).rows
        assert ranked.keys[0] == points_on(on, entries, over=over).shown  # type: ignore[union-attr]


def test_a_tasks_points_on_a_board_ranked_by_a_value_are_its_counted_submissions() -> None:
    # The first passes both tests and scores 2; the second passes one and
    # scores 9, its wrong test counting 0. Points first counts the first's
    # 100; the score first counts the second's 50.
    groups = {"main": group(each=100)}
    ids = named("main", 2)
    first = graded(ids, groups, values={"score": {"main/1": "1", "main/2": "1"}}, measures=SCORE)
    second = graded(
        ids, groups, ["main/2"], values={"score": {"main/1": "9", "main/2": "9"}}, measures=SCORE
    )
    entries = [candidate(1, minute(1), first), candidate(2, minute(2), second)]
    on = column("opt", groups)
    directions = {"score": Better.HIGHER}

    by_points = points_on(on, entries, order=["points", "score"], directions=directions)
    by_score = points_on(on, entries, order=["score"], directions=directions)

    assert by_points == Points(Fraction(100), Fraction(0), None)
    assert by_score == Points(Fraction(50), Fraction(0), None)


def test_a_tasks_points_are_0_on_a_board_whose_cell_does_not_count() -> None:
    # A wrong answer has no time, so a board ranked by time alone counts
    # nothing from it, though it earned points.
    groups = {"main": group(each=100)}
    ids = named("main", 2)
    half = graded(ids, groups, ["main/2"], values={"time_ms": {"main/1": "5"}}, measures=TIME)
    entries = [candidate(1, minute(1), half)]
    on = column("a", groups)

    assert points_on(on, entries) == Points(Fraction(50), Fraction(0), None)
    assert points_on(
        on, entries, order=["time_ms"], directions={"time_ms": Better.LOWER}
    ) == Points(Fraction(0), Fraction(0), None)
