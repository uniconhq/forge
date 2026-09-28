"""The clock the package reads the time from: the system's in a running
process, and one a test moves by hand.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock a test moves by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, by: timedelta) -> None:
        self._now += by

    def set(self, moment: datetime) -> None:
        self._now = moment
