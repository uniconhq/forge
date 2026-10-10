"""Signing an org account in at Woodpecker with nobody at a browser, and
minting its CI token. Woodpecker mints a token only through its web UI, and
each of the three steps is forced: Woodpecker knows only people who signed in
through the forge, so the sign-in is a real OAuth round trip as the account;
a Woodpecker web session cannot make API writes without the CSRF token it
hands its own page in `/web-config.js`; and the endpoint that mints a token
is such a write.

The dance runs inside the platform's own process, where the public URLs of
the forge and the CI do not resolve, while both services send their
redirects under those public URLs. So redirects are followed by hand, and a
URL under the forge's public prefix is requested at the forge's internal
one, a URL under the CI's public prefix at the CI's internal one. Cookies are
kept by the rewritten hosts throughout, so they are sent back consistently.
A fresh client with an empty cookie jar is used for each sign-in, so one
account's session never carries over into another's.
"""

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from forge.domain.errors import Forbidden, Rejected, Unavailable
from forge.log import get_logger

log = get_logger(__name__)

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MAX_REDIRECTS = 12
REDIRECTS = (301, 302, 303, 307, 308)
OK = 200
SESSION_COOKIE = "user_sess"
CSRF_NAME = "WOODPECKER_CSRF"


class CiLogin:
    def __init__(
        self,
        *,
        forge_public_url: str,
        forge_url: str,
        ci_public_url: str,
        ci_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._forge_public = forge_public_url.rstrip("/")
        self._ci_public = ci_public_url.rstrip("/")
        self._rewrites = (
            (self._forge_public, forge_url.rstrip("/")),
            (self._ci_public, ci_url.rstrip("/")),
        )
        self._transport = transport

    async def mint_token(self, username: str, password: str) -> str:
        """Sign `username` in at the forge with `password`, walk the CI's
        OAuth round trip approving consent, and mint a CI token. `Forbidden`
        when the forge refuses the password; `Rejected` when a page is not
        what the dance expects; `Unavailable` when either service does not
        answer.
        """
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=TIMEOUT, transport=self._transport
            ) as browser:
                await self._sign_into_forge(browser, username, password)
                await self._sign_into_ci(browser)
                token = await self._mint(browser)
                await self._check(browser, token)
        except httpx.HTTPError as exc:
            raise Unavailable(f"no answer during the CI sign-in: {type(exc).__name__}") from exc
        log.info("ci_login.completed", username=username)
        return token

    async def _sign_into_forge(
        self, browser: httpx.AsyncClient, username: str, password: str
    ) -> None:
        """The way the browser form does it. Forgejo 15 puts no CSRF field on
        this form; whatever hidden field is there is sent back anyway.
        """
        page = await self._request(browser, "GET", f"{self._forge_public}/user/login")
        fields = hidden_inputs(page.text)
        fields.update({"user_name": username, "password": password})
        response = await self._request(
            browser,
            "POST",
            f"{self._forge_public}/user/login",
            data=fields,
            headers={"Referer": self._rewrite(f"{self._forge_public}/user/login")},
        )
        if response.status_code not in REDIRECTS:
            raise Forbidden(f"the forge did not accept the sign-in as {username}")

    async def _sign_into_ci(self, browser: httpx.AsyncClient) -> None:
        """Walk the OAuth round trip by hand, approving the CI on the way,
        until the CI has set its session cookie.
        """
        location = f"{self._ci_public}/authorize"
        for _ in range(MAX_REDIRECTS):
            response = await self._request(browser, "GET", location)
            if _has_session(browser):
                return
            if response.status_code in REDIRECTS:
                location = urljoin(location, response.headers["location"])
                continue
            if response.status_code != OK:
                # The query can hold the OAuth code, and the detail is logged.
                where = urlsplit(location)._replace(query="", fragment="").geturl()
                raise Rejected(f"{where} answered {response.status_code} during the CI sign-in")
            location = await self._approve_consent(browser, location, response)
        raise Rejected("the CI sign-in did not settle")

    async def _approve_consent(
        self, browser: httpx.AsyncClient, location: str, page: httpx.Response
    ) -> str:
        """Post the forge's consent form and return where it sends the browser
        next. The approve control is a button, not an input, so `granted` is
        added by hand; without it the forge reads the post as a refusal.
        """
        fields = hidden_inputs(page.text)
        if "client_id" not in fields:
            raise Rejected(f"{location} is not the forge's consent page")
        fields["granted"] = "true"
        response = await self._request(
            browser,
            "POST",
            f"{self._forge_public}/login/oauth/grant",
            data=fields,
            headers={"Referer": self._rewrite(location)},
        )
        if response.status_code not in REDIRECTS:
            raise Rejected(f"consent answered {response.status_code} without a redirect")
        return urljoin(location, response.headers["location"])

    async def _mint(self, browser: httpx.AsyncClient) -> str:
        configuration = await self._request(browser, "GET", f"{self._ci_public}/web-config.js")
        csrf = javascript_string(configuration.text, CSRF_NAME)
        if not csrf:
            raise Rejected(f"no {CSRF_NAME} in the CI's web configuration")
        response = await self._request(
            browser, "POST", f"{self._ci_public}/api/user/token", headers={"X-CSRF-TOKEN": csrf}
        )
        token = response.text.strip()
        if response.status_code != OK or not token or token.startswith("<"):
            raise Rejected(f"the CI answered {response.status_code} to the token request")
        return token

    async def _check(self, browser: httpx.AsyncClient, token: str) -> None:
        response = await self._request(
            browser,
            "GET",
            f"{self._ci_public}/api/user",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != OK:
            raise Rejected("the minted CI token does not authorise a call")

    async def _request(
        self, browser: httpx.AsyncClient, method: str, url: str, **options: Any
    ) -> httpx.Response:
        return await browser.request(method, self._rewrite(url), **options)

    def _rewrite(self, url: str) -> str:
        for public, internal in self._rewrites:
            if url == public or url.startswith((f"{public}/", f"{public}?")):
                return internal + url[len(public) :]
        return url


def _has_session(browser: httpx.AsyncClient) -> bool:
    return any(cookie.name == SESSION_COOKIE for cookie in browser.cookies.jar)


class _HiddenInputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        if attributes.get("type") != "hidden":
            return
        name = attributes.get("name")
        if name:
            self.fields[name] = attributes.get("value") or ""


def hidden_inputs(html: str) -> dict[str, str]:
    """The hidden fields of the forms on a page, by name."""
    collector = _HiddenInputs()
    collector.feed(html)
    return collector.fields


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
