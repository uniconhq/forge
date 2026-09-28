"""The port over Forgejo and Woodpecker, assembled from one area object per
port area over three shared collaborators: `Users`, `Repos` and `Teams`. May
import `forge.port` and `forge.domain`, never `forge.services` or `forge.db`.
"""

from dataclasses import dataclass

from forge.forges.forgejo.computes import WoodpeckerComputes
from forge.forges.forgejo.content import ForgejoContent
from forge.forges.forgejo.grading import WoodpeckerGrading
from forge.forges.forgejo.http import ForgejoAuth, Http, WoodpeckerAuth, new_client
from forge.forges.forgejo.identity import ForgejoIdentity
from forge.forges.forgejo.oauth import OAuth
from forge.forges.forgejo.orgs import ForgejoOrgs
from forge.forges.forgejo.primitives import ForgejoPrimitives
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.forgejo.threads import ForgejoThreads
from forge.forges.forgejo.users import Users
from forge.forges.forgejo.workflows import ForgejoWorkflows
from forge.forges.forgejo.workspaces import ForgejoWorkspaces


@dataclass(frozen=True, slots=True)
class ForgejoConfig:
    """`platform_account` is the account `admin_token` belongs to, the one
    account protected versions are reserved for. `ci_public_url` is the URL
    the CI knows itself by, `WOODPECKER_HOST`: the CI writes its webhooks
    under it, and that is how the implementation tells the CI's webhook from
    any other.
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


class ForgejoForge:
    def __init__(
        self,
        config: ForgejoConfig,
        *,
        clients: tuple[Http, Http] | None = None,
    ) -> None:
        """Assemble the areas over the host and the CI. `clients` replaces the
        two HTTP clients, which is how a test puts a recording transport under
        the whole implementation.
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
        self.orgs = ForgejoOrgs(http, teams, users)
        self.content = ForgejoContent(repos, teams)
        self.grading = WoodpeckerGrading(http, ci, repos, ci_public_url=config.ci_public_url)
        self.workspaces = ForgejoWorkspaces(repos, users, teams)
        self.threads = ForgejoThreads(http)
        self.workflows = ForgejoWorkflows(repos, users)
        self.primitives = ForgejoPrimitives(repos)
        self.computes = WoodpeckerComputes(ci)

    @property
    def name(self) -> str:
        return "forgejo"

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
