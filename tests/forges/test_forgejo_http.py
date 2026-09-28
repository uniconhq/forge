"""The HTTP client retries a server that is busy, raises `Unavailable` once
the retries are used up, turns every refusal into one of the five errors, and
signs each identity its own way.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount, AsUser, Credential
from forge.forges.forgejo.http import ForgejoAuth, Http, WoodpeckerAuth

ACME = AsOrgAccount("acme", forge_token="forge-token-acme", ci_token="ci-token-acme")


def _client(answers: list[httpx.Response | Exception], seen: list[httpx.Request]) -> Http:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    transport = httpx.MockTransport(handle)
    return Http(
        httpx.AsyncClient(base_url="http://forge.test", transport=transport),
        ForgejoAuth("admin"),
        backoff_seconds=0,
    )


async def test_a_busy_server_is_retried_and_succeeds() -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.Response(502), httpx.Response(200, json={"ok": True})], seen)

    response = await http.call(PLATFORM, "GET", "/api/v1/version")

    assert response.json() == {"ok": True}
    assert len(seen) == 2


async def test_a_server_that_stays_down_is_unavailable() -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.ConnectError("refused")] * 4, seen)

    with pytest.raises(Unavailable):
        await http.call(PLATFORM, "GET", "/api/v1/version")
    assert len(seen) == 4


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"message": "no"}, NotFound),
        (401, {"message": "no"}, Forbidden),
        (403, {"message": "no"}, Forbidden),
        (409, {"message": "no"}, Conflict),
        (422, {"message": "user already exists [name: x]"}, Conflict),
        (422, {"message": "no"}, Rejected),
    ],
)
async def test_a_refusal_is_one_of_the_five_errors(
    status: int, body: dict[str, str], error: type[Exception]
) -> None:
    http = _client([httpx.Response(status, json=body)], [])

    with pytest.raises(error):
        await http.call(PLATFORM, "GET", "/api/v1/thing")


async def test_each_identity_signs_its_own_way() -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.Response(200, json={})] * 3, seen)
    credential = Credential("access-7", "refresh-7", expires_at=_later())

    await http.call(PLATFORM, "GET", "/a")
    await http.call(AsUser(7, credential), "GET", "/b")
    await http.call(ACME, "GET", "/c")

    assert [request.headers["Authorization"] for request in seen] == [
        "token admin",
        "Bearer access-7",
        "token forge-token-acme",
    ]
    with pytest.raises(Forbidden):
        await http.call(CI_ADMIN, "GET", "/d")


async def test_the_ci_signs_the_administrator_and_org_accounts_only() -> None:
    auth = WoodpeckerAuth("ci-admin")
    assert await auth.header(CI_ADMIN) == "Bearer ci-admin"
    assert await auth.header(ACME) == "Bearer ci-token-acme"
    with pytest.raises(Forbidden):
        await auth.header(PLATFORM)


def test_an_org_accounts_credentials_are_never_printed() -> None:
    assert "token-acme" not in repr(ACME)


async def test_a_list_is_read_until_a_short_page() -> None:
    pages: list[Any] = [[{"n": index} for index in range(50)], [{"n": 50}]]
    seen: list[httpx.Request] = []
    http = _client([httpx.Response(200, json=page) for page in pages], seen)

    found = await http.get_all(PLATFORM, "/api/v1/things")

    assert len(found) == 51
    assert len(seen) == 2


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)
