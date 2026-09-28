"""What a building block runs with: the transaction of the unit of work, the
factory for work that needs a transaction of its own, the forge, the
settings and the clock. The action that opened the unit of work commits or
rolls back `db`; a building block never does.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.engine import TransactionFactory
from forge.port import Forge
from forge.settings import Settings


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Context:
    db: AsyncSession
    transactions: TransactionFactory
    forge: Forge
    settings: Settings
    clock: Clock

    @property
    def now(self) -> datetime:
        return self.clock.now()
