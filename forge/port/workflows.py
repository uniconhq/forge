"""Workflows: arrangements of grading steps owned by an org or a user, marked
at the host so they can be searched, shared, versioned, starred and copied.
"""

from typing import Protocol

from forge.domain.content import File, Files
from forge.domain.identity import Identity
from forge.domain.ids import WorkflowId
from forge.domain.workflows import Visibility, Workflow


class WorkflowPort(Protocol):
    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId:
        """Make a workflow under `owner`, an org or the calling user.
        `Conflict` when the name is taken.
        """
        ...

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None: ...

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        """Let the user read the workflow."""
        ...

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None: ...

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str
    ) -> None:
        """Name the current state as a version. `Conflict` when the name is
        taken; `Forbidden` for a name reserved for protected versions.
        """
        ...

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File:
        """One file at a version. `NotFound` when there is no such version or
        file; `Forbidden` when the identity may not read the workflow.
        """
        ...

    async def search_public_workflows(self, query: str) -> tuple[Workflow, ...]:
        """Every public workflow whose name contains the query."""
        ...

    async def star_workflow(self, as_: Identity, workflow: WorkflowId) -> None: ...

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        """A new private workflow owned by `owner`, made from the source at
        `version`.
        """
        ...

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]:
        """Every workflow the user owns, with its visibility."""
        ...
