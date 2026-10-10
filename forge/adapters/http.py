"""The retrying HTTP client the adapters that speak HTTP share, each over its
own service: `Http[C]`, for calls made as a `C`. It signs each request for
whoever the call is made as, through the adapter's own `Auth`, retries a
server that is busy, and turns every refusal into one of the port's five
errors.

A retry never makes something twice. Every method but POST sets a state, so
reading, putting, patching and deleting are retried on a busy server or a
lost answer alike. A POST creates something, so it is retried only when it
never reached the server, the connection failing before anything was sent;
a POST the server answered with an error, or whose answer was lost, is
`Unavailable` at once, and the step that sent it finds on its next run
whether the thing was made.

Each client keeps at most `CONCURRENT_CALLS` connections, so at most that
many calls are in flight to one service from a process; a call waits for a
free one in order, for the pool timeout at most, and is `Unavailable` after
that without being asked again, since asking again would put it behind every
call that came after it. A connection is free again once its response is
closed: a plain request reads its body whole and closes it, and a stream
closes when its block ends.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.log import get_logger

log = get_logger(__name__)

CONCURRENT_CALLS = 8
LIMITS = httpx.Limits(max_connections=CONCURRENT_CALLS, max_keepalive_connections=CONCURRENT_CALLS)
TIMEOUT = httpx.Timeout(10.0, connect=5.0, pool=30.0)
RETRIES = 3
BACKOFF_SECONDS = 0.2
PAGE_SIZE = 50
MAX_PAGES = 200

SERVER_ERROR = 500
CREATES = frozenset({"POST"})
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
NOT_FOUND = 404
FORBIDDEN = (401, 403)
CONFLICT = 409
ALREADY_EXISTS = "already exists"

Params = Mapping[str, str | int]

DOT_SEGMENTS = frozenset({"", ".", ".."})


def segment(value: str) -> str:
    """One segment of a request's path holding a name, quoted whole, so a
    `/`, `?`, `#` or `%` in it stays part of the name and never reaches
    another endpoint. `NotFound` for an empty name, `.` or `..`, which a path
    resolves away and which name nothing at the host.
    """
    if value in DOT_SEGMENTS:
        raise NotFound(f"{value!r} names nothing")
    return quote(value, safe="")


def file_path(path: str) -> str:
    """A file's or a folder's path inside a request's path, each of its
    segments quoted as `segment` quotes a name, empty ones left out, so the
    empty path is the repository's root.
    """
    return "/".join(segment(part) for part in path.split("/") if part)


class Auth[C](Protocol):
    async def header(self, as_: C) -> str:
        """The `Authorization` value for a call made as `as_`."""
        ...


class Http[C]:
    def __init__(
        self,
        client: httpx.AsyncClient,
        auth: Auth[C],
        *,
        retries: int = RETRIES,
        backoff_seconds: float = BACKOFF_SECONDS,
    ) -> None:
        self._client = client
        self._auth = auth
        self._retries = retries
        self._backoff = backoff_seconds

    async def call(
        self,
        as_: C,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Params | None = None,
    ) -> httpx.Response:
        """One request as `as_`, refused as one of the five errors."""
        headers = {"Authorization": await self._auth.header(as_)}
        return await self.request(method, path, json=json, params=params, headers=headers)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One request with the headers given, retried while the server is
        busy, refused as one of the five errors.
        """
        response = await self._send(
            method, path, json=json, data=data, params=params, headers=headers
        )
        if response.is_success:
            return response
        raise refusal(response)

    async def authorization(self, as_: C) -> str:
        """The `Authorization` value a call as `as_` carries, for a request
        someone else makes on its behalf.
        """
        return await self._auth.header(as_)

    async def read_capped(
        self, as_: C, path: str, *, params: Params | None, max_size: int
    ) -> bytes:
        """The body of a GET as `as_`, read only while it stays within
        `max_size` bytes: `Rejected` past that, before the rest is read, so a
        big file never sits in memory whole. Not retried.
        """
        headers = {"Authorization": await self._auth.header(as_)}
        try:
            async with self._client.stream("GET", path, params=params, headers=headers) as response:
                if response.status_code >= SERVER_ERROR:
                    raise Unavailable(f"{path} answered {response.status_code}")
                if not response.is_success:
                    await response.aread()
                    raise refusal(response)
                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > max_size:
                    raise Rejected(f"{path} is larger than {max_size} bytes")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_size:
                        raise Rejected(f"{path} is larger than {max_size} bytes")
                return bytes(body)
        except httpx.HTTPError as exc:
            raise Unavailable(f"no answer from {path}: {type(exc).__name__}") from exc

    async def get_all(self, as_: C, path: str, **params: str | int) -> list[dict[str, Any]]:
        """Every page of a list endpoint."""
        collected: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self.call(
                as_, "GET", path, params={**params, "limit": PAGE_SIZE, "page": page}
            )
            batch: list[dict[str, Any]] = response.json()
            collected.extend(batch)
            if len(batch) < PAGE_SIZE:
                return collected
        raise Unavailable(f"{path} did not end within {MAX_PAGES} pages")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any,
        data: Mapping[str, str] | None,
        params: Params | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(
                    method, path, json=json, data=data, params=params, headers=headers
                )
            except httpx.PoolTimeout as exc:
                raise Unavailable(f"no connection free for {path}") from exc
            except httpx.HTTPError as exc:
                if attempt == self._retries or not _may_resend(method, exc):
                    raise Unavailable(f"no answer from {path}: {type(exc).__name__}") from exc
                await self._pause(attempt)
                continue
            if response.status_code < SERVER_ERROR:
                return response
            if attempt == self._retries or method in CREATES:
                raise Unavailable(f"{path} answered {response.status_code}")
            await self._pause(attempt)
        raise Unavailable(f"no answer from {path}")

    async def _pause(self, attempt: int) -> None:
        log.debug("http.retry", attempt=attempt + 1)
        await asyncio.sleep(self._backoff * (2**attempt))


def _may_resend(method: str, failure: httpx.HTTPError) -> bool:
    """Whether a request that failed on the way may be sent again: any that
    sets a state, and a create only when it never left.
    """
    return method not in CREATES or isinstance(failure, NOT_SENT)


def new_client(base_url: str, *, timeout: httpx.Timeout = TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, limits=LIMITS)


def message_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        for key in ("message", "error_description", "error"):
            if isinstance(body.get(key), str):
                return str(body[key])
    return response.text[:200]


def refusal(response: httpx.Response) -> Exception:
    detail = message_of(response)
    if response.status_code == NOT_FOUND:
        return NotFound(detail or "not found")
    if response.status_code in FORBIDDEN:
        return Forbidden(detail or "refused for this identity")
    if response.status_code == CONFLICT or ALREADY_EXISTS in detail:
        return Conflict(detail or "conflict")
    return Rejected(detail or f"answered {response.status_code}", **_oauth_members(response))


def _oauth_members(response: httpx.Response) -> dict[str, str]:
    """The `error` member of an OAuth refusal, carried on the error so the
    sign-in code can tell a spent grant from a wrong registration.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return {"error": str(body["error"])}
    return {}


def json_of(response: httpx.Response) -> dict[str, Any]:
    payload: dict[str, Any] = response.json()
    return payload


def list_of(response: httpx.Response) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = response.json()
    return payload
