"""One HTTP client for the host and one for the CI, built on the same
retrying `Http`. It signs each request for the identity the call is made
under, retries a server that is busy, and turns every refusal into one of the
port's five errors.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import AsOrgAccount, AsUser, CiAdmin, Identity, Platform
from forge.log import get_logger
from forge.port.tokens import TokenSource

log = get_logger(__name__)

CONCURRENT_CALLS = 8
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRIES = 3
BACKOFF_SECONDS = 0.2
PAGE_SIZE = 50
MAX_PAGES = 200

SERVER_ERROR = 500
NOT_FOUND = 404
FORBIDDEN = (401, 403)
CONFLICT = 409
ALREADY_EXISTS = "already exists"

Params = Mapping[str, str | int]


class Auth(Protocol):
    async def header(self, as_: Identity) -> str:
        """The `Authorization` value for a call made as `as_`."""
        ...


class ForgejoAuth:
    def __init__(self, admin_token: str, tokens: TokenSource) -> None:
        self._admin_token = admin_token
        self._tokens = tokens

    async def header(self, as_: Identity) -> str:
        match as_:
            case Platform():
                return f"token {self._admin_token}"
            case AsUser(credential=credential):
                return f"Bearer {credential.access}"
            case AsOrgAccount(org=org):
                return f"token {await self._tokens.forge_token(org)}"
            case CiAdmin():
                raise Forbidden("the CI administrator has no access to the forge")
        raise Forbidden("unknown identity")


class WoodpeckerAuth:
    def __init__(self, admin_token: str, tokens: TokenSource) -> None:
        self._admin_token = admin_token
        self._tokens = tokens

    async def header(self, as_: Identity) -> str:
        match as_:
            case CiAdmin():
                return f"Bearer {self._admin_token}"
            case AsOrgAccount(org=org):
                return f"Bearer {await self._tokens.ci_token(org)}"
        raise Forbidden("only the CI administrator and org accounts reach the CI")


class Http:
    def __init__(
        self,
        client: httpx.AsyncClient,
        auth: Auth,
        *,
        retries: int = RETRIES,
        backoff_seconds: float = BACKOFF_SECONDS,
    ) -> None:
        self._client = client
        self._auth = auth
        self._retries = retries
        self._backoff = backoff_seconds
        self._in_flight = asyncio.Semaphore(CONCURRENT_CALLS)

    async def call(
        self,
        as_: Identity,
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

    async def get_all(self, as_: Identity, path: str, **params: str | int) -> list[dict[str, Any]]:
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
                async with self._in_flight:
                    response = await self._client.request(
                        method, path, json=json, data=data, params=params, headers=headers
                    )
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise Unavailable(f"no answer from {path}: {type(exc).__name__}") from exc
                await self._pause(attempt)
                continue
            if response.status_code < SERVER_ERROR:
                return response
            if attempt == self._retries:
                raise Unavailable(f"{path} answered {response.status_code}")
            await self._pause(attempt)
        raise Unavailable(f"no answer from {path}")

    async def _pause(self, attempt: int) -> None:
        log.debug("http.retry", attempt=attempt + 1)
        await asyncio.sleep(self._backoff * (2**attempt))


def new_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=TIMEOUT)


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
