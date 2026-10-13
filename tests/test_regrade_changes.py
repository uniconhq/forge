"""What a regrade did to one submission, without a database (#448,
TASK-FORMAT.md section 3.5): its points went up or down, it now stops
before its tests, or nothing changed; on a task that gives no points, the
share of its tests accepted and then its values with a direction decide,
any value worse reading as down; with nothing to compare, it is newly
graded.
"""

from datetime import UTC, datetime
from fractions import Fraction

from forge.domain.definitions import Fold
from forge.domain.ids import PublicationId, VersionId
from forge.domain.publications import Publication
from forge.domain.regrades import (
    ChangeKind,
    RegradeCause,
    Side,
    change_of,
    counted,
    counted_under,
)
from forge.domain.scoring import Better


def _points(points: int, *, stopped: str | None = None) -> Side:
    return Side(1, Fraction(points), 1 if points else 0, 1, stopped)


def _valued(accepted: int, tests: int, **values: tuple[str, Better]) -> Side:
    return Side(
        1,
        None,
        accepted,
        tests,
        values={(name, Fold.MAX): Fraction(value) for name, (value, _) in values.items()},
        better={name: better for name, (_, better) in values.items()},
    )


def test_points_say_up_down_or_unchanged() -> None:
    assert change_of(_points(40), _points(100)) is ChangeKind.UP
    assert change_of(_points(100), _points(40)) is ChangeKind.DOWN
    assert change_of(_points(100), _points(100)) is ChangeKind.UNCHANGED


def test_a_run_now_stopped_before_its_tests_is_told_apart_whatever_its_points() -> None:
    assert change_of(_points(0), _points(0, stopped="compile_error")) is ChangeKind.STOPPED
    assert change_of(_points(100), _points(0, stopped="compile_error")) is ChangeKind.STOPPED
    # Stopped before and after is read by its points.
    assert (
        change_of(_points(0, stopped="compile_error"), _points(0, stopped="compile_error"))
        is ChangeKind.UNCHANGED
    )
    assert change_of(_points(0, stopped="compile_error"), _points(100)) is ChangeKind.UP


def test_with_nothing_to_compare_it_is_newly_graded() -> None:
    assert change_of(None, _points(100)) is ChangeKind.NEW
    assert change_of(_points(100), None) is ChangeKind.NEW


def test_with_no_points_the_share_accepted_decides_first() -> None:
    assert change_of(_valued(8, 10), _valued(9, 12)) is ChangeKind.DOWN
    assert change_of(_valued(1, 2), _valued(3, 4)) is ChangeKind.UP


def test_with_the_same_share_any_value_worse_is_down() -> None:
    lower, higher = Better.LOWER, Better.HIGHER
    before = _valued(2, 2, time_ms=("100", lower), score=("7", higher))

    assert change_of(before, _valued(2, 2, time_ms=("90", lower), score=("7", higher))) is (
        ChangeKind.UP
    )
    assert change_of(before, _valued(2, 2, time_ms=("90", lower), score=("6", higher))) is (
        ChangeKind.DOWN
    )
    assert change_of(before, _valued(2, 2, time_ms=("100", lower), score=("7", higher))) is (
        ChangeKind.UNCHANGED
    )


def test_a_value_on_one_side_only_reads_as_worst_where_it_is_missing() -> None:
    # A board reads a missing value as the worst (#441): losing one is
    # worse, even beside one that improved; gaining one is better.
    lower, higher = Better.LOWER, Better.HIGHER
    both = _valued(2, 2, time_ms=("100", lower), score=("7", higher))

    assert change_of(both, _valued(2, 2, score=("8", higher))) is ChangeKind.DOWN
    assert change_of(_valued(2, 2, score=("7", higher)), both) is ChangeKind.UP


def test_every_change_is_counted_zero_where_none() -> None:
    found = counted([ChangeKind.UP, ChangeKind.UP, ChangeKind.FAILED])

    assert found[ChangeKind.UP] == 2 and found[ChangeKind.FAILED] == 1
    assert set(found) == set(ChangeKind) and found[ChangeKind.DOWN] == 0


def _publication(number: int) -> Publication:
    return Publication(
        PublicationId(f"acme/spring/sum#{number}"),
        number,
        VersionId(f"v{number}"),
        False,
        (),
        datetime(2026, 10, 13, tzinfo=UTC),
    )


def test_a_result_counted_under_the_latest_publication_before_the_regrade() -> None:
    every = [_publication(1), _publication(2), _publication(3)]
    third = PublicationId("acme/spring/sum#3")

    # A save's regrade grades against the publication the save made; a
    # rejudge against the one that was already the latest.
    assert counted_under(RegradeCause.SAVE, third, every) == "acme/spring/sum#2"
    assert counted_under(RegradeCause.REJUDGE, third, every) == third
    assert counted_under(RegradeCause.SAVE, PublicationId("acme/spring/sum#1"), every) == (
        "acme/spring/sum#1"
    )
