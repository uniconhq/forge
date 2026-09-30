"""The object store the platform keeps large, short-lived things in: what a
browser uploads before a submit, and the log a grading run writes. Nothing
passes through the platform on the way in: a browser is handed a form or
part URLs signed for the platform's public address, a grading machine a URL
signed for the address machines reach it at, and the platform itself only
measures, reads and removes what arrived.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol


class Store(StrEnum):
    """Which of the two stores an object is in."""

    UPLOADS = "uploads"
    RESULTS = "results"


@dataclass(frozen=True, slots=True)
class UploadForm:
    """A form a browser posts one file with: where to post it, and the
    fields to send before the file, as they are.
    """

    url: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Measured:
    """What arrived at a key: its size in bytes and the SHA-256 of its
    content.
    """

    size: int
    sha256: bytes


@dataclass(frozen=True, slots=True)
class FinishedPart:
    """A part the browser sent: its number and the value the store answered
    it with, which names what it holds.
    """

    number: int
    etag: str


class ObjectStore(Protocol):
    def upload_form(self, key: str, *, max_size: int, expires_in: timedelta) -> UploadForm:
        """A form that writes one object at `key` in the uploads store, of at
        most `max_size` bytes, until `expires_in` has passed. The store
        refuses a larger file or a late one itself.
        """
        ...

    async def start_parts(self, key: str) -> str:
        """Begin an object at `key` in the uploads store sent in parts, and
        return the id that ties its parts together.
        """
        ...

    def part_url(
        self, key: str, parts_id: str, number: int, *, length: int, expires_in: timedelta
    ) -> str:
        """A URL a browser sends part `number` to, signed for exactly `length`
        bytes, until `expires_in` has passed.
        """
        ...

    async def finish_parts(self, key: str, parts_id: str, parts: Sequence[FinishedPart]) -> None:
        """Join the parts into the object. `Rejected` when a part is missing
        or is not the one the store holds; `NotFound` when there is no such
        upload in parts.
        """
        ...

    async def abandon_parts(self, key: str, parts_id: str) -> None:
        """Drop an upload in parts and every part of it. One that is gone
        already is left as it is.
        """
        ...

    def put_url(self, store: Store, key: str, *, expires_in: timedelta) -> str:
        """A URL a grading machine writes one object at `key` with, signed for
        the address machines reach the platform at, until `expires_in` has
        passed.
        """
        ...

    async def measure(self, store: Store, key: str) -> Measured | None:
        """The size and digest of the object at `key`, read through once, or
        none when there is no object there.
        """
        ...

    async def read(self, store: Store, key: str, *, max_size: int | None = None) -> bytes:
        """The object at `key`. `NotFound` when there is none, and `Rejected`
        when it is larger than `max_size`, of which no more than `max_size`
        and one bytes are read.
        """
        ...

    async def delete(self, store: Store, key: str) -> None:
        """Remove the object at `key`; one that is gone already is left as it
        is.
        """
        ...
