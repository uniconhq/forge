"""What a building block runs with: the transaction of the unit of work, the
forge, the settings and the clock, `after_commit` for work that may
only start once the unit of work has committed, `after_rollback` for undoing
what it did outside the database when it rolls back instead, `after_end` for
the few writes that must land whatever the unit of work does, `let_go` for
an action that has only read to hand its connection back before a slow
call, `nudge` for a live update sent when the unit of work commits,
`refresh_lock`,
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

from forge.db.engine import TransactionFactory, wrote
from forge.domain.clock import Clock
from forge.domain.keys import KeyMaker, random_key
from forge.domain.live import Nudge
from forge.port import Forge
from forge.runtime.broker import Broker
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
    _refresh_lock: Callable[[uuid.UUID], asyncio.Lock] = field(repr=False, kw_only=True)
    memo: Memo = field(repr=False, kw_only=True)
    make_key: KeyMaker = field(default=random_key, repr=False, kw_only=True)
    committed: list[AfterCommit] = field(default_factory=list, repr=False, kw_only=True)
    background: list[AfterCommit] = field(default_factory=list, repr=False, kw_only=True)
    rolled_back: list[AfterRollback] = field(default_factory=list, repr=False, kw_only=True)
    ended: list[AfterCommit] = field(default_factory=list, repr=False, kw_only=True)
    nudges: list[Nudge] = field(default_factory=list, repr=False, kw_only=True)
    _stopping: Callable[[], bool] = field(default=lambda: False, repr=False, kw_only=True)

    @property
    def now(self) -> datetime:
        return self.clock.now()

    async def let_go(self) -> None:
        """Hand the connection back to the pool before a slow call to the
        forge or the CI, in an action that has only read so far, so a page
        that waits seconds on the forge does not keep a connection that whole
        time. What was read stays usable, and the next query takes a
        connection again in a transaction of its own. A unit of work that has
        written or holds a lock keeps its connection, since ending its
        transaction there would break its all or nothing, so an action called
        inside another's unit of work may let go whatever its caller did.
        """
        if not wrote(self.db):
            await self.db.commit()

    def after_commit(self, work: AfterCommit) -> None:
        """Run `work` once this unit of work has committed, on a unit of work
        of its own, for something another process must be able to read the
        committed rows for, such as starting a grading's run, whose CI asks
        the platform about the grading at once. Nothing runs when the unit
        of work rolls back. The work owns its unit of work, and may commit it
        partway to let go of its connection before a slow call.
        """
        self.committed.append(work)

    def in_background(self, work: AfterCommit) -> None:
        """Start `work` once this unit of work has committed, on a unit of
        work of its own, and answer without waiting for it: for work the
        person who asked does not wait on, and that something else finishes
        when it never runs, such as making contestants' places to submit
        before their first upload, which that upload makes itself when it
        finds one missing. Nothing starts when the unit of work rolls back.
        Work that fails is logged, and a process that stops cuts short what
        is running. The work owns its unit of work, and may commit it
        partway to let go of its connection before a slow call.
        """
        self.background.append(work)

    def stopping(self) -> bool:
        """Whether the process is stopping, for work in the background to
        finish what it is doing and start nothing more.
        """
        return self._stopping()

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

    def nudge(self, nudge: Nudge) -> None:
        """Tell the streams that hear it that something changed, once this
        unit of work commits: the nudges are published in its own
        transaction just before the commit, so Postgres delivers them with
        it and never for a unit of work that rolls back.
        """
        self.nudges.append(nudge)

    def after_end(self, work: AfterCommit) -> None:
        """Run `work` once this unit of work has ended, committed or rolled
        back, on a unit of work of its own, for a write that must land
        whatever the unit of work does: noting that a session was used, or
        ending one the forge has refused, before the error that refusal
        raises rolls the rest back. It runs once this unit of work has let
        go of its connection, so it never waits on the pool while holding
        one. Work that fails is logged; the unit of work's own answer or
        error stands.
        """
        self.ended.append(work)

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

    @property
    def broker(self) -> Broker: ...
