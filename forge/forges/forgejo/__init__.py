"""The port over Forgejo and Woodpecker. May import `forge.port` and
`forge.domain`, never `forge.services` or `forge.db`.
"""

from dataclasses import dataclass

from forge.forges.forgejo.ci import Woodpecker
from forge.forges.forgejo.ci import new_client as new_ci_client
from forge.forges.forgejo.http import ForgejoHttp, TokenSource, new_client
from forge.forges.forgejo.oauth import OAuth
from forge.forges.forgejo.threads import ThreadOps
from forge.forges.forgejo.workflows import WorkflowOps
from forge.forges.forgejo.workspaces import WorkspaceOps

SIGN_IN_CALLBACK_PATH = "/api/v1/auth/callback"


@dataclass(frozen=True, slots=True)
class ForgejoConfig:
    public_url: str
    internal_url: str
    admin_token: str
    oauth_client_id: str
    oauth_client_secret: str
    sign_in_redirect_uri: str
    ci_url: str
    ci_public_url: str
    ci_admin_token: str


class ForgejoForge(WorkspaceOps, ThreadOps, WorkflowOps):
    """The real implementation. Every operation the port declares is one of
    the mixins this class is assembled from.
    """

    def __init__(self, config: ForgejoConfig, tokens: TokenSource) -> None:
        super().__init__(
            ForgejoHttp(
                new_client(config.internal_url), admin_token=config.admin_token, tokens=tokens
            )
        )
        self._oauth = OAuth(
            new_client(config.internal_url),
            public_url=config.public_url,
            client_id=config.oauth_client_id,
            client_secret=config.oauth_client_secret,
            redirect_uri=config.sign_in_redirect_uri,
        )
        self._ci = Woodpecker(
            new_ci_client(config.ci_url), admin_token=config.ci_admin_token, tokens=tokens
        )
        self._ci_public_url = config.ci_public_url.rstrip("/")

    @property
    def name(self) -> str:
        return "forgejo"

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._ci.aclose()
