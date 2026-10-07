"""Scoring a grading on read: credit, a group's points, values folded over
tests, what is shown at a moment, and every number exact. Each worked example
of TASK-FORMAT.md section 3.7 a classic task can express is here with its
numbers.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

from forge.domain.definitions import Group, Relative, Show
from forge.domain.scoring import (
    BEFORE_REVEAL,
    EVERYTHING,
    Better,
    Measure,
    best_of,
    credit_of,
    decimal_of,
    everywhere,
    exact,
    fold,
    late_factor,
    points_seen,
    score,
    written,
)
from forge.domain.workflow_definition import Fold


def group(**rules: Any) -> Group:
    return Group.model_validate(
        {("pass" if key == "pass_" else key): value for key, value in rules.items()}
    )


def named(name: str, count: int) -> list[str]:
    return [f"{name}/{number}" for number in range(1, count + 1)]


def result(rows: Mapping[str, str | tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    found = []
    for test, row in rows.items():
        outcome, values = (row, {}) if isinstance(row, str) else row
        found.append({"test": test, "outcome": outcome, "values": dict(values)})
    return {"tests": found}


def passing(ids: Sequence[str], failing: Sequence[str] = ()) -> dict[str, str]:
    return {test: "wrong_answer" if test in failing else "accepted" for test in ids}


def ioi(**shows: Show) -> dict[str, Group]:
    return {
        "samples": group(**({"show": shows["samples"]} if "samples" in shows else {})),
        "small": group(pass_=30, **({"show": shows["small"]} if "small" in shows else {})),
        "large": group(pass_=70, **({"show": shows["large"]} if "large" in shows else {})),
    }


IOI_TESTS = [*named("samples", 2), *named("small", 10), *named("large", 20)]


def test_ioi_subtasks_score_100_for_everything_and_70_without_one_small_test() -> None:
    ana = score(result(passing(IOI_TESTS)), IOI_TESTS, ioi(), None, {}, worth=100)
    bo = score(result(passing(IOI_TESTS, ["small/4"])), IOI_TESTS, ioi(), None, {}, worth=100)
    assert ana.points(everywhere, EVERYTHING) == 100
    assert bo.points(everywhere, EVERYTHING) == 70
    assert [group.points for group in bo.groups] == [0, 0, 70]


def test_hidden_subtasks_show_only_the_samples_and_their_most_until_the_reveal() -> None:
    groups = ioi(small=Show.AFTER_CLOSE, large=Show.AFTER_CLOSE)
    ana = score(result(passing(IOI_TESTS)), IOI_TESTS, groups, None, {}, worth=100)
    before = points_seen(ana, BEFORE_REVEAL, None)
    assert before is not None
    assert (before.shown, before.pending) == (Fraction(0), Fraction(100))
    assert [(group.group, group.max) for group in ana.groups] == [
        ("samples", Fraction(0)),
        ("small", Fraction(30)),
        ("large", Fraction(70)),
    ]
    after = points_seen(ana, EVERYTHING, None)
    assert after is not None
    assert (after.shown, after.pending) == (Fraction(100), Fraction(0))


def test_per_test_points_earn_five_a_test_and_a_test_weight_earns_itself() -> None:
    ids = named("main", 20)
    failing = ["main/18", "main/19", "main/20"]
    plain = score(
        result(passing(ids, failing)), ids, {"main": group(each=100)}, None, {}, worth=100
    )
    assert plain.points(everywhere, EVERYTHING) == 85

    weights = {str(n): 2 for n in range(11, 17)} | {str(n): 5 for n in range(17, 21)}
    weighted = {"main": group(each=42, test_weights=weights)}
    every = score(result(passing(ids)), ids, weighted, None, {}, worth=42)
    assert every.points(everywhere, EVERYTHING) == 42
    without_20 = score(result(passing(ids, ["main/20"])), ids, weighted, None, {}, worth=42)
    assert without_20.points(everywhere, EVERYTHING) == 37
    without_11 = score(result(passing(ids, ["main/11"])), ids, weighted, None, {}, worth=42)
    assert without_11.points(everywhere, EVERYTHING) == 40


FRACTION = {"fraction": Measure(Fold.MEAN, Better.HIGHER, Fraction(0), Fraction(1))}


def checker(rows: Mapping[str, tuple[str, str | None]]) -> dict[str, Any]:
    return result(
        {
            test: (outcome, {"fraction": Decimal(value)} if value is not None else {})
            for test, (outcome, value) in rows.items()
        }
    )


FOUR = {
    "main/1": ("accepted", "1"),
    "main/2": ("accepted", "0.8"),
    "main/3": ("accepted", "0.5"),
    "main/4": ("accepted", "1"),
}


def test_a_partial_checker_earns_50_on_worst_and_82_5_on_each() -> None:
    ids = named("main", 4)
    worst = score(checker(FOUR), ids, {"main": group(worst=100)}, "fraction", FRACTION, worth=100)
    each = score(checker(FOUR), ids, {"main": group(each=100)}, "fraction", FRACTION, worth=100)
    assert worst.points(everywhere, EVERYTHING) == 50
    assert each.points(everywhere, EVERYTHING) == Fraction("82.5")
    assert written(each.points(everywhere, EVERYTHING) or Fraction(0)) == "82.5"


def test_a_fifth_test_wrong_earns_0_whatever_its_checker_printed() -> None:
    five = FOUR | {"main/5": ("wrong_answer", "0.9")}
    ids = named("main", 5)
    worst = score(checker(five), ids, {"main": group(worst=100)}, "fraction", FRACTION, worth=100)
    each = score(checker(five), ids, {"main": group(each=100)}, "fraction", FRACTION, worth=100)
    assert worst.points(everywhere, EVERYTHING) == 0
    assert each.points(everywhere, EVERYTHING) == 66


def test_a_threshold_of_0_8_passes_8_of_10_exactly_and_not_7() -> None:
    ids = named("main", 10)
    groups = {"main": group(pass_=100, pass_at=0.8)}
    eight = score(result(passing(ids, ids[8:])), ids, groups, None, {}, worth=100)
    seven = score(result(passing(ids, ids[7:])), ids, groups, None, {}, worth=100)
    assert eight.points(everywhere, EVERYTHING) == 100
    assert seven.points(everywhere, EVERYTHING) == 0
    assert exact(0.8) == Fraction(20, 25)


def test_all_or_nothing_else_partial_gives_72_for_nine_and_100_for_ten() -> None:
    ids = named("main", 10)
    groups = {"main": group(each=80, pass_=20)}
    nine = score(result(passing(ids, ids[9:])), ids, groups, None, {}, worth=100)
    ten = score(result(passing(ids)), ids, groups, None, {}, worth=100)
    assert nine.points(everywhere, EVERYTHING) == 72
    assert ten.points(everywhere, EVERYTHING) == 100
    assert [(group.max, group.points) for group in nine.groups] == [(Fraction(100), Fraction(72))]


def test_late_days_take_their_share_and_never_below_nothing() -> None:
    ids = [*named("public", 5), *named("hidden", 14)]
    groups = {"public": group(each=30), "hidden": group(each=70, show=Show.AFTER_CLOSE)}
    on_time = score(
        result(passing(ids, ["hidden/1", "hidden/2"])), ids, groups, None, {}, worth=100
    )
    assert on_time.points(everywhere, EVERYTHING) == 90
    late = score(result(passing(ids)), ids, groups, None, {}, worth=100, factor=late_factor(2, 0.1))
    assert late.points(everywhere, EVERYTHING) == 80
    assert late_factor(11, 0.1) == 0
    assert late_factor(0, 0.1) == 1


def test_a_course_shows_public_30_and_70_pending_until_the_reveal() -> None:
    ids = [*named("public", 5), *named("hidden", 14)]
    groups = {"public": group(each=30), "hidden": group(each=70, show=Show.AFTER_CLOSE)}
    mei = score(result(passing(ids)), ids, groups, None, {}, worth=100)
    before = points_seen(mei, BEFORE_REVEAL, None)
    assert before is not None
    assert (before.shown, before.pending) == (Fraction(30), Fraction(70))


def test_hidden_tests_show_0_and_100_pending_then_75() -> None:
    ids = [*named("samples", 2), *named("main", 40)]
    groups = {"samples": group(), "main": group(each=100, show=Show.AFTER_CLOSE)}
    thirty = score(result(passing(ids, named("main", 40)[30:])), ids, groups, None, {}, worth=100)
    every = score(result(passing(ids)), ids, groups, None, {}, worth=100)
    for scored in (thirty, every):
        before = points_seen(scored, BEFORE_REVEAL, None)
        assert before is not None
        assert (before.shown, before.pending) == (Fraction(0), Fraction(100))
        assert scored.outcome(BEFORE_REVEAL) == "accepted"
    after = points_seen(thirty, EVERYTHING, None)
    assert after is not None
    assert (after.shown, after.pending) == (Fraction(75), Fraction(0))
    assert thirty.outcome(EVERYTHING) == "wrong_answer"


def test_a_verdict_group_shows_its_outcome_and_points_live() -> None:
    ids = [*named("samples", 2), *named("main", 40)]
    groups = {"samples": group(), "main": group(pass_=100, show=Show.VERDICT)}
    thirty = score(result(passing(ids, named("main", 40)[30:])), ids, groups, None, {}, worth=100)
    assert thirty.points(everywhere, BEFORE_REVEAL) == 0
    assert thirty.pending(BEFORE_REVEAL) == 0
    assert thirty.outcome(BEFORE_REVEAL) == "wrong_answer"


SCORE = {"score": Measure(Fold.SUM, Better.HIGHER, Fraction(0))}


def test_raw_scores_of_ten_to_the_eighth_sum_exactly_and_a_failure_counts_at_least() -> None:
    ids = named("main", 3)
    groups = {"main": group()}
    one = result(
        {
            "main/1": ("accepted", {"score": Decimal("100000000")}),
            "main/2": ("accepted", {"score": Decimal("100000001")}),
            "main/3": "wrong_answer",
        }
    )
    other = result(
        {
            "main/1": ("accepted", {"score": Decimal("100000000")}),
            "main/2": ("accepted", {"score": Decimal("100000000")}),
            "main/3": "time_limit_exceeded",
        }
    )
    first = score(one, ids, groups, None, SCORE, worth=None)
    second = score(other, ids, groups, None, SCORE, worth=None)
    assert first.value("score", everywhere, EVERYTHING) == 200000001
    assert second.value("score", everywhere, EVERYTHING) == 200000000
    assert not first.gives_points
    assert first.points(everywhere, EVERYTHING) is None


def test_a_lower_is_better_value_with_no_worst_bound_has_no_value_when_a_test_lacks_it() -> None:
    measure = Measure(Fold.MEAN, Better.LOWER)
    assert fold(measure, [Fraction(3), None]) is None
    assert fold(measure, [Fraction(3), Fraction(5)]) == 4
    assert fold(measure, []) is None
    assert (
        fold(Measure(Fold.MAX, Better.LOWER, Fraction(0), Fraction(2000)), [None, Fraction(3)])
        == 2000
    )


def test_relative_credit_is_the_value_against_the_best_any_candidate_reached() -> None:
    measures = {"score": Measure(Fold.SUM, Better.HIGHER, Fraction(0))}
    lower = {"score": Measure(Fold.SUM, Better.LOWER, Fraction(0))}
    relative = Relative(relative="score")
    row = {"score": Decimal("40")}
    assert credit_of("accepted", row, relative, measures, Fraction(80)) == Fraction(1, 2)
    assert credit_of("accepted", row, relative, lower, Fraction(20)) == Fraction(1, 2)
    assert credit_of("accepted", row, relative, measures, None) == 1
    assert credit_of("wrong_answer", row, relative, measures, Fraction(80)) == 0
    assert credit_of("accepted", {}, relative, measures, Fraction(80)) == 0
    results = [
        result({"main/1": ("accepted", {"score": Decimal("40")})}),
        result({"main/1": ("accepted", {"score": Decimal("80")})}),
        result({"main/1": ("wrong_answer", {"score": Decimal("900")})}),
    ]
    assert best_of(results, "score", Better.HIGHER) == {"main/1": 80}
    assert best_of(results, "score", Better.LOWER) == {"main/1": 40}


def test_relative_credit_is_scored_with_b_beside_the_value() -> None:
    ids = named("main", 2)
    measures = {"score": Measure(Fold.SUM, Better.HIGHER, Fraction(0))}
    rows = result(
        {
            "main/1": ("accepted", {"score": Decimal("40")}),
            "main/2": ("accepted", {"score": Decimal("90")}),
        }
    )
    scored = score(
        rows,
        ids,
        {"main": group(each=100)},
        Relative(relative="score"),
        measures,
        worth=100,
        best={"main/1": Fraction(80), "main/2": Fraction(90)},
    )
    assert scored.points(everywhere, EVERYTHING) == 75
    assert [(test.credit, test.best) for test in scored.groups[0].tests] == [
        (Fraction(1, 2), Fraction(80)),
        (Fraction(1), Fraction(90)),
    ]


def test_a_task_with_no_rule_weights_gives_no_points_whatever_its_worth() -> None:
    ids = named("main", 1)
    scored = score(result(passing(ids)), ids, {"main": group()}, None, {}, worth=100)
    assert not scored.gives_points
    assert points_seen(scored, EVERYTHING, None) is None


def test_numbers_are_exact_and_written_as_decimals() -> None:
    assert exact(Decimal("0.1")) + exact(Decimal("0.2")) == exact(Decimal("0.3"))  # type: ignore[operator]
    assert exact(True) is None
    assert exact("1") is None
    assert written(Fraction(1, 8)) == "0.125"
    assert written(Fraction(330, 4)) == "82.5"
    assert written(Fraction(100)) == "100"
    assert written(Fraction(1, 3)) == "0." + "3" * 30
    assert decimal_of(Fraction(2, 3)) == Decimal("0." + "6" * 29 + "7")
