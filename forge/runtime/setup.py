"""The package set up for one process: the settings, the forge behind the
port picked by `UNICON_FORGE`, the database and the clock. `start` builds
the one setup the process holds and every action opens its unit of work on
it; `ready` asks its database; `stop` tears it down; `public_url` is where
the platform is served. Nothing runs in the background: every piece of work
is done by the request that asks for it.
"""

import asyncio
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from pydantic import HttpUrl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge import forges
from forge.db.engine import (
    PoolSize,
    TransactionFactory,
    new_engine,
    new_probe_engine,
    new_transaction_factory,
    ping,
)
from forge.domain.clock import Clock, SystemClock
from forge.domain.errors import NotReady
from forge.domain.keys import KeyMaker, random_key
from forge.domain.live import CHANNEL, Nudge
from forge.log import get_logger
from forge.port import Forge
from forge.runtime.broker import Broker
from forge.runtime.context import ActionSetup, AfterCommit, AfterRollback, Context, transaction
from forge.runtime.held import held, hold, holding, release, setup_or_held
from forge.runtime.memo import Memo
from forge.settings import Settings, load_settings

log = get_logger(__name__)

READY_TIMEOUT_SECONDS = 2.0
AT_ONCE_AFTER_COMMIT = 8
"""How many pieces of work left for after a commit run at once, so a rejudge
of hundreds starts its runs in a fraction of the time and still leaves the
pool of connections to everyone else."""


class Setup:
    def __init__(
        self,
        *,
        settings: Settings,
        forge: Forge,
        engine: AsyncEngine,
        probe_engine: AsyncEngine,
        transactions: TransactionFactory,
        clock: Clock,
        keys: KeyMaker = random_key,
    ) -> None:
        self._settings = settings
        self._forge = forge
        self._engine = engine
        self._probe_engine = probe_engine
        self._transactions = transactions
        self._clock = clock
        self._keys = keys
        self._memo = Memo(clock)
        self._broker = Broker(str(settings.database_url))
        self._refreshing: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        callback_path: str,
        forge: Forge | None = None,
        clock: Clock | None = None,
        keys: KeyMaker = random_key,
    ) -> Setup:
        """Assemble the package. `callback_path` is the hosting process's own
        route that the host sends a browser back to after sign-in; it is
        joined to the public URL. `forge`, when given, is used as it is, in
        place of the one the settings pick. `keys` makes the key each newly
        named org, contest and task is filed under: a random one, unless a
        test files things under their names.
        """
        if not callback_path.startswith("/"):
            raise ValueError(f"callback_path is not a path: {callback_path}")
        engine = new_engine(
            str(settings.database_url),
            PoolSize(
                kept=settings.database_pool_size,
                overflow=settings.database_pool_overflow,
                wait=settings.database_pool_wait,
            ),
        )
        setup = cls(
            settings=settings,
            forge=forge
            or forges.build(
                settings, sign_in_redirect_uri=_joined(settings.public_url, callback_path)
            ),
            engine=engine,
            probe_engine=new_probe_engine(str(settings.database_url)),
            transactions=new_transaction_factory(engine),
            clock=clock or SystemClock(),
            keys=keys,
        )
        return setup

    @property
    def forge(self) -> Forge:
        return self._forge

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def broker(self) -> Broker:
        return self._broker

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[Context]:
        """One transaction and the context over it: committed when the block
        ends, rolled back when it raises, and closed either way. A commit that
        fails raises out of the block. Once it has committed, the work the
        block left for then runs, with the work left for its end, each on a
        unit of work of its own and up to `AT_ONCE_AFTER_COMMIT` at a time;
        one that fails is logged, since what the block did has landed. When
        it rolls back instead, the work left for that runs, the latest first,
        then the work left for its end, before the error goes on. Either way
        the block's own connection is back in the pool before any of it runs.
        The nudges the block left are published just before it commits.
        """
        later: list[AfterCommit] = []
        undo: list[AfterRollback] = []
        ended: list[AfterCommit] = []
        try:
            async with transaction(self._transactions) as db:
                ctx = Context(
                    db=db,
                    forge=self._forge,
                    settings=self._settings,
                    clock=self._clock,
                    _refresh_lock=self.refresh_lock,
                    memo=self._memo,
                    make_key=self._keys,
                    committed=later,
                    rolled_back=undo,
                    ended=ended,
                )
                yield ctx
                await _publish(db, ctx.nudges)
        except Exception:
            await _after_rollback(undo)
            await self._run_after(ended)
            raise
        await self._run_after(later + ended)

    async def _run_after(self, work: list[AfterCommit]) -> None:
        if work:
            room = asyncio.Semaphore(AT_ONCE_AFTER_COMMIT)
            await asyncio.gather(*(self._after_commit(each, room) for each in work))

    async def _after_commit(self, work: AfterCommit, room: asyncio.Semaphore) -> None:
        async with room:
            try:
                async with self.unit_of_work() as ctx:
                    await work(ctx)
            except Exception:
                log.exception("setup.after_commit_failed")

    def refresh_lock(self, session_id: uuid.UUID) -> asyncio.Lock:
        """The one lock this setup's units of work take while refreshing the
        session's credential, so two requests in the process refresh it once.
        It lives as long as someone holds it.
        """
        lock = self._refreshing.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._refreshing[session_id] = lock
        return lock

    async def ready(self) -> None:
        """Raise `NotReady` unless the database answers within two seconds.
        The cause is logged and kept out of the error.
        """
        try:
            await ping(self._probe_engine, READY_TIMEOUT_SECONDS)
        except Exception as exc:
            log.warning("setup.not_ready", error=type(exc).__name__, detail=str(exc))
            raise NotReady("The database did not answer.") from exc

    async def stop(self) -> None:
        await self._broker.stop()
        await self._forge.aclose()
        await self._probe_engine.dispose()
        await self._engine.dispose()


async def _publish(db: AsyncSession, nudges: list[Nudge]) -> None:
    """Every nudge in one statement, on the transaction about to commit."""
    if nudges:
        await db.execute(
            text(
                "SELECT pg_notify(:channel, payload) FROM unnest(CAST(:payloads AS text[])) payload"
            ),
            {"channel": CHANNEL, "payloads": [nudge.payload() for nudge in nudges]},
        )


async def _after_rollback(undo: list[AfterRollback]) -> None:
    """Each piece of work left for a rollback, the latest first; one that
    fails is logged and the rest still run.
    """
    for work in reversed(undo):
        try:
            await work()
        except Exception:
            log.exception("setup.after_rollback_failed")


def start(*, callback_path: str) -> None:
    """Build the setup the process holds from the `UNICON_*` settings.
    `callback_path` is the hosting process's sign-in callback route, the one
    thing the package cannot know on its own; it is joined to
    `UNICON_PUBLIC_URL`. Call it once, from inside the running event loop.
    """
    if holding():
        raise RuntimeError("forge.api.start was already called")
    hold(Setup.build(load_settings(), callback_path=callback_path))


async def ready() -> None:
    """Raise `NotReady` if the database does not answer within two seconds."""
    await held().ready()


async def stop() -> None:
    """Close every connection."""
    await release().stop()


def now(*, setup: ActionSetup | None = None) -> datetime:
    """The current instant by the clock the package enforces deadlines with."""
    return setup_or_held(setup).clock.now()


def public_url(*, setup: ActionSetup | None = None) -> str:
    """Where the platform is served, `UNICON_PUBLIC_URL`."""
    return str(setup_or_held(setup).settings.public_url)


def _joined(base: HttpUrl, path: str) -> str:
    return str(base).rstrip("/") + path
