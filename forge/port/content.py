"""Contest and task content: the places that hold the files defining them,
made and kept protected as the platform, and the files themselves, read and
written as the calling organiser, with a conflict check on every write.
"""

from collections.abc import Mapping
from typing import Protocol

from forge.domain.content import Change, ConflictToken, File, Files, FileSet, TreeEntry
from forge.domain.identity import Identity
from forge.domain.ids import ContestId, OrgName, TaskId, VersionId

ContentPlace = ContestId | TaskId


class ContentPort(Protocol):
    async def create_contest(self, org: OrgName, name: str, files: Files) -> ContestId:
        """Make a contest under the org with its starter files, as the
        platform, and nothing else: `secure` gives it its roles and its
        protection. `Conflict` when the name is taken.
        """
        ...

    async def create_task(self, contest: ContestId, name: str, files: Files) -> TaskId:
        """Make a task under the contest with its starter files, as the
        platform, and nothing else: `secure` gives it its roles and its
        protection. `Conflict` when the name is taken.
        """
        ...

    async def secure(self, place: ContentPlace) -> int:
        """Give the place what keeps it right, as the platform: the roles of
        its contest and, for a task, of the task itself reach it; its history
        cannot be rewritten; and for a task, publications are reserved for
        the platform. Each is checked first and only what is missing is put
        back, so this can be run again. Returns how many it had to put back.
        """
        ...

    async def exists(self, place: ContentPlace) -> bool:
        """Whether the contest or task is there, read as the platform."""
        ...

    async def list_contests(self, as_: Identity, org: OrgName) -> tuple[ContestId, ...]:
        """Every contest in the org the identity may read, by name."""
        ...

    async def list_tasks(self, as_: Identity, contest: ContestId) -> tuple[TaskId, ...]:
        """Every task in the contest the identity may read, by name."""
        ...

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, at: VersionId | None = None
    ) -> File:
        """One file at the latest version, or at `at`. `NotFound` when there is
        no such file; `Forbidden` when the identity may not read it.
        """
        ...

    async def write_file(
        self,
        as_: Identity,
        place: ContentPlace,
        path: str,
        content: bytes,
        *,
        message: str,
        expected: ConflictToken | None,
    ) -> VersionId:
        """Write one file and return the version it made. `expected` is the
        token the file was read with, or none to create it; `Conflict` when
        the file has changed since, or exists when none was expected.
        """
        ...

    async def save_files(
        self,
        as_: Identity,
        place: ContentPlace,
        files: Mapping[str, bytes | None],
        *,
        expected: Mapping[str, ConflictToken | None],
        message: str,
    ) -> VersionId:
        """Write every file in `files` as one change, as `as_`, and return the
        version it made; a file given as none is removed. `expected` holds
        the token each file was read with, and none, or no entry, for a file
        the change creates. `Conflict`, with nothing written, when any of
        them has changed since or exists when none was expected.
        """
        ...

    async def list_files(
        self, as_: Identity, place: ContentPlace, *, at: VersionId | None = None
    ) -> FileSet:
        """Every file in the place at the latest version, or at `at`, with
        the version that is and each file's token.
        """
        ...

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        """The entries directly under `path`."""
        ...

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]:
        """Every change to the place, or to one path in it, newest first."""
        ...
