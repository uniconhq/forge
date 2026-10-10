"""The git host's areas over Forgejo, assembled from one area object per
port area over three shared collaborators: `Users`, `Repos` and `Teams`, and
what a CI needs from Forgejo (`ci_host`). May import `forge.port`,
`forge.domain` and the shared `adapters` modules, never `adapters.ci`,
`forge.services` or `forge.db`.
"""

from dataclasses import dataclass

from forge.adapters.git.forgejo.ci_host import ForgejoCiHost
from forge.adapters.git.forgejo.content import ForgejoContent
from forge.adapters.git.forgejo.http import ForgejoAuth, Http, new_client
from forge.adapters.git.forgejo.identity import ForgejoIdentity
from forge.adapters.git.forgejo.oauth import OAuth
from forge.adapters.git.forgejo.orgs import ForgejoOrgs
from forge.adapters.git.forgejo.primitives import ForgejoPrimitives
from forge.adapters.git.forgejo.repos import Repos
from forge.adapters.git.forgejo.teams import Teams
from forge.adapters.git.forgejo.threads import ForgejoThreads
from forge.adapters.git.forgejo.uploads import ForgejoUploads
from forge.adapters.git.forgejo.users import Users
from forge.adapters.git.forgejo.workflows import ForgejoWorkflows
from forge.adapters.git.forgejo.workspaces import ForgejoWorkspaces


@dataclass(frozen=True, slots=True)
class ForgejoConfig:
    """`platform_account` is the account `admin_token` belongs to, the one
    account protected versions are reserved for.
    """

    public_url: str
    internal_url: str
    admin_token: str
    platform_account: str
    oauth_client_id: str
    oauth_client_secret: str
    sign_in_redirect_uri: str
    sign_ups_open: bool


class ForgejoForge:
    def __init__(self, config: ForgejoConfig, *, client: Http | None = None) -> None:
        """Assemble the areas over Forgejo. `client` replaces the HTTP
        client, which is how a test puts a recording transport under the
        whole adapter.
        """
        http = client or Http(new_client(config.internal_url), ForgejoAuth(config.admin_token))
        self._client = http

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
        self.workspaces = ForgejoWorkspaces(repos, users, teams)
        self.threads = ForgejoThreads(http)
        self.uploads = ForgejoUploads(http)
        self.workflows = ForgejoWorkflows(repos, users)
        self.primitives = ForgejoPrimitives(repos)
        # What a CI needs from Forgejo, handed to the CI it is paired with.
        self.ci_host = ForgejoCiHost(
            http, users, repos, public_url=config.public_url, internal_url=config.internal_url
        )

    @property
    def name(self) -> str:
        return "forgejo"

    async def aclose(self) -> None:
        await self._client.aclose()
