"""Which of a submission's attempts a board reads, by one rule: the latest
that finished with a result, until a newer attempt finishes with one, so a
regrade in progress, a system error and a staff cancel never take a result
away; with none, still grading while the latest attempt is queued, running
or a system error, and nothing once it is cancelled or ended over the limit
(TASK-FORMAT.md section 3.5; #442, #445); and where a submission stands
against its task's `submissions.max` by the same reading.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from forge.db.tables import Grading
from forge.domain.grading import Place
from forge.runtime.context import Context
from forge.services.gradings import place_of
from forge.services.scores import State, counted, usable

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
CTX = cast(Context, SimpleNamespace(now=NOW))
REASON = "The checker cannot be mended."


def _attempt(
    attempt: int, status: str, result: dict[str, Any] | None = None, reason: str | None = None
) -> Grading:
    return Grading(
        attempt=attempt,
        status=status,
        result=result,
        automatic=False,
        queued_at=NOW,
        dispatched_at=None,
        deadline_at=None,
        cancel_reason=reason,
    )


def test_a_regrade_after_a_system_error_holds_the_last_result() -> None:
    first = _attempt(1, "done", {"stopped": None})
    rows = [first, _attempt(2, "system_error"), _attempt(3, "queued")]

    read = usable(CTX, rows, ())

    assert (read.state, read.row) == (State.CANDIDATE, first)
    assert counted(rows) is first
    assert place_of(CTX, rows) is not Place.DELAYED


def test_a_regrade_of_a_finished_attempt_holds_that_attempt() -> None:
    first = _attempt(1, "done", {"stopped": None})

    read = usable(CTX, [first, _attempt(2, "queued")], ())

    assert (read.state, read.row) == (State.CANDIDATE, first)


def test_a_system_error_never_takes_a_result_away() -> None:
    first = _attempt(1, "done", {"stopped": None})
    rows = [first, _attempt(2, "system_error"), _attempt(3, "system_error")]

    read = usable(CTX, rows, ())

    assert (read.state, read.row) == (State.CANDIDATE, first)
    assert place_of(CTX, rows) is not Place.DELAYED


def test_a_staff_cancel_after_a_result_keeps_the_result() -> None:
    first = _attempt(1, "done", {"stopped": None})
    rows = [first, _attempt(2, "cancelled", reason=REASON)]

    read = usable(CTX, rows, ())

    assert (read.state, read.row) == (State.CANDIDATE, first)
    assert counted(rows) is first


def test_a_staff_cancel_voids_only_a_submission_that_never_had_a_result() -> None:
    cancelled = _attempt(2, "cancelled", reason=REASON)
    rows = [_attempt(1, "system_error"), cancelled]

    read = usable(CTX, rows, ())

    assert (read.state, read.row) == (State.VOID, None)
    assert counted(rows) is cancelled


def test_a_newer_result_replaces_an_earlier_one_even_a_better_one() -> None:
    accepted = {"stopped": None, "tests": [{"test": "main/1", "outcome": "accepted"}]}
    wrong = {"stopped": None, "tests": [{"test": "main/1", "outcome": "wrong_answer"}]}
    newer = _attempt(3, "done", wrong)
    rows = [_attempt(1, "done", accepted), _attempt(2, "system_error"), newer]

    read = usable(CTX, rows, ())

    assert (read.state, read.row) == (State.CANDIDATE, newer)


def test_a_submission_with_no_result_is_pending_while_graded_or_failed() -> None:
    failed = [_attempt(1, "system_error")]
    retried = [_attempt(1, "system_error"), _attempt(2, "running")]

    assert usable(CTX, failed, ()) == usable(CTX, retried, ())
    assert usable(CTX, failed, ()).state is State.GRADING
    assert place_of(CTX, failed) is Place.DELAYED
    assert place_of(CTX, retried) is not Place.DELAYED


def test_a_submission_ended_over_the_limit_is_void_and_holds_no_place() -> None:
    over = _attempt(2, "over_limit")
    rows = [_attempt(1, "system_error"), over]

    read = usable(CTX, rows, ())

    assert (read.state, read.row) == (State.VOID, None)
    assert place_of(CTX, rows) is not Place.DELAYED
    assert place_of(CTX, rows) is Place.NONE


def test_where_a_submission_stands_against_the_most() -> None:
    graded = [_attempt(1, "done", {"stopped": None}), _attempt(2, "system_error")]
    waiting = [_attempt(1, "queued")]
    failed = [_attempt(1, "system_error")]
    cancelled = [_attempt(1, "cancelled", reason=REASON)]

    assert place_of(CTX, graded) is Place.HOLDS
    assert place_of(CTX, waiting) is Place.HOLDS
    assert place_of(CTX, failed) is Place.DELAYED
    assert place_of(CTX, cancelled) is Place.NONE
