"""The CI's areas over Woodpecker: grading runs and the machines' enrolment.
The git host is reached only through the `CiHost` it is paired with, and
Woodpecker itself through its API, as its administrator or as an org
account. May import `forge.port`, `forge.domain` and `adapters.ci.host`,
never `adapters.git`, `forge.services` or `forge.db`.
"""

from dataclasses import dataclass
from datetime import timedelta

import httpx

from forge.adapters.ci.host import CiHost
from forge.adapters.ci.woodpecker.ci_login import CiLogin
from forge.adapters.ci.woodpecker.computes import WoodpeckerComputes
from forge.adapters.ci.woodpecker.grading import WoodpeckerGrading
from forge.adapters.ci.woodpecker.http import Http, WoodpeckerAuth, new_client


@dataclass(frozen=True, slots=True)
class WoodpeckerConfig:
    """`url` is where the platform reaches the CI and `public_url` the URL
    the CI knows itself by, `WOODPECKER_HOST`: the CI writes its webhooks
    under it, and that is how the adapter tells the CI's webhook from any
    other. `admin_token` is its administrator's. `login_lifetime` is how
    long the org account's login at the git host lasts, which is how long
    its sign-in at the CI does: the session's hard lifetime.
    """

    url: str
    public_url: str
    admin_token: str
    login_lifetime: timedelta = timedelta(days=30)


class WoodpeckerCi:
    def __init__(
        self,
        config: WoodpeckerConfig,
        host: CiHost,
        *,
        client: Http | None = None,
        browser_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`client` replaces the HTTP client, which is how a test puts a
        recording transport under the adapter; `browser_transport` does the
        same for the browser the sign-in walks with.
        """
        self._client = client or Http(new_client(config.url), WoodpeckerAuth(config.admin_token))
        self.grading = WoodpeckerGrading(
            self._client,
            host,
            ci_public_url=config.public_url,
            login_lifetime=config.login_lifetime,
            login=CiLogin(
                host,
                ci_public_url=config.public_url,
                ci_url=config.url,
                transport=browser_transport,
            ),
        )
        self.computes = WoodpeckerComputes(self._client)

    async def aclose(self) -> None:
        await self._client.aclose()
