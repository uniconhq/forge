"""The grading results store in memory, with the forge's large-file
objects beside it. A test plays the grading machine through `put` and
`get`, handing back a URL the store handed out, which allows only the
method and the one object it was signed for, until it expires.

Everything a person uploads goes into the forge's own large-file store
(`adapters.git.fake.uploads`); a test puts what a machine should find
there in `lfs`, by SHA-256.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from forge.domain.clock import Clock
from forge.domain.errors import Forbidden, NotFound, Rejected
from forge.port.objects import Stored

MACHINE_URL = "http://machines.test"
RESULTS_BUCKET = "unicon-results"
LFS_BUCKET = "forgejo-lfs"


@dataclass(frozen=True, slots=True)
class _Signed:
    """What one handed-out URL allows: the method, the bucket and key,
    until when, and the SHA-256 a PUT was signed for."""

    key: str
    expires_at: datetime
    method: str = "PUT"
    bucket: str = RESULTS_BUCKET
    sha256: str | None = None


class FakeObjects:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.objects: dict[str, bytes] = {}
        self.lfs: dict[str, bytes] = {}
        self._signed: dict[str, _Signed] = {}
        self._checksums: dict[str, str] = {}

    def put_url(self, key: str, *, expires_in: timedelta, sha256: str | None = None) -> str:
        token = self._sign(_Signed(key, self._clock.now() + expires_in, sha256=sha256))
        return f"{MACHINE_URL}/{RESULTS_BUCKET}/{key}?signature={token}"

    def get_url(self, key: str, *, expires_in: timedelta) -> str:
        token = self._sign(_Signed(key, self._clock.now() + expires_in, "GET"))
        return f"{MACHINE_URL}/{RESULTS_BUCKET}/{key}?signature={token}"

    def lfs_get_url(self, digest: str, *, expires_in: timedelta) -> str:
        name = f"lfs/{digest[:2]}/{digest[2:4]}/{digest[4:]}"
        token = self._sign(_Signed(name, self._clock.now() + expires_in, "GET", LFS_BUCKET))
        return f"{MACHINE_URL}/{LFS_BUCKET}/{name}?signature={token}"

    async def head(self, key: str) -> Stored:
        try:
            content = self.objects[key]
        except KeyError:
            raise NotFound(f"no object at {key}") from None
        return Stored(len(content), self._checksums.get(key))

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
        machine does. `Forbidden` once it has expired, `Rejected` for
        content not of the SHA-256 it was signed for.
        """
        signed = self._check(_signature(url))
        if signed.method != "PUT":
            raise Forbidden("the store's answer was signed for another method")
        if signed.sha256 is not None:
            if hashlib.sha256(content).hexdigest() != signed.sha256:
                raise Rejected("the content is not of the checksum the URL was signed for")
            self._checksums[signed.key] = signed.sha256
        else:
            self._checksums.pop(signed.key, None)
        self.objects[signed.key] = content

    def get(self, url: str) -> bytes:
        """Read with a URL `get_url` or `lfs_get_url` handed out, as a
        grading machine does. `Forbidden` once it has expired, `NotFound`
        when nothing is there.
        """
        signed = self._check(_signature(url))
        if signed.method != "GET":
            raise Forbidden("the store's answer was signed for another method")
        if signed.bucket == LFS_BUCKET:
            digest = signed.key.removeprefix("lfs/").replace("/", "")
            found = self.lfs.get(digest)
        else:
            found = self.objects.get(signed.key)
        if found is None:
            raise NotFound(f"no object at {signed.key}")
        return found

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
