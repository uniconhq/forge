"""The blocks a task's values are built from (SIMPLER-CORE.md 9.3 and 14,
#511): every block in the catalogue works out exactly; a fold reads a
group's runs with `where`, `weights` and `missing`, a missing report
counting as its column's worst bound; a fault waits for staff and spreads
but through an `if` branch not taken; 14.1's formulas give the fractions
written; one name per block, its form told from what it is given; and a
tree is kept small at the save and walked without recursion.
"""

import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from forge.domain import exact_json
from forge.domain.blocks.catalogue import CATALOGUE
from forge.domain.blocks.check import MOST_DEPTH, MOST_NODES, Found, check_values, tree_size
from forge.domain.blocks.evaluate import MOST_BITS, Column, Outcome, RunResult, evaluate
from forge.domain.blocks.numbers import Better, decimal_of, exact, written

COLUMNS = {
    "score": Column(Better.HIGHER, Fraction(0), Fraction(1)),
    "time_ms": Column(Better.LOWER, Fraction(0), None),
    "loss": Column(Better.LOWER, Fraction(0), Fraction(10)),
}
CHECKED_COLUMNS: dict[str, str | None] = {
    "input": "input is a file, and blocks read numbers only.",
    "seed": None,
    "score": None,
    "time_ms": None,
    "loss": None,
    "passed": None,
}


def many(name: str, *of: object) -> dict[str, Any]:
    return {"block": name, "of": list(of)}


def one(name: str, of: object) -> dict[str, Any]:
    return {"block": name, "of": of}


def fold(name: str, group: str, column: str | None = None, **inputs: object) -> dict[str, Any]:
    node: dict[str, Any] = {"block": name, "over": {"runs": group}, **inputs}
    if column is not None:
        node["of"] = {"run": [group, column]}
    return node


def value(name: str) -> dict[str, str]:
    return {"value": name}


def fact(name: str) -> dict[str, str]:
    return {"fact": name}


def run(
    id: str, verdict: str = "accepted", seed: int | None = None, **reports: object
) -> RunResult:
    key: dict[str, object] = {"input": f"tests/{id.removeprefix('r')}.in"}
    if seed is not None:
        key["seed"] = seed
    return RunResult(id, verdict, key, reports)


def worked(
    values: Mapping[str, object],
    runs: Sequence[RunResult] = (),
    groups: Mapping[str, Sequence[str]] | None = None,
    facts: Mapping[str, int | Fraction] | None = None,
) -> dict[str, Outcome]:
    by_id = {each.id: each for each in runs}
    held = {"all": list(by_id), **(groups or {})}
    return evaluate(
        values,
        {name: [by_id[id] for id in ids] for name, ids in held.items()},
        facts or {"seconds_late": 0, "seconds": 0, "compiled": 1},
        COLUMNS,
    )


def number(values: Mapping[str, object], name: str = "v", **given: Any) -> Fraction | None:
    outcome = worked(values, **given)[name]
    assert not outcome.waiting
    return outcome.number


def problems(values: Mapping[str, object]) -> Found:
    found = check_values(
        values,
        groups=["st1", "sample"],
        columns=CHECKED_COLUMNS,
        keys={"input": ["tests/1.in", "tests/2.in", "tests/3.in"], "seed": [1, 2, 3]},
    )
    return found.problems


def test_numbers_are_exact_and_written_as_decimals() -> None:
    assert exact(Decimal("0.1")) + exact(Decimal("0.2")) == exact(Decimal("0.3"))  # type: ignore[operator]
    assert exact(True) is None
    assert exact("1") is None
    assert written(Fraction(1, 8)) == "0.125"
    assert written(Fraction(330, 4)) == "82.5"
    assert written(Fraction(100)) == "100"
    assert written(Fraction(1, 3)) == "0." + "3" * 30
    assert decimal_of(Fraction(2, 3)) == Decimal("0." + "6" * 29 + "7")


RUNS = [run("r1", score=1, time_ms=30), run("r2", score=Decimal("0.5"), time_ms=50)]
EACH_BLOCK: list[tuple[str, object, Fraction]] = [
    ("add", many("add", 1, 2, Decimal("0.5")), Fraction(7, 2)),
    ("sub", many("sub", 1, 3), Fraction(-2)),
    ("mul", many("mul", 2, 3, 0.5), Fraction(3)),
    ("div", many("div", 3, 4), Fraction(3, 4)),
    ("lt", many("lt", 1, 2), Fraction(1)),
    ("le", many("le", 2, 2), Fraction(1)),
    ("gt", many("gt", 1, 2), Fraction(0)),
    ("ge", many("ge", 1, 2), Fraction(0)),
    ("eq", many("eq", 0.1, Decimal("0.1")), Fraction(1)),
    ("ne", many("ne", 2, 2), Fraction(0)),
    ("and", many("and", 1, 5, -1), Fraction(1)),
    ("or", many("or", 0, 0, 2), Fraction(1)),
    ("not", one("not", 0), Fraction(1)),
    ("if", {"block": "if", "test": 2, "then": 7, "else": 8}, Fraction(7)),
    ("min", many("min", 3, Decimal("0.5"), 2), Fraction(1, 2)),
    ("max", many("max", 3, Decimal("0.5"), 2), Fraction(3)),
    ("floor", one("floor", Decimal("-1.5")), Fraction(-2)),
    ("ceil", one("ceil", Decimal("-1.5")), Fraction(-1)),
    ("sum", fold("sum", "all", "score"), Fraction(3, 2)),
    ("count", fold("count", "all"), Fraction(2)),
    ("min", fold("min", "all", "time_ms"), Fraction(30)),
    ("max", fold("max", "all", "time_ms"), Fraction(50)),
]


def test_every_block_has_an_evaluation_test() -> None:
    assert {name for name, _, _ in EACH_BLOCK} == set(CATALOGUE)


@pytest.mark.parametrize(
    ("name", "tree", "expected"),
    EACH_BLOCK,
    ids=[f"{name}-{'runs' if 'over' in tree else 'numbers'}" for name, tree, _ in EACH_BLOCK],  # type: ignore[operator]
)
def test_each_block_works_out_exactly(name: str, tree: object, expected: Fraction) -> None:
    assert not problems({"v": tree})
    assert number({"v": tree}, runs=RUNS) == expected


def test_thirds_add_up_to_one_and_1200_over_1500_is_four_fifths() -> None:
    third = many("div", 1, 3)
    values = {
        "length": 1500,
        "whole": many("add", third, third, third),
        "ratio": many("div", 1200, value("length")),
    }

    outcomes = worked(values)

    assert outcomes["whole"].number == 1
    assert outcomes["ratio"].number == Fraction(4, 5)


def test_dividing_by_zero_is_missing_and_missing_spreads() -> None:
    values = {"zero": many("div", 1, 0), "more": many("add", value("zero"), 1)}

    outcomes = worked(values)

    assert outcomes["zero"].missing
    assert outcomes["more"].missing
    assert outcomes["more"].values == {"zero"}


def test_if_reads_only_the_branch_it_takes() -> None:
    values = {
        "gone": many("div", 1, 0),
        "v": {"block": "if", "test": 0, "then": value("gone"), "else": 3},
        "taken": {"block": "if", "test": 1, "then": value("gone"), "else": 3},
        "unsure": {"block": "if", "test": value("gone"), "then": 1, "else": 3},
    }

    outcomes = worked(values)

    assert outcomes["v"].number == 3
    assert outcomes["v"].values == frozenset()
    assert outcomes["taken"].missing
    assert outcomes["unsure"].missing


def test_and_and_or_read_every_input_so_missing_spreads_through_them() -> None:
    gone = many("div", 1, 0)

    assert number({"v": many("and", 0, gone)}) is None
    assert number({"v": many("or", 1, gone)}) is None


GRADED = [
    run("r1", score=1, time_ms=10, seed=1),
    run("r2", "wrong_answer", score=1, time_ms=20, seed=2),
    run("r3", score=Decimal("0.5"), time_ms=40, seed=3),
]


def test_a_wrong_answers_score_counts_as_its_worst_bound() -> None:
    values = {"v": fold("min", "all", "score"), "sum": fold("sum", "all", "score")}

    outcomes = worked(values, runs=GRADED)

    assert outcomes["v"].number == 0
    assert outcomes["sum"].number == Fraction(3, 2)


def test_a_wrong_answers_time_makes_the_fold_missing_with_no_bound_on_its_worse_side() -> None:
    assert number({"v": fold("max", "all", "time_ms")}, runs=GRADED) is None
    assert number({"v": fold("max", "all", "time_ms")}, runs=[GRADED[0], GRADED[2]]) == 40


def test_a_report_a_step_never_gave_is_missing_too() -> None:
    runs = [run("r1", score=1, time_ms=10), run("r2", score=1)]

    assert number({"v": fold("max", "all", "time_ms")}, runs=runs) is None
    assert number({"v": fold("max", "all", "loss")}, runs=runs) == 10


def test_a_folds_missing_skips_the_run_or_counts_a_number() -> None:
    skipped = fold("sum", "all", "time_ms", missing="skip")
    counted = fold("sum", "all", "time_ms", missing=100)
    fast = fold("count", "all", where=many("lt", {"run": ["all", "time_ms"]}, 30), missing="skip")

    assert number({"v": skipped}, runs=GRADED) == 50
    assert number({"v": counted}, runs=GRADED) == 150
    assert number({"v": fast}, runs=GRADED) == 1


def test_where_keeps_the_runs_that_meet_it() -> None:
    passing = many("eq", {"run": ["all", "passed"]}, 1)
    fast = many("le", {"run": ["all", "time_ms"]}, 20)

    assert number({"v": fold("count", "all", where=passing)}, runs=GRADED) == 2
    assert number({"v": fold("sum", "all", "score", where=passing)}, runs=GRADED) == Fraction(3, 2)
    assert number({"v": fold("count", "all", where=fast)}, runs=GRADED) is None
    assert number({"v": fold("count", "all", where=fast, missing="skip")}, runs=GRADED) == 1


def test_weights_weigh_a_run_by_its_keys_value_and_count_adds_them() -> None:
    weights = {"input": {"tests/3.in": 3}}
    by_seed = {"seed": {2: 2, 3: Decimal("0.5")}}

    assert number({"v": fold("sum", "all", "score", weights=weights)}, runs=GRADED) == Fraction(
        5, 2
    )
    assert number({"v": fold("count", "all", weights=weights)}, runs=GRADED) == 5
    assert number({"v": fold("count", "all", weights=by_seed)}, runs=GRADED) == Fraction(7, 2)


def test_a_weighted_mean_is_a_sum_over_a_count() -> None:
    weights = {"input": {"tests/3.in": 3}}
    mean = many(
        "div",
        fold("sum", "all", "score", weights=weights),
        fold("count", "all", weights=weights),
    )

    assert not problems({"v": mean})
    assert number({"v": mean}, runs=GRADED) == Fraction(1, 2)


def test_passed_and_a_keys_number_read_like_reports() -> None:
    assert number({"v": fold("sum", "all", "passed")}, runs=GRADED) == 2
    assert number({"v": fold("sum", "all", "seed")}, runs=GRADED) == 6


def test_a_fold_over_no_runs_is_missing_but_a_count_is_0() -> None:
    nothing = many("gt", {"run": ["all", "score"]}, 1)

    assert number({"v": fold("sum", "all", "score", where=nothing)}, runs=GRADED) is None
    assert number({"v": fold("min", "all", "score", where=nothing)}, runs=GRADED) is None
    assert number({"v": fold("count", "all", where=nothing)}, runs=GRADED) == 0


def test_a_fold_reads_its_groups_runs_only() -> None:
    values = {"v": fold("sum", "st1", "score")}

    outcome = worked(values, runs=GRADED, groups={"st1": ["r1", "r3"]})["v"]

    assert outcome.number == Fraction(3, 2)
    assert outcome.runs == {"r1", "r3"}


FAULTED = [*GRADED, run("r4", "system_error")]


def test_a_system_error_makes_a_fold_over_it_and_every_value_reading_it_wait() -> None:
    values = {
        "all_score": fold("min", "all", "score"),
        "st1": fold("min", "st1", "score"),
        "total": many("add", value("all_score"), value("st1")),
        "gone": many("add", value("total"), many("div", 1, 0)),
        "unread": {"block": "if", "test": 1, "then": value("st1"), "else": value("all_score")},
        "tested": {"block": "if", "test": value("all_score"), "then": 1, "else": 0},
    }

    outcomes = worked(values, runs=FAULTED, groups={"st1": ["r1", "r3"]})

    assert outcomes["all_score"].waiting
    assert outcomes["all_score"].runs == {"r1", "r2", "r3", "r4"}
    assert outcomes["total"].waiting
    assert outcomes["gone"].waiting
    assert not outcomes["st1"].waiting
    assert outcomes["unread"].number == Fraction(1, 2)
    assert outcomes["tested"].waiting


def test_what_a_value_read_is_kept_through_the_values_it_read() -> None:
    values = {
        "st1": fold("min", "st1", "score"),
        "late": many("gt", fact("seconds_late"), 0),
        "total": many("mul", value("st1"), many("sub", 1, value("late"))),
    }

    outcome = worked(values, runs=GRADED, groups={"st1": ["r1"]}, facts={"seconds_late": 5})[
        "total"
    ]

    assert outcome.number == 0
    assert outcome.runs == {"r1"}
    assert outcome.values == {"st1", "late"}
    assert outcome.facts == {"seconds_late"}


def test_the_formulas_of_14_1_give_their_fractions() -> None:
    day = 86400
    penalty = many(
        "mul",
        value("score"),
        many(
            "max",
            0,
            many(
                "sub",
                1,
                many("mul", Fraction(1, 10), one("ceil", many("div", fact("seconds_late"), day))),
            ),
        ),
    )
    rmse = many(
        "min",
        many("max", many("div", many("sub", Fraction(1, 2), value("rmse")), Fraction(1, 5)), 0),
        1,
    )
    late_at_all = many(
        "mul",
        {"block": "if", "test": many("gt", fact("seconds_late"), 0), "then": 0.9, "else": 1},
        value("score"),
    )

    def at(late: int, error: str = "0.4") -> dict[str, Outcome]:
        values = {
            "score": Fraction(4, 5),
            "rmse": Decimal(error),
            "penalty": penalty,
            "graded": rmse,
            "late_at_all": late_at_all,
        }
        return worked(values, facts={"seconds_late": late, "seconds": 0, "compiled": 1})

    assert at(0)["penalty"].number == Fraction(4, 5)
    assert at(1)["penalty"].number == Fraction(18, 25)
    assert at(day + 1)["penalty"].number == Fraction(16, 25)
    assert at(20 * day)["penalty"].number == 0
    assert at(0, "0.4")["graded"].number == Fraction(1, 2)
    assert at(0, "0.3")["graded"].number == 1
    assert at(0, "0.25")["graded"].number == 1
    assert at(0, "0.5")["graded"].number == 0
    assert at(0)["late_at_all"].number == Fraction(4, 5)
    assert at(1)["late_at_all"].number == Fraction(18, 25)


def test_min_given_numbers_or_a_groups_column_each_check_and_give_their_number() -> None:
    numbers = many("min", 3, 2)
    column = fold("min", "st1", "score")

    assert problems({"numbers": numbers, "column": column}) == []
    outcomes = worked(
        {"numbers": numbers, "column": column}, runs=GRADED, groups={"st1": ["r1", "r3"]}
    )
    assert outcomes["numbers"].number == 2
    assert outcomes["column"].number == Fraction(1, 2)


@pytest.mark.parametrize(
    "tree",
    [
        many("min", 3, {"runs": "st1"}),
        {"block": "min", "over": {"runs": "st1"}, "of": [3, 4]},
        {"block": "max", "over": 3, "of": {"run": ["st1", "score"]}},
        {"block": "min", "of": {"run": ["st1", "score"]}, "colour": "red"},
    ],
)
def test_min_given_inputs_that_fit_no_form_is_refused_naming_its_forms(tree: object) -> None:
    name = tree["block"] if isinstance(tree, dict) else ""

    assert problems({"v": tree}) == [
        (
            ("values", "v"),
            f"{name} is given numbers or a group's runs, and these inputs fit no form of it. "
            "It takes, given numbers, of; given a group's runs, over, of, where (optional), "
            "missing (optional).",
        )
    ]


def test_the_stage_4_fold_names_are_gone() -> None:
    for name in ("min_runs", "max_runs", "sum_runs", "count_runs"):
        assert name not in CATALOGUE
        assert problems({"v": fold(name, "st1", "score")})


def chain(nodes: int) -> object:
    """A tree of `nodes` nodes, a sum of 1s, two levels deep."""
    return {"block": "add", "of": [1] * (nodes - 1)}


def deep(depth: int) -> object:
    tree: object = 1
    for _ in range(depth - 1):
        tree = one("not", tree)
    return tree


def test_a_tree_of_more_than_1000_nodes_or_64_deep_is_refused() -> None:
    assert (MOST_NODES, MOST_DEPTH) == (1000, 64)
    assert tree_size(chain(1000)) == (1000, 2)
    assert tree_size(deep(64)) == (64, 64)
    assert problems({"v": chain(1000), "w": deep(64)}) == []
    assert problems({"v": chain(1001)}) == [
        (
            ("values", "v"),
            "This value has 1001 parts and is 2 deep. A value has at most 1000 parts and is "
            "at most 64 deep, so working it out stays quick.",
        )
    ]
    assert [path for path, _ in problems({"v": deep(65)})] == [("values", "v")]


def test_a_tree_far_deeper_than_python_recurses_is_refused_and_still_worked_out() -> None:
    tree = deep(20001)

    assert [path for path, _ in problems({"v": tree})] == [("values", "v")]
    assert number({"v": tree}) == 1


def test_a_block_the_catalogue_does_not_have_raises_when_read() -> None:
    with pytest.raises(ValueError, match="not a block Unicon has"):
        worked({"v": many("pow", 2, 3)})
    with pytest.raises(ValueError, match="fits no form of min"):
        worked({"v": many("min", 3, {"runs": "all"})})


def test_folds_nested_in_folds_are_worked_out_once_each() -> None:
    tree: object = 1
    for _ in range(6):
        tree = {"block": "sum", "over": {"runs": "all"}, "of": tree}
    runs = [run(f"r{index}", score=1) for index in range(10)]
    per_run = {
        "block": "sum",
        "over": {"runs": "all"},
        "of": many("mul", {"run": ["all", "score"]}, tree),
    }

    started = time.monotonic()
    outcomes = worked({"v": tree, "w": per_run}, runs=runs)

    assert time.monotonic() - started < 1
    assert outcomes["v"].number == 10**6
    assert outcomes["w"].number == 10**7


def test_a_number_past_4096_bits_is_missing_so_squaring_values_stay_quick() -> None:
    values: dict[str, object] = {"v0": many("mul", 1e300, 1e300)}
    for index in range(1, 17):
        values[f"v{index}"] = many("mul", value(f"v{index - 1}"), value(f"v{index - 1}"))

    started = time.monotonic()
    outcomes = worked(values)

    assert time.monotonic() - started < 1
    assert MOST_BITS == 4096
    assert outcomes["v1"].number == Fraction(10**1200)
    assert outcomes["v2"].missing
    assert outcomes["v16"].missing
    assert number({"v": many("div", 1, 2**4096)}) is None
    assert number({"v": many("add", 2**4095, 2**4095)}) is None


def test_a_decimal_with_a_huge_exponent_is_no_number_and_a_report_of_it_is_missing() -> None:
    started = time.monotonic()
    report = exact_json.loads('{"score": 1e2000000, "time_ms": 1e-2000000}')

    assert exact(report["score"]) is None
    assert exact(report["time_ms"]) is None
    assert time.monotonic() - started < 1
    assert exact(Decimal("1e1000")) == 10**1000
    runs = [run("r1", **report)]
    assert number({"v": fold("min", "all", "score")}, runs=runs) == 0
    assert number({"v": fold("max", "all", "time_ms")}, runs=runs) is None


def test_a_fact_that_is_not_a_number_raises() -> None:
    with pytest.raises(ValueError, match="the fact compiled is True, not a number"):
        worked({"v": fact("compiled")}, facts={"compiled": True})
    with pytest.raises(ValueError, match="the fact seconds is None"):
        worked({"v": fact("seconds")}, facts={"compiled": 1})
