"""Files, versions and history as the port hands them out. A version is an
opaque id; a file carries the version it was read at, which a write presents
back as its conflict check.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from forge.domain.ids import VersionId

Files = dict[str, bytes]


@dataclass(frozen=True, slots=True)
class File:
    path: str
    content: bytes
    version: VersionId


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
    """One entry of a thing's history."""

    version: VersionId
    author_id: int | None
    message: str
    at: datetime
