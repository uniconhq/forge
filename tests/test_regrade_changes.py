"""What a regrade did to one submission, without a database (#448,
TASK-FORMAT.md section 3.5): its points went up or down, it now stops
before its tests, or nothing changed; on a task that gives no points, the
share of its tests accepted and then its values with a direction decide,
any value worse reading as down; with nothing to compare, it is newly
graded.
"""

from fractions import Fraction

from forge.domain.regrades import Change, Side, change_of, counted
from forge.domain.scoring import Better


def _points(points: int, *, stopped: str | None = None) -> Side:
    return Side(1, Fraction(points), 1 if points else 0, 1, stopped)


def _valued(accepted: int, tests: int, **values: tuple[str, Better]) -> Side:
    return Side(
        1,
        None,
        accepted,
        tests,
        values={name: Fraction(value) for name, (value, _) in values.items()},
        better={name: better for name, (_, better) in values.items()},
    )


def test_points_say_up_down_or_unchanged() -> None:
    assert change_of(_points(40), _points(100)) is Change.UP
    assert change_of(_points(100), _points(40)) is Change.DOWN
    assert change_of(_points(100), _points(100)) is Change.UNCHANGED


def test_a_run_now_stopped_before_its_tests_is_told_apart_whatever_its_points() -> None:
    assert change_of(_points(0), _points(0, stopped="compile_error")) is Change.STOPPED
    assert change_of(_points(100), _points(0, stopped="compile_error")) is Change.STOPPED
    # Stopped before and after is read by its points.
    assert (
        change_of(_points(0, stopped="compile_error"), _points(0, stopped="compile_error"))
        is Change.UNCHANGED
    )
    assert change_of(_points(0, stopped="compile_error"), _points(100)) is Change.UP


def test_with_nothing_to_compare_it_is_newly_graded() -> None:
    assert change_of(None, _points(100)) is Change.NEW
    assert change_of(_points(100), None) is Change.NEW


def test_with_no_points_the_share_accepted_decides_first() -> None:
    assert change_of(_valued(8, 10), _valued(9, 12)) is Change.DOWN
    assert change_of(_valued(1, 2), _valued(3, 4)) is Change.UP


def test_with_the_same_share_any_value_worse_is_down() -> None:
    lower, higher = Better.LOWER, Better.HIGHER
    before = _valued(2, 2, time_ms=("100", lower), score=("7", higher))

    assert change_of(before, _valued(2, 2, time_ms=("90", lower), score=("7", higher))) is (
        Change.UP
    )
    assert change_of(before, _valued(2, 2, time_ms=("90", lower), score=("6", higher))) is (
        Change.DOWN
    )
    assert change_of(before, _valued(2, 2, time_ms=("100", lower), score=("7", higher))) is (
        Change.UNCHANGED
    )


def test_every_change_is_counted_zero_where_none() -> None:
    found = counted([Change.UP, Change.UP, Change.FAILED])

    assert found[Change.UP] == 2 and found[Change.FAILED] == 1
    assert set(found) == set(Change) and found[Change.DOWN] == 0
