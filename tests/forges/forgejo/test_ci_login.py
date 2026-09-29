"""The CI sign-in dance against a scripted forge and CI: every request goes
to the internal hosts however the redirects are written, the cookies each
service sets come back to it, consent is approved, the CSRF token is read
from the CI's web configuration, and the token that comes out is checked.
"""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from forge.domain.errors import Forbidden, Rejected, Unavailable
from forge.forges.forgejo.ci_login import CiLogin, hidden_inputs, javascript_string

FORGE_PUBLIC = "http://forge.test"
FORGE_INTERNAL = "http://forge.internal"
CI_PUBLIC = "http://ci.test"
CI_INTERNAL = "http://ci.internal"
PASSWORD = "correct-horse"

LOGIN_PAGE = '<form><input type="hidden" name="_csrf" value="csrf-forge"></form>'
CONSENT_PAGE = (
    "<form>"
    '<input type="hidden" name="_csrf" value="csrf-forge">'
    '<input type="hidden" name="client_id" value="woodpecker">'
    '<input type="hidden" name="redirect_uri" value="http://ci.test/authorize">'
    '<input type="hidden" name="state" value="st-1">'
    '<button name="granted" value="true">Authorize</button>'
    "</form>"
)
WEB_CONFIG = 'window.WOODPECKER_VERSION = "3.0";\nwindow.WOODPECKER_CSRF = "csrf-ci";\n'


@dataclass
class Stack:
    """A forge and a CI that answer the dance the way the real ones do, and
    remember every request. `consent` says whether the forge shows the
    consent page or has the grant on record already.
    """

    consent: bool = True
    seen: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host, path = request.url.host, request.url.path
        cookies = _cookies(request)
        if host == "forge.internal":
            return self._forge(request, path, cookies)
        if host == "ci.internal":
            return self._ci(request, path, cookies)
        return httpx.Response(599, text=f"unexpected host {host}")

    def _forge(self, request: httpx.Request, path: str, cookies: dict[str, str]) -> httpx.Response:
        if path == "/user/login" and request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        if path == "/user/login" and request.method == "POST":
            form = parse_qs(request.content.decode())
            if form.get("_csrf") != ["csrf-forge"] or form.get("password") != [PASSWORD]:
                return httpx.Response(200, text="wrong")
            return httpx.Response(
                303, headers={"location": "/", "set-cookie": "i_like_gitea=forge-sess; Path=/"}
            )
        if path == "/login/oauth/authorize":
            if cookies.get("i_like_gitea") != "forge-sess":
                return httpx.Response(303, headers={"location": f"{FORGE_PUBLIC}/user/login"})
            if self.consent:
                return httpx.Response(200, text=CONSENT_PAGE)
            return httpx.Response(
                303, headers={"location": f"{CI_PUBLIC}/authorize?code=code-1&state=st-1"}
            )
        if path == "/login/oauth/grant" and request.method == "POST":
            form = parse_qs(request.content.decode())
            if form.get("granted") != ["true"]:
                return httpx.Response(
                    303, headers={"location": f"{CI_PUBLIC}/authorize?error=access_denied"}
                )
            return httpx.Response(
                303, headers={"location": f"{CI_PUBLIC}/authorize?code=code-1&state=st-1"}
            )
        return httpx.Response(404)

    def _ci(self, request: httpx.Request, path: str, cookies: dict[str, str]) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        if path == "/authorize" and "code" not in query:
            return httpx.Response(
                303,
                headers={
                    "location": f"{FORGE_PUBLIC}/login/oauth/authorize?client_id=woodpecker"
                    f"&redirect_uri={CI_PUBLIC}/authorize&state=st-1",
                    "set-cookie": "oauth_state=st-1; Path=/",
                },
            )
        if path == "/authorize":
            if cookies.get("oauth_state") != "st-1" or query.get("code") != ["code-1"]:
                return httpx.Response(400, text="bad state")
            return httpx.Response(
                303, headers={"location": "/", "set-cookie": "user_sess=ci-sess; Path=/"}
            )
        if path == "/web-config.js":
            return httpx.Response(200, text=WEB_CONFIG)
        if path == "/api/user/token" and request.method == "POST":
            if cookies.get("user_sess") != "ci-sess":
                return httpx.Response(401)
            if request.headers.get("X-CSRF-TOKEN") != "csrf-ci":
                return httpx.Response(403)
            return httpx.Response(200, text="token-1\n")
        if path == "/api/user":
            if request.headers.get("Authorization") == "Bearer token-1":
                return httpx.Response(200, json={"login": "unicon-ci-acme"})
            return httpx.Response(401)
        return httpx.Response(404)


def _cookies(request: httpx.Request) -> dict[str, str]:
    header = request.headers.get("cookie", "")
    return dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part)


def _login(stack: Stack) -> CiLogin:
    return CiLogin(
        forge_public_url=FORGE_PUBLIC,
        forge_url=FORGE_INTERNAL,
        ci_public_url=CI_PUBLIC,
        ci_url=CI_INTERNAL,
        transport=httpx.MockTransport(stack.handle),
    )


@pytest.mark.parametrize("consent", [True, False])
async def test_the_dance_yields_a_working_token_over_the_internal_hosts(consent: bool) -> None:
    stack = Stack(consent=consent)

    token = await _login(stack).mint_token("unicon-ci-acme", PASSWORD)

    assert token == "token-1"
    assert {request.url.host for request in stack.seen} == {"forge.internal", "ci.internal"}
    paths = [f"{request.method} {request.url.path}" for request in stack.seen]
    assert paths[:4] == [
        "GET /user/login",
        "POST /user/login",
        "GET /authorize",
        "GET /login/oauth/authorize",
    ]
    assert ("POST /login/oauth/grant" in paths) is consent
    assert paths[-3:] == ["GET /web-config.js", "POST /api/user/token", "GET /api/user"]
    grant = [request for request in stack.seen if request.url.path == "/login/oauth/grant"]
    if consent:
        assert parse_qs(grant[0].content.decode())["state"] == ["st-1"]
    assert urlsplit(str(stack.seen[-1].url)).netloc == "ci.internal"


async def test_a_wrong_password_is_forbidden() -> None:
    stack = Stack()

    with pytest.raises(Forbidden, match="did not accept the sign-in as unicon-ci-acme"):
        await _login(stack).mint_token("unicon-ci-acme", "wrong")
    assert [request.url.path for request in stack.seen] == ["/user/login", "/user/login"]


async def test_a_page_that_is_not_the_consent_page_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = Stack()
    original = stack._forge

    def without_consent(
        request: httpx.Request, path: str, cookies: dict[str, str]
    ) -> httpx.Response:
        if path == "/login/oauth/authorize":
            return httpx.Response(200, text="<html>maintenance</html>")
        return original(request, path, cookies)

    monkeypatch.setattr(stack, "_forge", without_consent)

    with pytest.raises(Rejected, match="not the forge's consent page"):
        await _login(stack).mint_token("unicon-ci-acme", PASSWORD)


async def test_a_service_that_does_not_answer_is_unavailable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    login = CiLogin(
        forge_public_url=FORGE_PUBLIC,
        forge_url=FORGE_INTERNAL,
        ci_public_url=CI_PUBLIC,
        ci_url=CI_INTERNAL,
        transport=httpx.MockTransport(down),
    )
    with pytest.raises(Unavailable):
        await login.mint_token("unicon-ci-acme", PASSWORD)


def test_the_two_page_readers() -> None:
    assert hidden_inputs(CONSENT_PAGE) == {
        "_csrf": "csrf-forge",
        "client_id": "woodpecker",
        "redirect_uri": "http://ci.test/authorize",
        "state": "st-1",
    }
    assert javascript_string(WEB_CONFIG, "WOODPECKER_CSRF") == "csrf-ci"
    assert javascript_string(WEB_CONFIG, "WOODPECKER_NOPE") is None
