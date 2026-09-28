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
from forge.domain.errors import NotReady
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge
from forge.forges.forgejo import ForgejoConfig, ForgejoForge
from forge.log import get_logger
from forge.port import Forge
from forge.services import sessions
from forge.services.background import Loops, TimedPass
from forge.services.org_accounts import OrgAccountTokens
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
        joined to the public URL.
        """
        if not callback_path.startswith("/"):
            raise ValueError(f"callback_path is not a path: {callback_path}")
        engine = new_engine(str(settings.database_url))
        inner = forge or _forge_for(settings, _joined(settings.public_url, callback_path))
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
    if actions.holding():
        raise RuntimeError("forge.api.start was already called")
    setup = Setup.build(load_settings(), callback_path=callback_path)
    actions.hold(setup)
    setup.start_background()


async def ready() -> None:
    """Raise `NotReady` if the database does not answer within two seconds."""
    await actions.held().ready()


async def stop() -> None:
    """Stop the background loops and close every connection."""
    await actions.release().stop()


def now(*, setup: ActionSetup | None = None) -> datetime:
    """The current instant by the clock the package enforces deadlines with."""
    return (setup or actions.held()).clock.now()


def public_url(*, setup: ActionSetup | None = None) -> str:
    """Where the platform is served, `UNICON_PUBLIC_URL`."""
    return str((setup or actions.held()).settings.public_url)


def migrate(database_url: str) -> None:
    """Bring a database up to the package's latest migration."""
    upgrade_to_head(database_url)


def _joined(base: HttpUrl, path: str) -> str:
    return str(base).rstrip("/") + path


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
