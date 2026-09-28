"""What a building block runs with: the transaction of the unit of work, the
forge, the settings and the clock, and `own_transaction` for the few writes
that must land whatever the unit of work does. The action that opened the
unit of work commits or rolls back `db`; a building block never does.

`ActionSetup` is what a context is opened on: an action, and the plain
functions beside it, take one from a setup.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.engine import TransactionFactory
from forge.domain.clock import Clock
from forge.port import Forge
from forge.settings import Settings


@asynccontextmanager
async def transaction(factory: TransactionFactory) -> AsyncIterator[AsyncSession]:
    """One transaction from `factory`: committed when the block ends, rolled
    back when it raises, and closed either way. A commit that fails raises
    out of the block.
    """
    async with factory() as db:
        try:
            yield db
        except BaseException:
            await db.rollback()
            raise
        await db.commit()


@dataclass(frozen=True, slots=True)
class Context:
    db: AsyncSession
    forge: Forge
    settings: Settings
    clock: Clock
    _transactions: TransactionFactory = field(repr=False, kw_only=True)

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def own_transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        """A short transaction apart from `db`, committed when the block ends
        and rolled back when it raises, for a write that must land whatever
        the unit of work does afterwards.
        """
        return transaction(self._transactions)


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
