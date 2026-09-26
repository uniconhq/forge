"""The package assembled for one process: the settings, the forge behind the
port picked by `UNICON_FORGE`, the database, the clock, and the background
loops. The process that hosts the package builds one of these at start,
makes a `Context` per unit of work, and calls the services with it.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge.context import Clock, Context, SystemClock
from forge.db.engine import (
    SessionFactory,
    new_engine,
    new_probe_engine,
    new_session_factory,
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
from forge.settings import Settings

READY_TIMEOUT_SECONDS = 2.0
SESSION_SWEEP_INTERVAL = timedelta(hours=1)


@dataclass
class Runtime:
    settings: Settings
    forge: Forge
    engine: AsyncEngine
    probe_engine: AsyncEngine
    sessions: SessionFactory
    clock: Clock
    loops: Loops = field(default_factory=Loops)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        sign_in_redirect_uri: str,
        forge: Forge | None = None,
        clock: Clock | None = None,
    ) -> Runtime:
        """Assemble the package. `sign_in_redirect_uri` is where the host sends
        a browser back to after sign-in, which the hosting process owns.
        """
        engine = new_engine(str(settings.database_url))
        inner = forge or _forge_for(settings, sign_in_redirect_uri)
        runtime = cls(
            settings=settings,
            forge=CachedForge(inner, enabled=settings.forge_cache),
            engine=engine,
            probe_engine=new_probe_engine(str(settings.database_url)),
            sessions=new_session_factory(engine),
            clock=clock or SystemClock(),
        )
        runtime.loops.add(
            TimedPass("sessions.sweep", runtime._sweep_sessions, SESSION_SWEEP_INTERVAL)
        )
        return runtime

    def context(self, db: AsyncSession) -> Context:
        return Context(
            db=db,
            sessions=self.sessions,
            forge=self.forge,
            settings=self.settings,
            clock=self.clock,
        )

    def start_background(self) -> None:
        self.loops.start(self.sessions)

    async def ready(self) -> None:
        await ping(self.probe_engine, READY_TIMEOUT_SECONDS)

    async def stop(self) -> None:
        await self.loops.stop()
        await self.forge.aclose()
        await self.probe_engine.dispose()
        await self.engine.dispose()

    async def _sweep_sessions(self, db: AsyncSession) -> None:
        await sessions.sweep(db, self.settings, self.clock.now())


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
            ci_public_url=str(settings.woodpecker_url),
            ci_admin_token=settings.woodpecker_token.get_secret_value(),
        ),
        OrgAccountTokens(),
    )
