"""What the fakes share, as the services they stand in for share a
deployment: one clock, one record of every call made through the port in the
order it was made, whichever fake took it, and one switch that takes them all
down at once.
"""

from dataclasses import dataclass
from typing import Any

from forge.domain.clock import Clock, SystemClock
from forge.domain.errors import Unavailable
from forge.domain.identity import Identity


@dataclass(frozen=True, slots=True)
class Call:
    """One call through the port: which operation, as whom, with what."""

    operation: str
    identity: Identity
    arguments: dict[str, Any]


class FakeWorld:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()
        self.calls: list[Call] = []
        self.unavailable = False

    def record(self, operation: str, identity: Identity, **arguments: Any) -> None:
        self.calls.append(Call(operation, identity, arguments))

    def check_up(self) -> None:
        if self.unavailable:
            raise Unavailable("the fake forge is switched off")
