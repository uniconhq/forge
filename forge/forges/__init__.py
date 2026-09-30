"""The implementations of the port: `forgejo` for the real host and `fake` for
tests, and `cached` in front of either. Nothing in `domain`, `services` or
`db` imports this package; `build` is how the runtime gets a forge.
"""

from forge.domain.errors import Misconfigured
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge
from forge.forges.forgejo import ForgejoConfig, ForgejoForge
from forge.forges.forgejo.objects import StorageConfig
from forge.port import Forge
from forge.settings import Settings


def build(settings: Settings, *, sign_in_redirect_uri: str) -> Forge:
    """The forge `UNICON_FORGE` picks, configured from the settings, behind
    the cache `UNICON_FORGE_CACHE` turns on. `sign_in_redirect_uri` is where
    the host sends a browser back to after sign-in.
    """
    return CachedForge(
        _implementation(settings, sign_in_redirect_uri), enabled=settings.forge_cache
    )


def _implementation(settings: Settings, sign_in_redirect_uri: str) -> Forge:
    if settings.forge == "fake":
        if settings.forge_public_url is None:
            return FakeForge(sign_in_redirect_uri=sign_in_redirect_uri)
        return FakeForge(
            public_url=str(settings.forge_public_url), sign_in_redirect_uri=sign_in_redirect_uri
        )
    forgejo, s3 = settings.forgejo, settings.s3
    if forgejo is None or s3 is None or settings.forge_public_url is None:
        raise Misconfigured("UNICON_FORGE=forgejo without its settings")
    return ForgejoForge(
        ForgejoConfig(
            public_url=str(settings.forge_public_url),
            internal_url=str(forgejo.internal_url),
            admin_token=forgejo.admin_token.get_secret_value(),
            platform_account=forgejo.platform_account,
            oauth_client_id=forgejo.oauth_client_id,
            oauth_client_secret=forgejo.oauth_client_secret.get_secret_value(),
            sign_in_redirect_uri=sign_in_redirect_uri,
            sign_ups_open=forgejo.registration_open,
            ci_url=str(forgejo.woodpecker_url),
            ci_public_url=str(forgejo.woodpecker_public_url),
            ci_admin_token=forgejo.woodpecker_token.get_secret_value(),
            storage=StorageConfig(
                endpoint=str(s3.endpoint),
                region=s3.region,
                access_key=s3.access_key,
                secret_key=s3.secret_key.get_secret_value(),
                uploads_bucket=s3.uploads_bucket,
                results_bucket=s3.results_bucket,
                public_url=str(settings.public_url),
                machine_url=str(settings.machine_url),
            ),
        )
    )
