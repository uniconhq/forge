"""The object store in memory. It holds each store's objects by key, and it
refuses what a real store refuses: a form post larger than its policy or
after its expiry, a part larger or smaller than its URL was signed for, and
a join of parts that do not match what arrived. A test plays the browser and
the grading machine through `post`, `put_part` and `put`, handing back what
the store handed out.
"""

import hashlib
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from forge.domain.clock import Clock
from forge.domain.errors import Forbidden, NotFound, Rejected
from forge.port.objects import FinishedPart, Measured, Store, UploadForm

OBJECTS_URL = "http://objects.test"
MACHINE_URL = "http://machines.test"


@dataclass
class _Parts:
    key: str
    received: dict[int, bytes] = field(default_factory=dict)
    etags: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Signed:
    """What one handed-out form or URL allows: the key, the store, the most
    or exact number of bytes, and until when.
    """

    store: Store
    key: str
    max_size: int | None
    length: int | None
    expires_at: datetime
    parts_id: str | None = None
    number: int | None = None


class FakeObjects:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.objects: dict[Store, dict[str, bytes]] = {store: {} for store in Store}
        self._parts: dict[str, _Parts] = {}
        self._signed: dict[str, _Signed] = {}
        self.deleted: list[tuple[Store, str]] = []

    def upload_form(self, key: str, *, max_size: int, expires_in: timedelta) -> UploadForm:
        token = self._sign(
            _Signed(Store.UPLOADS, key, max_size, None, self._clock.now() + expires_in)
        )
        return UploadForm(
            url=f"{OBJECTS_URL}/unicon-uploads/",
            fields={"bucket": "unicon-uploads", "key": key, "policy": token},
        )

    async def start_parts(self, key: str) -> str:
        parts_id = secrets.token_hex(8)
        self._parts[parts_id] = _Parts(key)
        return parts_id

    def part_url(
        self, key: str, parts_id: str, number: int, *, length: int, expires_in: timedelta
    ) -> str:
        token = self._sign(
            _Signed(
                Store.UPLOADS,
                key,
                None,
                length,
                self._clock.now() + expires_in,
                parts_id=parts_id,
                number=number,
            )
        )
        return f"{OBJECTS_URL}/unicon-uploads/{key}?partNumber={number}&signature={token}"

    async def finish_parts(self, key: str, parts_id: str, parts: Sequence[FinishedPart]) -> None:
        found = self._parts.get(parts_id)
        if found is None or found.key != key:
            raise NotFound("no such upload in parts")
        numbers = [part.number for part in parts]
        if not parts or numbers != sorted(set(numbers)):
            raise Rejected("the parts are not listed once each in order")
        for part in parts:
            if found.etags.get(part.number) != part.etag:
                raise Rejected(f"part {part.number} is not the one the store holds")
        self.objects[Store.UPLOADS][key] = b"".join(found.received[number] for number in numbers)
        del self._parts[parts_id]

    async def abandon_parts(self, key: str, parts_id: str) -> None:
        self._parts.pop(parts_id, None)

    def put_url(self, store: Store, key: str, *, expires_in: timedelta) -> str:
        token = self._sign(_Signed(store, key, None, None, self._clock.now() + expires_in))
        return f"{MACHINE_URL}/unicon-{store}/{key}?signature={token}"

    async def measure(self, store: Store, key: str) -> Measured | None:
        content = self.objects[store].get(key)
        if content is None:
            return None
        return Measured(size=len(content), sha256=hashlib.sha256(content).digest())

    async def read(self, store: Store, key: str, *, max_size: int | None = None) -> bytes:
        try:
            content = self.objects[store][key]
        except KeyError:
            raise NotFound(f"no object at {key}") from None
        if max_size is not None and len(content) > max_size:
            raise Rejected(f"the object at {key} is larger than {max_size} bytes")
        return content

    async def delete(self, store: Store, key: str) -> None:
        self.deleted.append((store, key))
        self.objects[store].pop(key, None)

    def post(self, fields: Mapping[str, str], content: bytes) -> None:
        """Post `content` with the fields of a form the store handed out, as
        a browser does. `Forbidden` once the form has expired; `Rejected` for
        more bytes than its policy allows.
        """
        signed = self._check(fields.get("policy", ""))
        assert signed.max_size is not None
        if len(content) > signed.max_size:
            raise Rejected("the file is larger than the policy allows")
        self.objects[Store.UPLOADS][signed.key] = content

    def put_part(self, url: str, content: bytes) -> str:
        """Send one part to a part URL the store handed out, and return the
        value the store answers it with. `Forbidden` once the URL has
        expired, or for any length but the one it was signed for.
        """
        signed = self._check(_signature(url))
        if signed.length != len(content):
            raise Forbidden("the part's length is not the one the URL was signed for")
        assert signed.parts_id is not None and signed.number is not None
        found = self._parts.get(signed.parts_id)
        if found is None:
            raise NotFound("no such upload in parts")
        etag = hashlib.md5(content, usedforsecurity=False).hexdigest()
        found.received[signed.number] = content
        found.etags[signed.number] = etag
        return etag

    def put(self, url: str, content: bytes) -> None:
        """Write `content` with a URL `put_url` handed out, as a grading
        machine does. `Forbidden` once it has expired.
        """
        signed = self._check(_signature(url))
        self.objects[signed.store][signed.key] = content

    def _sign(self, signed: _Signed) -> str:
        token = secrets.token_urlsafe(12)
        self._signed[token] = signed
        return token

    def _check(self, token: str) -> _Signed:
        signed = self._signed.get(token)
        if signed is None:
            raise Forbidden("the store did not hand this out")
        if self._clock.now() >= signed.expires_at:
            raise Forbidden("the signature has expired")
        return signed


def _signature(url: str) -> str:
    return parse_qs(urlsplit(url).query).get("signature", [""])[0]
