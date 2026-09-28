"""Workspaces, submissions and publications. A workspace is where one
contestant, or the group entering together, works for one contest; a
submission and a publication are protected versions that only the platform
may create.
"""

from collections.abc import Sequence
from typing import Protocol

from forge.domain.content import Files
from forge.domain.identity import Identity
from forge.domain.ids import ContestId, PublicationId, SubmissionId, TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner


class WorkspacePort(Protocol):
    async def open_workspace(
        self,
        contest: ContestId,
        owner: WorkspaceOwner,
        member_ids: Sequence[int],
        tasks: Sequence[TaskId],
    ) -> WorkspaceId:
        """Make the place the owner works in for the contest, with a desk and
        one submission place per task, and give the members write access.
        `Conflict` when it already exists.
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

    async def publish(self, task: TaskId, files: Files) -> PublicationId:
        """Write the compiled plans and name that change as the next protected
        version, as the platform.
        """
        ...

    async def list_publications(self, task: TaskId) -> tuple[PublicationId, ...]:
        """Every publication of the task, oldest first."""
        ...
