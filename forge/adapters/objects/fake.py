"""The grading run log store in memory. A test plays the grading machine
through `put`, handing back the URL the store handed out.

This is the only object store left: everything a person uploads goes into
the forge's own large-file store (`adapters.git.fake.uploads`).
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from forge.domain.clock import Clock
from forge.domain.errors import Forbidden, NotFound, Rejected

MACHINE_URL = "http://machines.test"
RESULTS_BUCKET = "unicon-results"


@dataclass(frozen=True, slots=True)
class _Signed:
    """What one handed-out URL allows: the key, and until when."""

    key: str
    expires_at: datetime


class FakeObjects:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.objects: dict[str, bytes] = {}
        self._signed: dict[str, _Signed] = {}

    def put_url(self, key: str, *, expires_in: timedelta) -> str:
        token = self._sign(_Signed(key, self._clock.now() + expires_in))
        return f"{MACHINE_URL}/{RESULTS_BUCKET}/{key}?signature={token}"

    async def read(self, key: str, *, max_size: int | None = None) -> bytes:
        try:
            content = self.objects[key]
        except KeyError:
            raise NotFound(f"no object at {key}") from None
        if max_size is not None and len(content) > max_size:
            raise Rejected(f"the object at {key} is larger than {max_size} bytes")
        return content

    def put(self, url: str, content: bytes) -> None:
        """Write `content` with a URL `put_url` handed out, as a grading
        machine does. `Forbidden` once it has expired.
        """
        self.objects[self._check(_signature(url)).key] = content

    def _sign(self, signed: _Signed) -> str:
        token = secrets.token_urlsafe(12)
        self._signed[token] = signed
        return token

    def _check(self, token: str) -> _Signed:
        signed = self._signed.get(token)
        if signed is None:
            raise Forbidden("the store was not asked for this")
        if self._clock.now() > signed.expires_at:
            raise Forbidden("the store's answer has expired")
        return signed


def _signature(url: str) -> str:
    return parse_qs(urlsplit(url).query).get("signature", [""])[0]
