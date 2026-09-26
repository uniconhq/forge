"""Contest and task content: the files that define them, read and written as
the calling organiser, with a conflict check on every write.
"""

from typing import Protocol

from forge.domain.content import Change, ConflictToken, File, Files, TreeEntry
from forge.domain.identity import Identity
from forge.domain.ids import ContestId, OrgName, TaskId, VersionId

ContentPlace = ContestId | TaskId


class ContentPort(Protocol):
    async def create_contest(self, org: OrgName, name: str, files: Files) -> ContestId:
        """Make a contest under the org with its starter files, as the
        platform. `Conflict` when the name is taken.
        """
        ...

    async def create_task(self, contest: ContestId, name: str, files: Files) -> TaskId:
        """Make a task under the contest with its starter files, as the
        platform. `Conflict` when the name is taken.
        """
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
