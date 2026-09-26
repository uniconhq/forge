"""Files, versions and history as the port hands them out. A `VersionId` names
a point in a place's history. A `ConflictToken` is what a file is read with
and what a write presents back, so a write over a file that has changed since
is refused.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from forge.domain.ids import VersionId

Files = dict[str, bytes]


class ConflictToken(str):
    """Names the state of one file as it was read. Opaque above the port."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class File:
    path: str
    content: bytes
    token: ConflictToken


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str
    kind: EntryKind
    size: int | None = None


@dataclass(frozen=True, slots=True)
class Change:
    """One entry of a place's history."""

    version: VersionId
    author_id: int | None
    message: str
    at: datetime
