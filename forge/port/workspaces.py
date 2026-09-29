"""Workspaces, submissions and publications. A workspace is where one
contestant, or the group entering together, works for one contest: a desk
their questions live in and a place to submit each task to, made one part
at a time so a part that fails is made again alone. A submission and a
publication are protected versions that only the platform may create. A
publication names a version a save already wrote and carries a note, the
short document `forge.domain.publications` writes and reads.
"""

from collections.abc import Sequence
from typing import Protocol

from forge.domain.content import Files
from forge.domain.identity import Identity
from forge.domain.ids import (
    ContestId,
    PublicationId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
)
from forge.domain.names import WorkspaceOwner
from forge.domain.publications import Publication


class WorkspacePort(Protocol):
    async def open_workspace(
        self, contest: ContestId, owner: WorkspaceOwner, member_ids: Sequence[int]
    ) -> WorkspaceId:
        """Make the owner's workspace in the contest with its desk, which the
        contest's organisers read, and give the members write access to the
        desk, as the platform. What is there already is kept and only what
        is missing is made, so this can be run again.
        """
        ...

    def workspace_of(self, contest: ContestId, owner: WorkspaceOwner) -> WorkspaceId:
        """The id the owner's workspace in the contest has, opened or not. No
        call is made.
        """
        ...

    async def open_submission_place(
        self, workspace: WorkspaceId, task: TaskId, member_ids: Sequence[int]
    ) -> None:
        """Make the workspace's place to submit the task, with its submissions
        reserved for the platform, and give the members write access to it,
        as the platform. It can be run again, as `open_workspace` can.
        `NotFound` for a task of another contest.
        """
        ...

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        """Take the members' access away and keep the contents."""
        ...

    async def list_submissions(
        self, workspace: WorkspaceId, task: TaskId
    ) -> tuple[SubmissionId, ...]:
        """Every submission the workspace made for the task, oldest first."""
        ...

    async def record_submission(
        self, as_: Identity, workspace: WorkspaceId, task: TaskId, files: Files
    ) -> SubmissionId:
        """Write the files as `as_`, the contestant submitting, so the change
        is theirs, and name that exact change as the next protected version,
        as the platform. Two submissions at once each get their own number
        over their own files.
        """
        ...

    async def publish(self, task: TaskId, at: VersionId, note: str) -> PublicationId:
        """Name the task's version `at` as its next publication, numbered after
        the highest there is, carrying `note`, as the platform. Nothing is
        written: the save that made `at` wrote everything the publication
        freezes. Two publications at once each get their own number.
        """
        ...

    async def list_publications(self, task: TaskId) -> tuple[Publication, ...]:
        """Every publication of the task, oldest first, each with what its
        note says.
        """
        ...
