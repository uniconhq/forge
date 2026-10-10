"""Signing an org account in at Woodpecker with nobody at a browser, and
minting its CI token. Woodpecker mints a token only through its web UI, and
each of the three steps is forced: Woodpecker knows only people who signed in
through the git host, so the sign-in is a real OAuth round trip as the
account, through the git host's own pages (`WebSignIn`); a Woodpecker web
session cannot make API writes without the CSRF token it hands its own page
in `/web-config.js`; and the endpoint that mints a token is such a write.

The round trip runs in a fresh `Browser` over both services' addresses, so
each redirect, written under a public address, reaches the service at its
internal one.
"""

from urllib.parse import urljoin, urlsplit

import httpx

from forge.adapters.browser import REDIRECTS, Browser, open_browser
from forge.adapters.ci.host import WebSignIn
from forge.domain.errors import Rejected, Unavailable
from forge.log import get_logger

log = get_logger(__name__)

MAX_REDIRECTS = 12
OK = 200
SESSION_COOKIE = "user_sess"
CSRF_NAME = "WOODPECKER_CSRF"


class CiLogin:
    def __init__(
        self,
        web: WebSignIn,
        *,
        ci_public_url: str,
        ci_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._web = web
        self._ci_public = ci_public_url.rstrip("/")
        self._addresses = (web.web_address, (self._ci_public, ci_url.rstrip("/")))
        self._transport = transport

    async def mint_token(self, username: str, password: str) -> str:
        """Sign `username` in at the git host with `password`, walk the CI's
        OAuth round trip approving consent, and mint a CI token. `Forbidden`
        when the git host refuses the password; `Rejected` when a page is not
        what the round trip expects; `Unavailable` when either service does
        not answer.
        """
        try:
            async with open_browser(self._addresses, transport=self._transport) as browser:
                await self._web.sign_in(browser, username, password)
                await self._sign_into_ci(browser)
                token = await self._mint(browser)
                await self._check(browser, token)
        except httpx.HTTPError as exc:
            raise Unavailable(f"no answer during the CI sign-in: {type(exc).__name__}") from exc
        log.info("ci_login.completed", username=username)
        return token

    async def _sign_into_ci(self, browser: Browser) -> None:
        """Walk the OAuth round trip by hand, approving the CI on the way,
        until the CI has set its session cookie.
        """
        location = f"{self._ci_public}/authorize"
        for _ in range(MAX_REDIRECTS):
            response = await browser.request("GET", location)
            if browser.has_cookie(SESSION_COOKIE):
                return
            if response.status_code in REDIRECTS:
                location = urljoin(location, response.headers["location"])
                continue
            if response.status_code != OK:
                # The query can hold the OAuth code, and the detail is logged.
                where = urlsplit(location)._replace(query="", fragment="").geturl()
                raise Rejected(f"{where} answered {response.status_code} during the CI sign-in")
            location = await self._web.approve_consent(browser, location, response)
        raise Rejected("the CI sign-in did not settle")

    async def _mint(self, browser: Browser) -> str:
        configuration = await browser.request("GET", f"{self._ci_public}/web-config.js")
        csrf = javascript_string(configuration.text, CSRF_NAME)
        if not csrf:
            raise Rejected(f"no {CSRF_NAME} in the CI's web configuration")
        response = await browser.request(
            "POST", f"{self._ci_public}/api/user/token", headers={"X-CSRF-TOKEN": csrf}
        )
        token = response.text.strip()
        if response.status_code != OK or not token or token.startswith("<"):
            raise Rejected(f"the CI answered {response.status_code} to the token request")
        return token

    async def _check(self, browser: Browser, token: str) -> None:
        response = await browser.request(
            "GET", f"{self._ci_public}/api/user", headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code != OK:
            raise Rejected("the minted CI token does not authorise a call")


def javascript_string(source: str, name: str) -> str | None:
    """Read `name = "value"` out of the served configuration script."""
    marker = f"{name} ="
    start = source.find(marker)
    if start < 0:
        return None
    opening = source.find('"', start)
    closing = source.find('"', opening + 1)
    if opening < 0 or closing < 0:
        return None
    return source[opening + 1 : closing]
