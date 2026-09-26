"""The Forgejo client retries a forge that is busy, raises `Unavailable` once
the retries are used up, and turns every refusal into one of the five errors.
"""

from typing import Any

import httpx
import pytest

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import PLATFORM, AsOrgAccount, AsUser, Credential
from forge.forges.forgejo.http import ForgejoHttp


class Tokens:
    async def forge_token(self, org: str) -> str:
        return f"forge-token-{org}"

    async def ci_token(self, org: str) -> str:
        return f"ci-token-{org}"


def _client(answers: list[httpx.Response | Exception], seen: list[httpx.Request]) -> ForgejoHttp:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    transport = httpx.MockTransport(handle)
    return ForgejoHttp(
        httpx.AsyncClient(base_url="http://forge.test", transport=transport),
        admin_token="admin",
        tokens=Tokens(),
        backoff_seconds=0,
    )


async def test_a_busy_forge_is_retried_and_succeeds() -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.Response(502), httpx.Response(200, json={"ok": True})], seen)

    response = await http.call(PLATFORM, "GET", "/api/v1/version")

    assert response.json() == {"ok": True}
    assert len(seen) == 2


async def test_a_forge_that_stays_down_is_unavailable() -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.ConnectError("refused")] * 4, seen)

    with pytest.raises(Unavailable):
        await http.call(PLATFORM, "GET", "/api/v1/version")
    assert len(seen) == 4


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, NotFound), (401, Forbidden), (403, Forbidden), (409, Conflict), (422, Rejected)],
)
async def test_a_refusal_is_one_of_the_five_errors(status: int, error: type[Exception]) -> None:
    http = _client([httpx.Response(status, json={"message": "no"})], [])

    with pytest.raises(error, match="no"):
        await http.call(PLATFORM, "GET", "/api/v1/thing")


async def test_each_identity_signs_its_own_way() -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.Response(200, json={})] * 3, seen)
    credential = Credential("access-7", "refresh-7", expires_at=_far())

    await http.call(PLATFORM, "GET", "/a")
    await http.call(AsUser(7, credential), "GET", "/b")
    await http.call(AsOrgAccount("acme"), "GET", "/c")

    assert [request.headers["Authorization"] for request in seen] == [
        "token admin",
        "Bearer access-7",
        "token forge-token-acme",
    ]


async def test_a_list_is_read_until_an_empty_page() -> None:
    pages: list[Any] = [[{"n": 1}, {"n": 2}], [{"n": 3}], []]
    http = _client([httpx.Response(200, json=page) for page in pages], [])

    assert await http.get_all(PLATFORM, "/api/v1/things") == [{"n": 1}, {"n": 2}, {"n": 3}]


def _far() -> Any:
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(hours=1)
