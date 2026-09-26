"""One HTTP client for every call to Forgejo. It signs each request for the
identity the call is made under, retries a forge that is busy, and turns every
refusal into one of the port's five errors.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import AsOrgAccount, AsUser, CiAdmin, Identity, Platform
from forge.log import get_logger

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


class TokenSource(Protocol):
    """Where the credentials of the org accounts come from."""

    async def forge_token(self, org: str) -> str: ...

    async def ci_token(self, org: str) -> str: ...


class ForgejoHttp:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        admin_token: str,
        tokens: TokenSource,
        retries: int = RETRIES,
        backoff_seconds: float = BACKOFF_SECONDS,
    ) -> None:
        self._client = client
        self._admin_token = admin_token
        self._tokens = tokens
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
        data: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Make one request as `as_`. A 404 is `NotFound`, 401 and 403 are
        `Forbidden`, 409 is `Conflict`, any other 4xx is `Rejected`, and a forge
        that does not answer after the retries is `Unavailable`.
        """
        sent = dict(headers or {})
        sent.update(await self._authorization(as_))
        response = await self._send(method, path, json=json, data=data, params=params, headers=sent)
        if response.is_success:
            return response
        raise _refusal(response)

    async def get_all(self, as_: Identity, path: str, **params: str | int) -> list[dict[str, Any]]:
        """Every page of a list endpoint, until a page comes back empty."""
        collected: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self.call(
                as_, "GET", path, params={**params, "limit": PAGE_SIZE, "page": page}
            )
            batch: list[dict[str, Any]] = response.json()
            if not batch:
                return collected
            collected.extend(batch)
        raise Unavailable(f"{path} did not end within {MAX_PAGES} pages")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _authorization(self, as_: Identity) -> dict[str, str]:
        match as_:
            case Platform():
                return {"Authorization": f"token {self._admin_token}"}
            case AsUser(credential=credential):
                return {"Authorization": f"Bearer {credential.access}"}
            case AsOrgAccount(org=org):
                return {"Authorization": f"token {await self._tokens.forge_token(org)}"}
            case CiAdmin():
                raise Forbidden("the CI administrator has no access to the forge")
        raise Forbidden("unknown identity")

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any,
        data: Mapping[str, str] | None,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        for attempt in range(self._retries + 1):
            try:
                async with self._in_flight:
                    response = await self._client.request(
                        method, path, json=json, data=data, params=params, headers=headers
                    )
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise Unavailable(f"the forge did not answer: {type(exc).__name__}") from exc
                await self._pause(attempt)
                continue
            if response.status_code < SERVER_ERROR:
                return response
            if attempt == self._retries:
                raise Unavailable(f"the forge answered {response.status_code}")
            await self._pause(attempt)
        raise Unavailable("the forge did not answer")

    async def _pause(self, attempt: int) -> None:
        log.debug("forge.retry", attempt=attempt + 1)
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


def _refusal(response: httpx.Response) -> Exception:
    detail = message_of(response)
    if response.status_code == NOT_FOUND:
        return NotFound(detail or "not found at the forge")
    if response.status_code in FORBIDDEN:
        return Forbidden(detail or "the forge refused this identity")
    if response.status_code == CONFLICT:
        return Conflict(detail or "the forge reports a conflict")
    return Rejected(detail or f"the forge answered {response.status_code}")
