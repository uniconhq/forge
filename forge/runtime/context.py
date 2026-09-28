"""What a building block runs with: the transaction of the unit of work, the
factory for work that needs a transaction of its own, the forge, the
settings and the clock. The action that opened the unit of work commits or
rolls back `db`; a building block never does.

`ActionSetup` is what a context is opened on: an action, and the plain
functions beside it, take one from a setup.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.engine import TransactionFactory
from forge.domain.clock import Clock
from forge.port import Forge
from forge.settings import Settings


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


@runtime_checkable
class ActionSetup(Protocol):
    """What an action and the plain functions beside it take from a setup."""

    @property
    def forge(self) -> Forge: ...

    @property
    def clock(self) -> Clock: ...

    @property
    def settings(self) -> Settings: ...

    def unit_of_work(self) -> AbstractAsyncContextManager[Context]: ...

    async def ready(self) -> None: ...

    async def stop(self) -> None: ...
