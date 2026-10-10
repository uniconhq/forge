"""The adapters behind the port, one group per service outside the platform:
`git` for the git host, `ci` for the CI, `objects` for the store run logs
are kept in and `mail` for the mail server, with `fake` in each for tests,
and `cached` in front of the git host's reads. Nothing in `domain`,
`services` or `db` imports this package; `build` is how the runtime gets its
`Forge`, one of each joined into one, and the only place that joins them.
"""

from forge.adapters.cached import CachedForge
from forge.adapters.ci.woodpecker import WoodpeckerCi, WoodpeckerConfig
from forge.adapters.fakes import FakeForge
from forge.adapters.git.forgejo import ForgejoConfig, ForgejoForge
from forge.adapters.mail.smtp import MailConfig, NoMail, SmtpMail
from forge.adapters.objects.s3 import S3Objects, StorageConfig
from forge.domain.errors import Misconfigured
from forge.port import Ci, Forge, GitHost, MailPort, ObjectStore
from forge.settings import Settings


class JoinedForge:
    """One `Forge` from its parts: the git host's areas from `git`, the CI's
    from `ci`, and the object store and the mail server as they are. Its
    name is the git host's, as the logs have always read.
    """

    def __init__(self, *, git: GitHost, ci: Ci, objects: ObjectStore, mail: MailPort) -> None:
        self.identity = git.identity
        self.orgs = git.orgs
        self.content = git.content
        self.workspaces = git.workspaces
        self.threads = git.threads
        self.workflows = git.workflows
        self.primitives = git.primitives
        self.uploads = git.uploads
        self.grading = ci.grading
        self.computes = ci.computes
        self.objects = objects
        self.mail = mail
        self._git = git
        self._ci = ci

    @property
    def git(self) -> GitHost:
        """The git host's adapter this was joined from."""
        return self._git

    @property
    def ci(self) -> Ci:
        """The CI's adapter this was joined from."""
        return self._ci

    @property
    def name(self) -> str:
        return self._git.name

    async def aclose(self) -> None:
        await self._git.aclose()
        await self._ci.aclose()


def build(settings: Settings, *, sign_in_redirect_uri: str) -> Forge:
    """The forge `UNICON_FORGE` picks, grading with the CI `UNICON_CI`
    picks, configured from the settings, behind the cache
    `UNICON_FORGE_CACHE` turns on. `sign_in_redirect_uri` is where the host
    sends a browser back to after sign-in.
    """
    return CachedForge(
        _implementation(settings, sign_in_redirect_uri), enabled=settings.forge_cache
    )


def _implementation(settings: Settings, sign_in_redirect_uri: str) -> Forge:
    if settings.forge == "fake":
        if settings.forge_public_url is None:
            return FakeForge(
                sign_in_redirect_uri=sign_in_redirect_uri,
                ci_login_lifetime=settings.session_hard_ttl,
            )
        return FakeForge(
            public_url=str(settings.forge_public_url),
            sign_in_redirect_uri=sign_in_redirect_uri,
            ci_login_lifetime=settings.session_hard_ttl,
        )
    forgejo, woodpecker, s3 = settings.forgejo, settings.woodpecker, settings.s3
    if forgejo is None or s3 is None or settings.forge_public_url is None:
        raise Misconfigured("UNICON_FORGE=forgejo without its settings")
    git = ForgejoForge(
        ForgejoConfig(
            public_url=str(settings.forge_public_url),
            internal_url=str(forgejo.internal_url),
            admin_token=forgejo.admin_token.get_secret_value(),
            platform_account=forgejo.platform_account,
            oauth_client_id=forgejo.oauth_client_id,
            oauth_client_secret=forgejo.oauth_client_secret.get_secret_value(),
            sign_in_redirect_uri=sign_in_redirect_uri,
            sign_ups_open=forgejo.registration_open,
        )
    )
    match settings.ci:
        case "woodpecker":
            if woodpecker is None:
                raise Misconfigured("UNICON_CI=woodpecker without its settings")
            ci = WoodpeckerCi(
                WoodpeckerConfig(
                    url=str(woodpecker.url),
                    public_url=str(woodpecker.public_url),
                    admin_token=woodpecker.token.get_secret_value(),
                    login_lifetime=settings.session_hard_ttl,
                ),
                git.ci_host,
            )
        case _:
            raise Misconfigured(f"UNICON_CI={settings.ci} names no CI this platform grades with")
    return JoinedForge(
        git=git,
        ci=ci,
        objects=S3Objects(
            StorageConfig(
                endpoint=str(s3.endpoint),
                region=s3.region,
                access_key=s3.access_key,
                secret_key=s3.secret_key.get_secret_value(),
                results_bucket=s3.results_bucket,
                machine_url=str(settings.machine_url),
            )
        ),
        mail=_mail(settings),
    )


def _mail(settings: Settings) -> SmtpMail | NoMail:
    mail = settings.mail
    if mail is None:
        return NoMail()
    return SmtpMail(
        MailConfig(
            host=mail.smtp_addr,
            port=mail.smtp_port,
            protocol=mail.protocol,
            sender=mail.sender,
            user=mail.user,
            password=mail.password.get_secret_value() if mail.password is not None else None,
        )
    )
