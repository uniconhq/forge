"""The port over Forgejo and Woodpecker, assembled from one area object per
port area over three shared collaborators: `Users`, `Repos` and `Teams`, and
the object store beside them over S3. May import `forge.port` and
`forge.domain`, never `forge.services` or `forge.db`.
"""

from dataclasses import dataclass
from datetime import timedelta

import httpx

from forge.domain.errors import Misconfigured
from forge.forges.forgejo.ci_login import CiLogin
from forge.forges.forgejo.computes import WoodpeckerComputes
from forge.forges.forgejo.content import ForgejoContent
from forge.forges.forgejo.grading import WoodpeckerGrading
from forge.forges.forgejo.http import ForgejoAuth, Http, WoodpeckerAuth, new_client
from forge.forges.forgejo.identity import ForgejoIdentity
from forge.forges.forgejo.mail import MailConfig, NoMail, SmtpMail
from forge.forges.forgejo.oauth import OAuth
from forge.forges.forgejo.objects import NoStore, S3Objects, StorageConfig
from forge.forges.forgejo.orgs import ForgejoOrgs
from forge.forges.forgejo.primitives import ForgejoPrimitives
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.forgejo.threads import ForgejoThreads
from forge.forges.forgejo.uploads import ForgejoUploads
from forge.forges.forgejo.users import Users
from forge.forges.forgejo.workflows import ForgejoWorkflows
from forge.forges.forgejo.workspaces import ForgejoWorkspaces


@dataclass(frozen=True, slots=True)
class ForgejoConfig:
    """`platform_account` is the account `admin_token` belongs to, the one
    account protected versions are reserved for. `ci_public_url` is the URL
    the CI knows itself by, `WOODPECKER_HOST`: the CI writes its webhooks
    under it, and that is how the implementation tells the CI's webhook from
    any other. `storage` is the object store beside the forge, which the
    Forgejo implementation reaches over S3; without it every call to the
    store is `Misconfigured`. `mail` is the server the forge sends its own
    mail through, which the platform's mail goes through too; without it
    nothing is sent. `ci` names the CI the grading and compute areas talk
    to, which the `ci_*` settings reach. `ci_login_lifetime` is how long the
    org account's
    login at the forge lasts, which is how long its sign-in at the CI does:
    the session's hard lifetime.
    """

    public_url: str
    internal_url: str
    admin_token: str
    platform_account: str
    oauth_client_id: str
    oauth_client_secret: str
    sign_in_redirect_uri: str
    sign_ups_open: bool
    ci_url: str
    ci_public_url: str
    ci_admin_token: str
    storage: StorageConfig | None = None
    mail: MailConfig | None = None
    ci_login_lifetime: timedelta = timedelta(days=30)
    ci: str = "woodpecker"


class ForgejoForge:
    def __init__(
        self,
        config: ForgejoConfig,
        *,
        clients: tuple[Http, Http] | None = None,
        browser_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Assemble the areas over the host and the CI. `clients` replaces the
        two HTTP clients, which is how a test puts a recording transport under
        the whole implementation; `browser_transport` does the same for the
        client the CI sign-in dance browses with.
        """
        http, ci = clients or (
            Http(new_client(config.internal_url), ForgejoAuth(config.admin_token)),
            Http(new_client(config.ci_url), WoodpeckerAuth(config.ci_admin_token)),
        )
        self._clients = (http, ci)

        users = Users(http)
        repos = Repos(http, platform_account=config.platform_account)
        teams = Teams(http)
        oauth = OAuth(
            http,
            public_url=config.public_url,
            client_id=config.oauth_client_id,
            client_secret=config.oauth_client_secret,
            redirect_uri=config.sign_in_redirect_uri,
        )

        self.identity = ForgejoIdentity(
            oauth, users, public_url=config.public_url, sign_ups_open=config.sign_ups_open
        )
        self.orgs = ForgejoOrgs(http, teams, users, platform_account=config.platform_account)
        self.content = ForgejoContent(repos, teams)
        if config.ci != "woodpecker":
            raise Misconfigured(f"UNICON_CI={config.ci} names no CI this forge grades with")
        self.grading = WoodpeckerGrading(
            http,
            ci,
            repos,
            users,
            ci_public_url=config.ci_public_url,
            login_lifetime=config.ci_login_lifetime,
            login=CiLogin(
                forge_public_url=config.public_url,
                forge_url=config.internal_url,
                ci_public_url=config.ci_public_url,
                ci_url=config.ci_url,
                transport=browser_transport,
            ),
        )
        self.workspaces = ForgejoWorkspaces(repos, users, teams)
        self.threads = ForgejoThreads(http)
        self.uploads = ForgejoUploads(http)
        self.workflows = ForgejoWorkflows(repos, users)
        self.primitives = ForgejoPrimitives(repos)
        self.computes = WoodpeckerComputes(ci)
        self.objects: S3Objects | NoStore = (
            S3Objects(config.storage) if config.storage is not None else NoStore()
        )
        self.mail: SmtpMail | NoMail = (
            SmtpMail(config.mail) if config.mail is not None else NoMail()
        )

    @property
    def name(self) -> str:
        return "forgejo"

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
