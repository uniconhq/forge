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

from forge.domain.content import ConflictToken, File, Files
from forge.domain.identity import Identity, User
from forge.domain.ids import VersionId, WorkflowId
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

    async def workflow_readers(self, as_: Identity, workflow: WorkflowId) -> tuple[User, ...]:
        """The users the workflow is shared with, read as the platform once
        the host says `as_` may write it; `Forbidden` otherwise.
        """
        ...

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str, at: VersionId | None = None
    ) -> None:
        """Name the state `at`, a commit of the workflow, or its head when
        none is given, as a version, as `as_`. `Conflict` when the name is
        taken; `Forbidden` for a name reserved for protected versions or for
        an identity that may not write the workflow.
        """
        ...

    async def read_workflow_draft(
        self, as_: Identity, workflow: WorkflowId, path: str
    ) -> tuple[VersionId, File]:
        """One file as the workflow holds it now, at the head of its main
        line, and the commit that head is. `NotFound` when there is no such
        file; `Forbidden` when the identity may not read the workflow.
        """
        ...

    async def write_workflow_file(
        self,
        as_: Identity,
        workflow: WorkflowId,
        path: str,
        content: bytes,
        *,
        expected: ConflictToken | None,
        message: str,
    ) -> File:
        """Write one file on the main line as `as_` and return it as written,
        with its new token. `expected` is the token it was read with, or
        none to create it; `Conflict` when it has changed since, or is there
        when none was expected; `Forbidden` when the identity
        may not write the workflow.
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

    async def describe_workflow(self, as_: Identity, workflow: WorkflowId) -> Workflow:
        """The workflow with its visibility, stars and versions, for an
        identity that may read it. `NotFound` when there is none it may read,
        whether there is none or it is not shared with them.
        """
        ...

    async def workflows_readable_by(self, as_: Identity) -> tuple[Workflow, ...]:
        """Every workflow the identity may read: its own, its orgs', those
        shared with it and the public ones.
        """
        ...

    async def workflow_key(self, workflow: WorkflowId) -> str:
        """The host's own id for the workflow, read as the platform. It stays
        with the workflow when it or its owner is renamed and never names
        another, so a name that has come to mean another workflow is told by
        it. `NotFound` when there is no such workflow.
        """
        ...
