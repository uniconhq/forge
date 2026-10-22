"""The object store the platform keeps grading run logs and run task steps'
outputs in, and the read-only way into the forge's own large-file store a
run task's step fetches the task's files by. A person's files go into the
forge's large-file store through the upload door (`port.uploads`), and never
pass through the platform.

A grading machine is handed URLs signed for the address machines reach the
platform at, each for one object, and writes its log and a step's outputs
straight there, and reads an earlier step's outputs and the task's large
files from there; the platform only reads what arrived.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Stored:
    """What the store says of one object: its length, and its SHA-256 in
    hex when it was written by a URL signed for one."""

    size: int
    sha256: str | None


class ObjectStore(Protocol):
    def put_url(self, key: str, *, expires_in: timedelta, sha256: str | None = None) -> str:
        """A URL a grading machine writes one object at `key` with, signed for
        the address machines reach the platform at, until `expires_in` has
        passed. Given `sha256`, in hex, the URL is signed for that checksum:
        the machine sends it, in base64, in the header
        `x-amz-checksum-sha256`, and the store refuses a body that is not
        of it, so the URL writes those bytes or nothing.
        """
        ...

    def get_url(self, key: str, *, expires_in: timedelta) -> str:
        """A URL a grading machine reads the one object at `key` with, signed
        as `put_url` signs, until `expires_in` has passed.
        """
        ...

    def lfs_get_url(self, digest: str, *, expires_in: timedelta) -> str:
        """A URL a grading machine reads the forge's large-file object of
        SHA-256 `digest` with, at the name the forge's store gives it,
        signed with a key that may only read that store, until
        `expires_in` has passed.
        """
        ...

    async def head(self, key: str) -> Stored:
        """What the store says of the object at `key`, its content unread.
        `NotFound` when there is none.
        """
        ...

    async def read(self, key: str, *, max_size: int | None = None) -> bytes:
        """The object at `key`. `NotFound` when there is none, and `Rejected`
        when it is larger than `max_size`, of which no more than `max_size`
        and one bytes are read.
        """
        ...
