"""What a grading run reports back through its callback, and whether a
verdict is one the package keeps.

A report is one JSON object with an `event`: `started` once the harness has
accepted its envelope, `progress` after each container with the step and how
many of its containers are done of how many, and `finished` with the
verdict. Anything else is not a report. A verdict is kept only when it is
at most `VERDICT_MAX` bytes as JSON, matches the runner's
`verdict.schema.json` and names the grading, stage and attempt it is posted
for; any other is the platform's failure, never a grade. The bound keeps
what the contestant and the organisers read of one grading, a summary and a
row per test, from growing without end.
"""

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.domain.contracts import violation
from forge.domain.errors import InvalidCallback

STEP_LIMIT = 200
SUMMARY_LIMIT = 500
VERDICT_MAX = 1024 * 1024


class Event(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class Report:
    """One report: its event, the step and the counts of a progress report,
    and the verdict of a finished one, as sent.
    """

    event: Event
    step: str | None = None
    done: int | None = None
    total: int | None = None
    verdict: Any = None

    @property
    def progress(self) -> dict[str, Any]:
        """The progress report as the row keeps it."""
        return {"step": self.step, "done": self.done, "total": self.total}


def read_report(body: bytes) -> Report:
    """The report `body` holds. `InvalidCallback` for one that is not a
    report.
    """
    try:
        document = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
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
            if "verdict" not in document:
                raise InvalidCallback("A finished report carries its verdict.")
            return Report(event, verdict=document["verdict"])


def _count(value: object) -> int | None:
    """A count as sent: a whole number, nothing below zero."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def verdict_problem(verdict: Any, *, grading: uuid.UUID, stage: str, attempt: int) -> str | None:
    """What is wrong with `verdict` as the verdict of this grading's attempt at
    this stage, in words for staff, or none when it is one to keep.
    """
    try:
        size = len(json.dumps(verdict, ensure_ascii=False, allow_nan=False).encode())
    except TypeError, ValueError:
        return "The verdict is not a JSON document."
    if size > VERDICT_MAX:
        return f"The verdict is {size} bytes, more than the {VERDICT_MAX} a verdict may be."
    broken = violation(verdict, "verdict")
    if broken is not None:
        return f"The verdict does not match verdict.schema.json {broken}"[:SUMMARY_LIMIT]
    try:
        named = uuid.UUID(str(verdict["grading_id"]))
    except ValueError:
        named = None
    if named != grading:
        return "The verdict names another grading."
    if (verdict["stage"], verdict["attempt"]) != (stage, attempt):
        return "The verdict names another stage or attempt."
    return None
