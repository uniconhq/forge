"""Files, versions and history as the port hands them out. A `VersionId` names
a point in a place's history. A `ConflictToken` is what a file is read with
and what a write presents back, so a write over a file that has changed since
is refused. Two files with the same content carry the same token, which is
how a comparison between two versions tells a changed file from one left as
it was without reading either.

An `Edit` is one file as a save writes it, with the token it was read with;
a `FileSet` is every file of a place at one version, by path, with its
token. `check_path` is the rule every path a person names passes before it
reaches the port: plain segments joined by `/`, relative to the place.
`has_path` is how a path ending in `/` names a folder, there when some file
is under it.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from forge.domain.errors import InvalidPath
from forge.domain.ids import VersionId

Files = dict[str, bytes]

UNSAFE_CHARACTERS = frozenset('\\?#%:*"<>|')


def check_path(path: str) -> str:
    """`path` when it is a plain relative path, and `InvalidPath` otherwise:
    no empty, `.` or `..` segment, no leading `/`, and no backslash, control
    character or character a URL or git reads as something else.
    """
    segments = path.split("/")
    if (
        not path
        or any(segment in ("", ".", "..") for segment in segments)
        or any(character in UNSAFE_CHARACTERS or ord(character) < 32 for character in path)
    ):
        raise InvalidPath(f"{path!r} is not a path inside the place.", path=path)
    return path


def has_path(paths: Collection[str], path: str) -> bool:
    """Whether `path` is among `paths`, or for a path ending in `/`, whether
    some path is under that folder.
    """
    if path.endswith("/"):
        return any(name.startswith(path) for name in paths)
    return path in paths


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
    """One entry of a place's history: the version it made, who made it, what
    they said about it and when.
    """

    version: VersionId
    author_id: int | None
    message: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Edit:
    """One file as a save writes it: its new content, and the token it was
    read with, or none for a file the save creates.
    """

    content: bytes
    token: ConflictToken | None = None


@dataclass(frozen=True, slots=True)
class FileSet:
    """Every file of a place at one version, by path, each with its token. A
    folder is there exactly when some file is under it.
    """

    version: VersionId
    tokens: Mapping[str, ConflictToken]

    def has(self, path: str) -> bool:
        """Whether the file is there, or for a path ending in `/`, whether
        some file is under that folder.
        """
        return has_path(self.tokens.keys(), path)

    def under(self, path: str) -> dict[str, ConflictToken]:
        """The file at `path`, or every file under it when it ends in `/`."""
        if path.endswith("/"):
            return {name: token for name, token in self.tokens.items() if name.startswith(path)}
        return {path: self.tokens[path]} if path in self.tokens else {}
