"""What a building block runs with: the transaction of the unit of work, the
forge, the settings and the clock, `own_transaction` for the few writes that
must land whatever the unit of work does, `after_commit` for work that may
only start once the unit of work has committed, `after_rollback` for undoing
what it did outside the database when it rolls back instead, `refresh_lock`,
the setup's lock on refreshing one session's credential, `memo`, the answers the setup keeps
for a few seconds, and `make_key`, which makes the key a newly named org,
contest or task is filed under. The action that opened the unit
of work commits or rolls back `db`; a building block never does.

`ActionSetup` is what a context is opened on: an action, and the plain
functions beside it, take one from a setup.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.engine import TransactionFactory
from forge.domain.clock import Clock
from forge.domain.keys import KeyMaker, random_key
from forge.port import Forge
from forge.runtime.memo import Memo
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


type AfterCommit = Callable[[Context], Awaitable[None]]
type AfterRollback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Context:
    db: AsyncSession
    forge: Forge
    settings: Settings
    clock: Clock
    _transactions: TransactionFactory = field(repr=False, kw_only=True)
    _refresh_lock: Callable[[uuid.UUID], asyncio.Lock] = field(repr=False, kw_only=True)
    memo: Memo = field(repr=False, kw_only=True)
    make_key: KeyMaker = field(default=random_key, repr=False, kw_only=True)
    committed: list[AfterCommit] = field(default_factory=list, repr=False, kw_only=True)
    rolled_back: list[AfterRollback] = field(default_factory=list, repr=False, kw_only=True)

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def own_transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        """A short transaction apart from `db`, committed when the block ends
        and rolled back when it raises, for a write that must land whatever
        the unit of work does afterwards.
        """
        return transaction(self._transactions)

    def after_commit(self, work: AfterCommit) -> None:
        """Run `work` once this unit of work has committed, on a unit of work
        of its own, for something another process must be able to read the
        committed rows for, such as starting a grading's run, whose CI asks
        the platform about the grading at once. Nothing runs when the unit
        of work rolls back. The work owns its unit of work, and may commit it
        partway to let go of its connection before a slow call.
        """
        self.committed.append(work)

    def after_rollback(self, work: AfterRollback) -> None:
        """Run `work` if this unit of work rolls back, whether something in
        it raised or its commit failed: once the transaction is rolled back
        and before the error leaves it, for undoing what it made outside the
        database, at the forge or the CI. The work added last runs first.
        Work that fails is logged and the error the unit of work raised is
        raised still. Nothing runs when it commits, and nothing when it is
        cancelled, which leaves what it made like a crash would.
        """
        self.rolled_back.append(work)

    def refresh_lock(self, session_id: uuid.UUID) -> asyncio.Lock:
        """The lock one process holds while it refreshes a session's
        credential, the same for every unit of work on the setup.
        """
        return self._refresh_lock(session_id)


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

    def refresh_lock(self, session_id: uuid.UUID) -> asyncio.Lock: ...
