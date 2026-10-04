"""Workflows: arrangements of grading steps owned by an org or a user, marked
at the host so they can be searched, shared, versioned, starred and copied.

A host lets only the platform create a place, so a workflow is made as the
platform in its owner's name: a person's own, which they then own, or an
org's, which the organisers whose role at the org covers it reach, admin and
manager to write and observer to read. The person a call is made for writes
its first change, names its versions and reads it, with the host's own check
underneath. Who may read it is changed as the platform, once the host has
said the person may write it.
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
        """Make a workflow under `owner`, an org or a user, as the platform,
        with its files written as `as_`, who must be able to write under that
        owner: the user themself, or at an org a manager or admin there.
        Whether `as_` may create under `owner` at all is the caller's rule,
        which `forge.services.workflows` keeps; the host checks only the
        write. `Conflict` when the name is taken.
        """
        ...

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None:
        """Make the workflow public or private, as the platform once the host
        says `as_` may write it; `Forbidden` otherwise.
        """
        ...

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        """Let the user read the workflow, as the platform once the host says
        `as_` may write it; `Forbidden` otherwise.
        """
        ...

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        """Take the user's read of the workflow away, as `share_workflow`
        gives it.
        """
        ...

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
        `version`, which `as_` reads, and made the way `create_workflow`
        makes one.
        """
        ...

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]:
        """Every workflow the user owns, with its visibility."""
        ...

    async def workflow_key(self, workflow: WorkflowId) -> str:
        """The host's own id for the workflow, read as the platform. It stays
        with the workflow when it or its owner is renamed and never names
        another, so a name that has come to mean another workflow is told by
        it. `NotFound` when there is no such workflow.
        """
        ...
