"""Which of a submission's attempts a board reads while a regrade is in
progress: where the submission stood before it, so a regrade moves no board
until it ends (TASK-FORMAT.md section 3.5; #407).
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from forge.db.tables import Grading
from forge.domain.boards import State
from forge.domain.definitions import OnSystemError
from forge.runtime.context import Context
from forge.services.scores import usable

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
CTX = cast(Context, SimpleNamespace(now=NOW))


def _attempt(attempt: int, status: str, result: dict[str, Any] | None = None) -> Grading:
    return Grading(
        attempt=attempt,
        status=status,
        result=result,
        falls_back=False,
        queued_at=NOW,
        dispatched_at=None,
        deadline_at=None,
        cancel_reason=None,
    )


def test_a_regrade_after_a_system_error_leaves_the_submission_grading() -> None:
    rows = [
        _attempt(1, "done", {"stopped": None}),
        _attempt(2, "system_error"),
        _attempt(3, "queued"),
    ]

    read = usable(CTX, rows, (), OnSystemError.GRADING)

    assert (read.state, read.row) == (State.GRADING, None)


def test_a_regrade_of_a_finished_attempt_holds_that_attempt() -> None:
    first = _attempt(1, "done", {"stopped": None})

    read = usable(CTX, [first, _attempt(2, "queued")], (), OnSystemError.GRADING)

    assert (read.state, read.row) == (State.CANDIDATE, first)


def test_a_regrade_after_a_system_error_holds_the_contests_fallback() -> None:
    first = _attempt(1, "done", {"stopped": None})
    rows = [first, _attempt(2, "system_error"), _attempt(3, "queued")]

    read = usable(CTX, rows, (), OnSystemError.LAST_RESULT)

    assert (read.state, read.row) == (State.CANDIDATE, first)
