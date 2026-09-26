"""The package assembled for one process: the settings, the forge behind the
port picked by `UNICON_FORGE`, the database, and the background loops. The
backend builds one of these at start and calls the services with it.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge.db.engine import (
    SessionFactory,
    new_engine,
    new_probe_engine,
    new_session_factory,
    ping,
)
from forge.db.migrations import upgrade_to_head
from forge.domain.errors import NotFound
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge
from forge.forges.forgejo import SIGN_IN_CALLBACK_PATH, ForgejoConfig, ForgejoForge
from forge.port import Forge
from forge.services import sessions
from forge.services.background import Loops, TimedPass
from forge.settings import Settings

READY_TIMEOUT_SECONDS = 2.0
SESSION_SWEEP_INTERVAL = timedelta(hours=1)


class OrgAccountTokens:
    """The credentials of the org accounts. None exist until orgs are
    provisioned, so every lookup is `NotFound`.
    """

    async def forge_token(self, org: str) -> str:
        raise NotFound(f"org {org} has no org account yet")

    async def ci_token(self, org: str) -> str:
        raise NotFound(f"org {org} has no org account yet")


@dataclass
class Runtime:
    settings: Settings
    forge: Forge
    engine: AsyncEngine
    probe_engine: AsyncEngine
    sessions: SessionFactory
    loops: Loops = field(default_factory=Loops)

    @classmethod
    def build(cls, settings: Settings, *, forge: Forge | None = None) -> Runtime:
        engine = new_engine(str(settings.database_url))
        runtime = cls(
            settings=settings,
            forge=CachedForge(forge or _forge_for(settings), enabled=settings.forge_cache),
            engine=engine,
            probe_engine=new_probe_engine(str(settings.database_url)),
            sessions=new_session_factory(engine),
        )
        runtime.loops.add(
            TimedPass("sessions.sweep", runtime._sweep_sessions, SESSION_SWEEP_INTERVAL)
        )
        return runtime

    def start_background(self) -> None:
        self.loops.start(self.sessions)

    async def ready(self) -> None:
        await ping(self.probe_engine, READY_TIMEOUT_SECONDS)

    async def stop(self) -> None:
        await self.loops.stop()
        closer = getattr(self.forge, "aclose", None)
        if closer is not None:
            await closer()
        await self.probe_engine.dispose()
        await self.engine.dispose()

    async def _sweep_sessions(self, db: AsyncSession) -> None:
        await sessions.sweep(db, self.settings)


def migrate(database_url: str) -> None:
    """Bring a database up to the package's latest migration."""
    upgrade_to_head(database_url)


def _forge_for(settings: Settings) -> Forge:
    if settings.forge == "fake":
        return FakeForge(
            public_url=str(settings.forge_public_url),
            sign_in_redirect_uri=str(settings.public_url).rstrip("/") + SIGN_IN_CALLBACK_PATH,
        )
    return ForgejoForge(
        ForgejoConfig(
            public_url=str(settings.forge_public_url),
            internal_url=str(settings.forge_internal_url),
            admin_token=settings.forge_admin_token.get_secret_value(),
            oauth_client_id=settings.forge_oauth_client_id,
            oauth_client_secret=settings.forge_oauth_client_secret.get_secret_value(),
            sign_in_redirect_uri=str(settings.public_url).rstrip("/") + SIGN_IN_CALLBACK_PATH,
            ci_url=str(settings.woodpecker_url),
            ci_public_url=str(settings.woodpecker_url),
            ci_admin_token=settings.woodpecker_token.get_secret_value(),
        ),
        OrgAccountTokens(),
    )
