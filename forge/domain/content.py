"""Files, versions and history as the port hands them out. A `VersionId` names
a point in a place's history. A `ConflictToken` is what a file is read with
and what a write presents back, so a write over a file that has changed since
is refused. Two files with the same content carry the same token, which is
how a comparison between two versions tells a changed file from one left as
it was without reading either.

An `Edit` is one file as a save writes it, with the token it was read with:
either the bytes the save carries, or an `Uploaded` naming a file already at
the forge, whose pointer the save writes instead, or none, which removes the
file; removing a folder is removing each file under it, named one by one;
a `FileSet` is every file of a place at one version, by path, with its
token. A file that is an upload carries its `UploadInfo`, read from the
pointer its commit holds (`forge.domain.uploads.upload_info`). `check_path`
is the rule every path a person names passes before it reaches the port:
plain segments joined by `/`, relative to the place; `path_problem` says
what is wrong with a path written into a task or a workflow, to the person
who wrote it. `has_path` is how a path ending in `/` names a folder, there
when some file is under it.
"""

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

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
        raise InvalidPath(
            f"{path!r} cannot be used as a file's path. Give a path inside the task or "
            "contest, with no empty, . or .. parts.",
            path=path,
        )
    return path


_EXAMPLES = {"task": "data/ or checker/checker.cpp", "workflow": "diff/ or diff/program.yaml"}


def path_problem(
    value: object,
    *,
    folder: bool | None,
    of: Literal["task", "workflow"] = "task",
) -> str | None:
    """What is wrong with `value` as the path of a file or folder of a task
    or workflow, if anything, said to the person who wrote it: a relative
    path of named parts, none `.` or `..`, none starting or ending with a
    space, and no backslash or control character. A folder's path ends in
    `/` and a file's does not; `folder` none takes either.
    """
    if not isinstance(value, str) or not value:
        return f"Give the path of a file or folder in the {of}, such as {_EXAMPLES[of]}."
    if value.startswith("/") or "\\" in value:
        return f"Give the path from the top of the {of}'s files, with forward slashes (/)."
    parts = value.removesuffix("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "The path cannot have empty, . or .. parts."
    if any(part != part.strip(" ") for part in parts):
        return "No part of the path can start or end with a space."
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return "The path cannot hold a control character, such as a tab or a new line."
    if folder is True and not value.endswith("/"):
        return "This is a folder, so its path ends with /, such as data/."
    if folder is False and value.endswith("/"):
        return "This is one file, so its path does not end with /."
    return None


def has_path(paths: Collection[str], path: str) -> bool:
    """Whether `path` is among `paths`, or for a path ending in `/`, whether
    some path is under that folder.
    """
    if path.endswith("/"):
        return any(name.startswith(path) for name in paths)
    return path in paths


def names_path(entry: str, path: str) -> bool:
    """Whether `entry` names the file at `path`: it is that path, or a
    folder, ending in `/`, that the file is under.
    """
    return entry == path or (entry.endswith("/") and path.startswith(entry))


def named_by(paths: Iterable[str], entry: str) -> set[str]:
    """The paths among `paths` that `entry` names, by `names_path`."""
    return {path for path in paths if names_path(entry, path)}


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
    `version` is a version holding the file as it was read, the one it was
    read at or the latest change to it, so the same bytes can be read again
    whole, such as by a download; none when the forge did not say.
    """

    path: str
    content: bytes
    token: ConflictToken
    upload: UploadInfo | None = None
    version: VersionId | None = None


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of a folder: a file, with the size of what its commit
    holds and that commit, the one that last changed it, which holds it as
    listed whatever the head has moved to since (none when the forge does
    not say), or a folder. `upload` tells an uploaded file from a typed one.
    `token` is the file's `ConflictToken`, as a read gives it, plain text so the
    entry is served as it is; `start` is how a small text
    file begins (`start_of`), none for any other file and for an upload,
    whose commit holds only the pointer to its bytes.
    """

    path: str
    kind: EntryKind
    size: int | None = None
    upload: UploadInfo | None = None
    version: VersionId | None = None
    token: str | None = None
    start: str | None = None


SMALL_FILE = 1024
"""The most bytes a file has for a listing to give its start: a row shows one
line or about 40 characters of it, and a larger file shows its size."""
START_CHARS = 80
"""The most characters of a small file's start a listing gives, enough for a
row's line with room to say whether more follows."""


def start_of(content: bytes) -> str | None:
    """How a file begins, as a listing gives it: its first `START_CHARS`
    characters when it is text, valid UTF-8 with no NUL, an empty file
    giving an empty start; none when it is not text. The whole file when
    the start, encoded, is as long as the file.
    """
    if b"\x00" in content:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[:START_CHARS]


@dataclass(frozen=True, slots=True)
class Change:
    """One entry of a place's history: the version it made, who made it, what
    they said about it and when. `author` is the username of `author_id`,
    which the history names whether or not they still hold a role there, and
    none when the forge knows no such account, the change has no author, or
    the platform made it, which is no person (a new task's first files).
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
    save carries or an upload the forge already holds, or none to remove the
    file, and the token it was read with, or none for a file the save
    creates. A removal always carries its token.
    """

    content: bytes | Uploaded | None
    token: ConflictToken | None = None


@dataclass(frozen=True, slots=True)
class WrittenEdit:
    """One file as the write carries it, once an edit naming an upload has
    become the pointer to it, and none for a file the save removes.
    Everything past the save's first step works on these, so nothing below
    it has to ask where a file's bytes came from.
    """

    content: bytes | None
    token: ConflictToken | None = None


@dataclass(frozen=True, slots=True)
class FileSet:
    """Every file of a place at one version, by path, each with its token,
    and the size in bytes of each the forge gave one for. A folder is there
    exactly when some file is under it.
    """

    version: VersionId
    tokens: Mapping[str, ConflictToken]
    sizes: Mapping[str, int] = field(default_factory=dict)

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
