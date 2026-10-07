"""What a grading run reports back through its callback, and whether a
result is one the package keeps.

A report is one JSON object with an `event`: `started` once the harness has
accepted its envelope, `progress` after each container with the step and how
many of its containers are done of how many, and `finished` with the result.
Anything else is not a report. The report is read with its numbers exactly
as written (`forge.domain.exact_json`), so a result's values reach the
grading's row without a digit changing. A result is kept only when it is at
most `RESULT_MAX` bytes as JSON and matches the runner's
`result.schema.json`; any other is the platform's failure, never a grade.
The grading it is for is the one whose callback token it came with. The
bound keeps what the contestant and the organisers read of one grading, a
row per test and its values, from growing without end.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.domain import exact_json
from forge.domain.contracts import violation
from forge.domain.errors import InvalidCallback

STEP_LIMIT = 200
ERROR_LIMIT = 500
RESULT_MAX = 1024 * 1024


class Event(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class Report:
    """One report: its event, the step and the counts of a progress report,
    and the result of a finished one, as sent.
    """

    event: Event
    step: str | None = None
    done: int | None = None
    total: int | None = None
    result: Any = None

    @property
    def progress(self) -> dict[str, Any]:
        """The progress report as the row keeps it."""
        return {"step": self.step, "done": self.done, "total": self.total}


def read_report(body: bytes) -> Report:
    """The report `body` holds. `InvalidCallback` for one that is not a
    report.
    """
    try:
        document = exact_json.loads(body)
    except UnicodeDecodeError, ValueError, RecursionError:
        raise InvalidCallback("The report is not JSON.") from None
    if not isinstance(document, dict):
        raise InvalidCallback("The report is not a JSON object.")
    try:
        event = Event(str(document.get("event")))
    except ValueError:
        raise InvalidCallback("The report names no event this platform takes.") from None
    match event:
        case Event.STARTED:
            return Report(event)
        case Event.PROGRESS:
            step = document.get("step")
            done, total = _count(document.get("done")), _count(document.get("total"))
            if not isinstance(step, str) or not step or len(step) > STEP_LIMIT:
                raise InvalidCallback("A progress report names its step.")
            if done is None or total is None or done > total:
                raise InvalidCallback("A progress report counts what is done of a total.")
            return Report(event, step=step, done=done, total=total)
        case Event.FINISHED:
            if "result" not in document:
                raise InvalidCallback("A finished report carries its result.")
            return Report(event, result=document["result"])


def _count(value: object) -> int | None:
    """A count as sent: a whole number, nothing below zero."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def result_problem(result: Any) -> str | None:
    """What is wrong with `result` as a result, in words for staff, or none
    when it is one to keep.
    """
    try:
        size = len(exact_json.dumps(result).encode())
    except ValueError, RecursionError:
        return "The result is not a JSON document."
    if size > RESULT_MAX:
        return f"The result is {size} bytes, more than the {RESULT_MAX} a result may be."
    broken = violation(result, "result")
    if broken is not None:
        return f"The result does not match result.schema.json {broken}"[:ERROR_LIMIT]
    return None
