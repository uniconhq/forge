"""What a regrade did to one submission, without a database (#448,
TASK-FORMAT.md section 3.5): a finished result beside an earlier one is
unchanged, and with nothing to compare it is newly graded; what changed is
said by its values and its runs' verdicts.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from forge.domain.ids import PublicationId, VersionId
from forge.domain.publication_record import Publication
from forge.domain.regrades import (
    VERDICTS_SHOWN,
    ChangeKind,
    RegradeCause,
    Side,
    VerdictChange,
    change_of,
    counted,
    counted_under,
    run_changes,
)
from forge.domain.run_results import Worked


def test_a_finished_result_is_unchanged_beside_an_earlier_one_and_new_without() -> None:
    assert change_of(Side(1)) is ChangeKind.UNCHANGED
    assert change_of(None) is ChangeKind.NEW


def test_every_change_is_counted_zero_where_none() -> None:
    found = counted([ChangeKind.NEW, ChangeKind.NEW, ChangeKind.FAILED])

    assert found[ChangeKind.NEW] == 2 and found[ChangeKind.FAILED] == 1
    assert set(found) == set(ChangeKind) and found[ChangeKind.UNCHANGED] == 0


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


def _worked(verdicts: dict[str, str]) -> Worked:
    """A grading worked out with no value, each run getting its verdict."""
    read = {run: SimpleNamespace(verdict=verdict) for run, verdict in verdicts.items()}
    return cast(Worked, SimpleNamespace(outcomes={}, grading=SimpleNamespace(read=read)))


def test_every_run_changing_alike_is_one_line_and_many_runs_are_counted() -> None:
    every = {f"r{n}": "accepted" for n in range(1, 1001)}
    alike = run_changes(_worked(every), _worked(dict.fromkeys(every, "compile_error")))
    some = run_changes(
        _worked(every), _worked({**every, **{f"r{n}": "wrong_answer" for n in range(2, 60, 2)}})
    )
    one = run_changes(_worked({"r1": "accepted"}), _worked({"r1": "wrong_answer"}))

    assert alike.verdicts == (VerdictChange(None, "accepted", "compile_error", 1000),)
    assert alike.more_verdicts == 0
    assert [each.run for each in some.verdicts] == [f"r{n}" for n in range(2, 42, 2)]
    assert len(some.verdicts) == VERDICTS_SHOWN and some.more_verdicts == 9
    assert one.verdicts == (VerdictChange("r1", "accepted", "wrong_answer"),)
