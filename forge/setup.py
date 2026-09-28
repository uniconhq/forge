"""The package set up for one process: the settings, the forge behind the
port picked by `UNICON_FORGE`, the database, the clock, and the background
loops. `start` builds the one setup the process holds and every action opens
its unit of work on it; `ready` asks its database; `stop` tears it down.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge import actions
from forge.actions import ActionSetup
from forge.context import Clock, Context, SystemClock
from forge.db.engine import (
    TransactionFactory,
    new_engine,
    new_probe_engine,
    new_transaction_factory,
    ping,
)
from forge.db.migrations import upgrade_to_head
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge
from forge.forges.forgejo import ForgejoConfig, ForgejoForge
from forge.port import Forge
from forge.services import sessions
from forge.services.background import Loops, TimedPass
from forge.services.org_accounts import OrgAccountTokens
from forge.settings import Settings, load_settings

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
        sign_in_redirect_uri: str,
        forge: Forge | None = None,
        clock: Clock | None = None,
    ) -> Setup:
        """Assemble the package. `sign_in_redirect_uri` is where the host sends
        a browser back to after sign-in, which the hosting process owns.
        """
        engine = new_engine(str(settings.database_url))
        inner = forge or _forge_for(settings, sign_in_redirect_uri)
        setup = cls(
            settings=settings,
            forge=CachedForge(inner, enabled=settings.forge_cache),
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
        async with self._transactions() as db:
            try:
                yield Context(
                    db=db,
                    transactions=self._transactions,
                    forge=self._forge,
                    settings=self._settings,
                    clock=self._clock,
                )
            except BaseException:
                await db.rollback()
                raise
            await db.commit()

    def start_background(self) -> None:
        self._loops.start(self._transactions)

    async def ready(self) -> None:
        await ping(self._probe_engine, READY_TIMEOUT_SECONDS)

    async def stop(self) -> None:
        await self._loops.stop()
        await self._forge.aclose()
        await self._probe_engine.dispose()
        await self._engine.dispose()

    async def _sweep_sessions(self, db: AsyncSession) -> None:
        await sessions.sweep(db, self._settings, self._clock.now())


def start(*, sign_in_redirect_uri: str) -> None:
    """Build the setup the process holds from the `UNICON_*` settings and
    start its background loops. `sign_in_redirect_uri` is the hosting
    process's sign-in callback, the one thing the package cannot know on its
    own. Call it once, from inside the running event loop.
    """
    if actions.holding():
        raise RuntimeError("forge.start was already called")
    setup = Setup.build(load_settings(), sign_in_redirect_uri=sign_in_redirect_uri)
    actions.hold(setup)
    setup.start_background()


async def ready() -> None:
    """Raise if the database does not answer within two seconds."""
    await actions.held().ready()


async def stop() -> None:
    """Stop the background loops and close every connection."""
    await actions.release().stop()


def now(*, setup: ActionSetup | None = None) -> datetime:
    """The current instant by the clock the package enforces deadlines with."""
    return (setup or actions.held()).clock.now()


def migrate(database_url: str) -> None:
    """Bring a database up to the package's latest migration."""
    upgrade_to_head(database_url)


def _forge_for(settings: Settings, sign_in_redirect_uri: str) -> Forge:
    if settings.forge == "fake":
        return FakeForge(
            public_url=str(settings.forge_public_url), sign_in_redirect_uri=sign_in_redirect_uri
        )
    assert settings.forge_public_url and settings.forge_internal_url
    assert settings.forge_admin_token and settings.forge_oauth_client_secret
    assert settings.forge_oauth_client_id and settings.woodpecker_url and settings.woodpecker_token
    assert settings.woodpecker_public_url
    return ForgejoForge(
        ForgejoConfig(
            public_url=str(settings.forge_public_url),
            internal_url=str(settings.forge_internal_url),
            admin_token=settings.forge_admin_token.get_secret_value(),
            oauth_client_id=settings.forge_oauth_client_id,
            oauth_client_secret=settings.forge_oauth_client_secret.get_secret_value(),
            sign_in_redirect_uri=sign_in_redirect_uri,
            sign_ups_open=settings.forge_registration_open,
            ci_url=str(settings.woodpecker_url),
            ci_public_url=str(settings.woodpecker_public_url),
            ci_admin_token=settings.woodpecker_token.get_secret_value(),
        ),
        OrgAccountTokens(),
    )
