"""The package set up for one process: the settings, the forge behind the
port picked by `UNICON_FORGE`, the database, the clock, and the background
loops. `start` builds the one setup the process holds and every action opens
its unit of work on it; `ready` asks its database; `stop` tears it down;
`public_url` is where the platform is served.

The loops are the session sweeper, the `provisioning` poller and the nightly
drift pass. `MAKERS` is what the poller hands each kind of row to: the org,
the contest, the task and the registration for grading, each made by the
service of that name.
"""

import asyncio
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine

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
from forge.runtime.background import Loops, Poller, TimedPass
from forge.runtime.context import ActionSetup, Context, transaction
from forge.runtime.held import held, hold, holding, release, setup_or_held
from forge.services import (
    contests,
    drift,
    orgs,
    provisioning,
    registrations,
    sessions,
    tasks,
)
from forge.settings import Settings, load_settings

log = get_logger(__name__)

READY_TIMEOUT_SECONDS = 2.0
SESSION_SWEEP_INTERVAL = timedelta(hours=1)
DRIFT_INTERVAL = timedelta(hours=24)

MAKERS: dict[str, provisioning.RowWork] = {
    orgs.KIND: orgs.provision,
    contests.KIND: contests.provision,
    tasks.KIND: tasks.provision,
    registrations.KIND: registrations.provision,
}


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
        setup._loops.add(
            TimedPass("sessions.sweep", sessions.sweep, SESSION_SWEEP_INTERVAL),
            provisioning.poller(MAKERS),
            TimedPass("drift.nightly", drift.nightly, DRIFT_INTERVAL),
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
                _refresh_lock=self.refresh_lock,
            )

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

    def start_background(self) -> None:
        self._loops.start(self.unit_of_work)

    async def tick(self, name: str) -> None:
        """Run one tick of the poller or timed pass named `name` now, as the
        loop would. `ValueError` naming the loops there are for any other
        name.
        """
        loops: dict[str, Poller | TimedPass] = {
            poller.name: poller for poller in self._loops.pollers
        }
        loops.update({timed.name: timed for timed in self._loops.passes})
        loop = loops.get(name)
        if loop is None:
            raise ValueError(
                f"there is no poller or timed pass named {name!r}; there are {sorted(loops)}"
            )
        await loop.tick(self.unit_of_work)

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


def start(*, callback_path: str, background: bool = True) -> None:
    """Build the setup the process holds from the `UNICON_*` settings and
    start its background loops. `callback_path` is the hosting process's
    sign-in callback route, the one thing the package cannot know on its own;
    it is joined to `UNICON_PUBLIC_URL`. With `background` off no poller or
    timed pass runs, for a one-off command that must not tick one as a side
    effect. Call it once, from inside the running event loop.
    """
    if holding():
        raise RuntimeError("forge.api.start was already called")
    setup = Setup.build(load_settings(), callback_path=callback_path)
    hold(setup)
    if background:
        setup.start_background()


async def ready() -> None:
    """Raise `NotReady` if the database does not answer within two seconds."""
    await held().ready()


async def stop() -> None:
    """Stop the background loops and close every connection."""
    await release().stop()


def now(*, setup: ActionSetup | None = None) -> datetime:
    """The current instant by the clock the package enforces deadlines with."""
    return setup_or_held(setup).clock.now()


def public_url(*, setup: ActionSetup | None = None) -> str:
    """Where the platform is served, `UNICON_PUBLIC_URL`."""
    return str(setup_or_held(setup).settings.public_url)


def _joined(base: HttpUrl, path: str) -> str:
    return str(base).rstrip("/") + path
