"""The package set up for one process: the settings, the forge behind the
port picked by `UNICON_FORGE`, the database, the clock, and the background
loops. `start` builds the one setup the process holds and every action opens
its unit of work on it; `ready` asks its database; `stop` tears it down;
`public_url` is where the platform is served.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge import forges
from forge.db.engine import (
    TransactionFactory,
    new_engine,
    new_probe_engine,
    new_transaction_factory,
    ping,
)
from forge.domain.clock import Clock, SystemClock
from forge.domain.errors import NotReady
from forge.log import get_logger
from forge.port import Forge
from forge.runtime import held
from forge.runtime.background import Loops, TimedPass
from forge.runtime.context import ActionSetup, Context, transaction
from forge.services import sessions
from forge.settings import Settings, load_settings

log = get_logger(__name__)

READY_TIMEOUT_SECONDS = 2.0
SESSION_SWEEP_INTERVAL = timedelta(hours=1)


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
    ) -> None:
        self._settings = settings
        self._forge = forge
        self._engine = engine
        self._probe_engine = probe_engine
        self._transactions = transactions
        self._clock = clock
        self._loops = Loops()

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        callback_path: str,
        forge: Forge | None = None,
        clock: Clock | None = None,
    ) -> Setup:
        """Assemble the package. `callback_path` is the hosting process's own
        route that the host sends a browser back to after sign-in; it is
        joined to the public URL. `forge`, when given, is used as it is, in
        place of the one the settings pick.
        """
        if not callback_path.startswith("/"):
            raise ValueError(f"callback_path is not a path: {callback_path}")
        engine = new_engine(str(settings.database_url))
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
        )
        setup._loops.add(TimedPass("sessions.sweep", setup._sweep_sessions, SESSION_SWEEP_INTERVAL))
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

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[Context]:
        """One transaction and the context over it: committed when the block
        ends, rolled back when it raises, and closed either way. A commit that
        fails raises out of the block.
        """
        async with transaction(self._transactions) as db:
            yield Context(
                db=db,
                forge=self._forge,
                settings=self._settings,
                clock=self._clock,
                _transactions=self._transactions,
            )

    def start_background(self) -> None:
        self._loops.start(self._transactions)

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
        await self._loops.stop()
        await self._forge.aclose()
        await self._probe_engine.dispose()
        await self._engine.dispose()

    async def _sweep_sessions(self, db: AsyncSession) -> None:
        await sessions.sweep(db, self._settings, self._clock.now())


def start(*, callback_path: str) -> None:
    """Build the setup the process holds from the `UNICON_*` settings and
    start its background loops. `callback_path` is the hosting process's
    sign-in callback route, the one thing the package cannot know on its own;
    it is joined to `UNICON_PUBLIC_URL`. Call it once, from inside the running
    event loop.
    """
    if held.holding():
        raise RuntimeError("forge.api.start was already called")
    setup = Setup.build(load_settings(), callback_path=callback_path)
    held.hold(setup)
    setup.start_background()


async def ready() -> None:
    """Raise `NotReady` if the database does not answer within two seconds."""
    await held.held().ready()


async def stop() -> None:
    """Stop the background loops and close every connection."""
    await held.release().stop()


def now(*, setup: ActionSetup | None = None) -> datetime:
    """The current instant by the clock the package enforces deadlines with."""
    return (setup or held.held()).clock.now()


def public_url(*, setup: ActionSetup | None = None) -> str:
    """Where the platform is served, `UNICON_PUBLIC_URL`."""
    return str((setup or held.held()).settings.public_url)


def _joined(base: HttpUrl, path: str) -> str:
    return str(base).rstrip("/") + path
