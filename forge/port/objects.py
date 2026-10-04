"""The object store the platform keeps grading run logs in. Nothing else
goes here: a person's files go into the forge's own large-file store through
the upload door (`port.uploads`), and never pass through the platform.

A grading machine is handed a URL signed for the address machines reach the
platform at, and writes its log straight there; the platform only reads what
arrived.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Measured:
    """What arrived at a key: its size in bytes and the SHA-256 of its
    content.
    """

    size: int
    sha256: bytes


class ObjectStore(Protocol):
    def put_url(self, key: str, *, expires_in: timedelta) -> str:
        """A URL a grading machine writes one object at `key` with, signed for
        the address machines reach the platform at, until `expires_in` has
        passed.
        """
        ...

    async def read(self, key: str, *, max_size: int | None = None) -> bytes:
        """The object at `key`. `NotFound` when there is none, and `Rejected`
        when it is larger than `max_size`, of which no more than `max_size`
        and one bytes are read.
        """
        ...
