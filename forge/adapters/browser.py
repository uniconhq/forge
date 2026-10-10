"""A browser with nobody at it, for the sign-ins a service allows only
through its web pages. It keeps the cookies each service sets, follows no
redirect on its own, and sends a request for a service's public address to
its internal one: the platform's process cannot reach the public names, while
every service writes its redirects under them. A fresh browser with an empty
cookie jar is opened for each sign-in, so one account's session never carries
over into another's.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
REDIRECTS = (301, 302, 303, 307, 308)


class Browser:
    """`addresses` pairs each service's public address with its internal
    one.
    """

    def __init__(self, client: httpx.AsyncClient, addresses: Sequence[tuple[str, str]]) -> None:
        self._client = client
        self._addresses = tuple(
            (public.rstrip("/"), internal.rstrip("/")) for public, internal in addresses
        )

    async def request(self, method: str, url: str, **options: Any) -> httpx.Response:
        return await self._client.request(method, self.rewrite(url), **options)

    def rewrite(self, url: str) -> str:
        """`url` at the internal address of the service whose public one it
        is under, and as it is otherwise.
        """
        for public, internal in self._addresses:
            if url == public or url.startswith((f"{public}/", f"{public}?")):
                return internal + url[len(public) :]
        return url

    def has_cookie(self, name: str) -> bool:
        return any(cookie.name == name for cookie in self._client.cookies.jar)


@asynccontextmanager
async def open_browser(
    addresses: Sequence[tuple[str, str]], *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[Browser]:
    """A fresh browser over `addresses`; `transport` replaces the network,
    which is how a test scripts the services.
    """
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=TIMEOUT, transport=transport
    ) as client:
        yield Browser(client, addresses)
