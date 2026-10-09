"""The HTTP client retries a server that is busy, raises `Unavailable` once
the retries are used up, never sends a create twice once it reached the
server, turns every refusal into one of the five errors, and signs each
identity its own way. Over a real connection pool it keeps eight calls in
flight, the ninth waiting its turn and `Unavailable` once the pool timeout
passes, and every response, a stream's included, gives its connection back.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import PLATFORM, AsOrgAccount, AsUser, Credential
from forge.forges.forgejo.ci_state import WoodpeckerState, written
from forge.forges.forgejo.http import (
    CI_ADMIN,
    CONCURRENT_CALLS,
    ForgejoAuth,
    Http,
    WoodpeckerAuth,
    new_client,
)

ACME = AsOrgAccount(
    "acme",
    forge_token="forge-token-acme",
    ci_state=written(WoodpeckerState(4, "ci-token-acme", None)),
)


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


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
async def test_a_request_that_sets_a_state_is_retried_on_a_busy_server_or_a_lost_answer(
    method: str,
) -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.Response(502), httpx.ReadTimeout("lost"), httpx.Response(204)], seen)

    response = await http.call(PLATFORM, method, "/api/v1/teams/1/members/ada")

    assert response.status_code == 204
    assert len(seen) == 3


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(502), httpx.ReadTimeout("lost"), httpx.RemoteProtocolError("cut")],
    ids=["busy", "answer-lost", "cut-off"],
)
async def test_a_create_that_reached_the_server_is_never_sent_again(
    failure: httpx.Response | Exception,
) -> None:
    seen: list[httpx.Request] = []
    http = _client([failure, httpx.Response(201, json={"id": 1})], seen)

    with pytest.raises(Unavailable):
        await http.call(PLATFORM, "POST", "/api/v1/orgs", json={"username": "acme"})
    assert len(seen) == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow")],
    ids=["refused", "connect-timeout"],
)
async def test_a_create_that_never_left_is_sent_again(failure: Exception) -> None:
    seen: list[httpx.Request] = []
    http = _client([failure, httpx.Response(201, json={"id": 1})], seen)

    response = await http.call(PLATFORM, "POST", "/api/v1/orgs", json={"username": "acme"})

    assert response.json() == {"id": 1}
    assert len(seen) == 2


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_call_that_found_no_free_connection_is_not_asked_again(method: str) -> None:
    seen: list[httpx.Request] = []
    http = _client([httpx.PoolTimeout("busy"), httpx.Response(200, json={})], seen)

    with pytest.raises(Unavailable):
        await http.call(PLATFORM, method, "/api/v1/orgs")
    assert len(seen) == 1


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


class Server:
    """An HTTP server on a local port that holds every `/held` request until
    `release` is set, answers `/large` with more than a capped read takes,
    and counts the connections opened to it.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.connections = 0
        self.held = 0
        self.url = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while head := await reader.readuntil(b"\r\n\r\n"):
                if head.startswith(b"GET /held "):
                    self.held += 1
                    await self.release.wait()
                    body = b"ok"
                else:
                    body = b"x" * 4096
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    found = Server()
    listening = await asyncio.start_server(found.handle, "127.0.0.1", 0)
    found.url = f"http://127.0.0.1:{listening.sockets[0].getsockname()[1]}"
    async with listening:
        yield found
        found.release.set()


def _pooled(server: Server, pool: float) -> Http:
    timeout = httpx.Timeout(5.0, pool=pool)
    return Http(new_client(server.url, timeout=timeout), ForgejoAuth("admin"), backoff_seconds=0)


async def _until(condition: Any) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the server never saw it")


async def test_a_ninth_call_waits_for_one_of_the_eight_to_finish(server: Server) -> None:
    http = _pooled(server, pool=5.0)
    calls = [
        asyncio.create_task(http.call(PLATFORM, "GET", "/held"))
        for _ in range(CONCURRENT_CALLS + 1)
    ]
    await _until(lambda: server.held == CONCURRENT_CALLS)
    await asyncio.sleep(0.1)
    assert (server.held, server.connections) == (CONCURRENT_CALLS, CONCURRENT_CALLS)

    server.release.set()
    answers = await asyncio.gather(*calls)

    assert [answer.text for answer in answers] == ["ok"] * (CONCURRENT_CALLS + 1)
    assert server.connections == CONCURRENT_CALLS
    await http.aclose()


async def test_a_call_that_waits_past_the_pool_timeout_is_unavailable(server: Server) -> None:
    http = _pooled(server, pool=0.2)
    calls = [
        asyncio.create_task(http.call(PLATFORM, "GET", "/held")) for _ in range(CONCURRENT_CALLS)
    ]
    await _until(lambda: server.held == CONCURRENT_CALLS)

    with pytest.raises(Unavailable, match="no connection free"):
        await http.call(PLATFORM, "GET", "/held")

    server.release.set()
    await asyncio.gather(*calls)
    await http.aclose()


async def test_a_capped_read_gives_its_connection_back_whether_it_fits_or_not(
    server: Server,
) -> None:
    http = _pooled(server, pool=0.5)

    for _ in range(CONCURRENT_CALLS + 2):
        with pytest.raises(Rejected):
            await http.read_capped(PLATFORM, "/large", params=None, max_size=100)
        assert await http.read_capped(PLATFORM, "/large", params=None, max_size=10_000)

    server.release.set()
    assert (await http.call(PLATFORM, "GET", "/held")).text == "ok"
    await http.aclose()
