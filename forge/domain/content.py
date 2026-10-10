"""Files, versions and history as the port hands them out. A `VersionId` names
a point in a place's history. A `ConflictToken` is what a file is read with
and what a write presents back, so a write over a file that has changed since
is refused. Two files with the same content carry the same token, which is
how a comparison between two versions tells a changed file from one left as
it was without reading either.

An `Edit` is one file as a save writes it, with the token it was read with:
either the bytes the save carries, or an `Uploaded` naming a file already at
the forge, whose pointer the save writes instead;
a `FileSet` is every file of a place at one version, by path, with its
token. A file that is an upload carries its `UploadInfo`, read from the
pointer its commit holds (`forge.domain.uploads.upload_info`). `check_path`
is the rule every path a person names passes before it reaches the port:
plain segments joined by `/`, relative to the place. `has_path` is how a
path ending in `/` names a folder, there when some file is under it.
"""

import uuid
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
class UploadInfo:
    """What a file that is an upload holds: the size and SHA-256 of the
    bytes the forge's large-file store keeps for it. Its commit holds the
    pointer to them, which is what `content` and `size` describe, so it is
    never opened as text, and it is changed by uploading it again.
    """

    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class File:
    """One file as it was read, with the token a write presents back, and
    `upload` when it is an upload, whose `content` is then the pointer.
    """

    path: str
    content: bytes
    token: ConflictToken
    upload: UploadInfo | None = None


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of a folder: a file, with the size of what its commit
    holds, or a folder. `upload` tells an uploaded file from a typed one.
    """

    path: str
    kind: EntryKind
    size: int | None = None
    upload: UploadInfo | None = None


@dataclass(frozen=True, slots=True)
class Change:
    """One entry of a place's history: the version it made, who made it, what
    they said about it and when. `author` is the username of `author_id`,
    which the history names whether or not they still hold a role there, and
    none when the forge knows no such account or the change has no author.
    """

    version: VersionId
    author_id: int | None
    message: str
    at: datetime
    author: str | None = None
    """Given by the port where the forge names the author with the change,
    and filled in by the history otherwise."""


@dataclass(frozen=True, slots=True)
class Uploaded:
    """A file whose content is an upload already at the forge: the save
    writes the pointer to it rather than any bytes, and the bytes went in
    through the upload door before the save (`services.uploads`).
    """

    upload: uuid.UUID


@dataclass(frozen=True, slots=True)
class Edit:
    """One file as a save writes it: its new content, either typed text the
    save carries or an upload the forge already holds, and the token it was
    read with, or none for a file the save creates.
    """

    content: bytes | Uploaded
    token: ConflictToken | None = None


@dataclass(frozen=True, slots=True)
class WrittenEdit:
    """One file as the write carries it, once an edit naming an upload has
    become the pointer to it. Everything past the save's first step works on
    these, so nothing below it has to ask where a file's bytes came from.
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
