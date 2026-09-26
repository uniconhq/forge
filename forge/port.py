"""The forge port: the one interface between this package and the git host
behind it, in the platform's own words. `forges.forgejo` and `forges.fake`
implement it.

Two rules hold it together. Every reference the package stores is an opaque id
the port hands out. Every failure is one of five typed errors: `NotFound`,
`Forbidden`, `Conflict`, `Rejected` and `Unavailable`. Operations done for a
person take the identity the call is made under, so the host records the
change as theirs and enforces their permissions underneath the platform's own.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from forge.domain.content import Change, File, Files, TreeEntry
from forge.domain.grading import Enrolment, Run
from forge.domain.identity import Credential, Identity, User
from forge.domain.ids import (
    AgentId,
    ContestId,
    OrgName,
    PrimitiveId,
    PublicationId,
    RunId,
    SubmissionId,
    TaskId,
    ThreadId,
    VersionId,
    WorkflowId,
    WorkspaceId,
)
from forge.domain.names import WorkspaceOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.threads import Thread, ThreadKind, ThreadPlace
from forge.domain.workflows import Primitive, Visibility, Workflow

ContentPlace = ContestId | TaskId


@dataclass(frozen=True, slots=True)
class SignedIn:
    """What completing a sign-in yields: who signed in, the credential to act
    as them, and the nonce the identity answer carried.
    """

    user: User
    credential: Credential
    nonce: str | None


@runtime_checkable
class Forge(Protocol):
    """A git host as the platform sees it."""

    @property
    def name(self) -> str:
        """Which implementation this is, for logs."""
        ...

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        """Where to send a browser to sign in, carrying the checks for the answer."""
        ...

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        """Exchange the code the host sent back and read who signed in."""
        ...

    async def refresh_credential(self, credential: Credential) -> Credential:
        """A fresh credential for the same user. `Forbidden` once the host will
        no longer renew it.
        """
        ...

    async def user_of(self, credential: Credential) -> User: ...

    async def find_user(self, user_id: int) -> User: ...

    async def deactivate_user(self, user_id: int) -> None:
        """Stop the user signing in, reversibly, without removing anything."""
        ...

    async def delete_user(self, user_id: int) -> None:
        """Remove the user and whatever they still own."""
        ...

    async def create_org(self, name: OrgName, *, description: str) -> None:
        """Make an org with its three roles and its org account."""
        ...

    async def update_org(self, name: OrgName, *, description: str) -> None: ...

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None: ...

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None: ...

    async def roles_of(self, user_id: int) -> tuple[RoleGrant, ...]:
        """Every role the user holds directly, at every scope."""
        ...

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]: ...

    async def create_contest(
        self, as_: Identity, org: OrgName, name: str, files: Files
    ) -> ContestId: ...

    async def create_task(
        self, as_: Identity, contest: ContestId, name: str, files: Files
    ) -> TaskId: ...

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, version: VersionId | None = None
    ) -> File: ...

    async def write_file(
        self,
        as_: Identity,
        place: ContentPlace,
        path: str,
        content: bytes,
        *,
        message: str,
        expected_version: VersionId | None,
    ) -> VersionId:
        """Write one file. `expected_version` is the version the write started
        from; `Conflict` when the file has moved since.
        """
        ...

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]: ...

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]: ...

    async def open_workspace(
        self,
        contest: ContestId,
        owner: WorkspaceOwner,
        member_ids: Sequence[int],
        tasks: Sequence[TaskId],
    ) -> WorkspaceId:
        """Make the place a contestant works for one contest and give the
        members write access to it.
        """
        ...

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        """Take the members' access away and keep the contents."""
        ...

    async def list_submissions(
        self, workspace: WorkspaceId, task: TaskId
    ) -> tuple[SubmissionId, ...]: ...

    async def record_submission(
        self, workspace: WorkspaceId, task: TaskId, files: Files, *, submitter_id: int
    ) -> SubmissionId:
        """Record the files as a protected version that only the platform
        may create.
        """
        ...

    async def publish(self, task: TaskId, files: Files) -> PublicationId:
        """Write the compiled plans, create the protected version, and register
        the task for grading as the org account.
        """
        ...

    async def list_publications(self, task: TaskId) -> tuple[PublicationId, ...]: ...

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId: ...

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind
    ) -> tuple[Thread, ...]: ...

    async def edit_thread(
        self, as_: Identity, thread: ThreadId, *, title: str, body: str
    ) -> None: ...

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None: ...

    async def comment(
        self, as_: Identity, thread: ThreadId, body: str, *, answered: bool = False
    ) -> None: ...

    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId: ...

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None: ...

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None: ...

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None: ...

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str
    ) -> None: ...

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File: ...

    async def search_public_workflows(self, query: str) -> tuple[Workflow, ...]: ...

    async def star_workflow(self, as_: Identity, workflow: WorkflowId) -> None: ...

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        """A new workflow owned by `owner`, made from the source at `version`."""
        ...

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]: ...

    async def list_primitives(self) -> tuple[Primitive, ...]: ...

    async def read_primitive_declaration(self, primitive: PrimitiveId, version: str) -> bytes: ...

    async def register_for_grading(self, task: TaskId) -> None:
        """Make the task known to the CI, once, as the org account."""
        ...

    async def start_run(
        self, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        """Start a grading run as the org account, pinned to one compute."""
        ...

    async def read_run(self, run: RunId) -> Run: ...

    async def cancel_run(self, run: RunId) -> None: ...

    async def enrol_agent(self, org: OrgName | None, label: str) -> Enrolment:
        """Enrol a machine as an agent of one org, or of the platform when
        `org` is none.
        """
        ...

    async def revoke_agent(self, agent: AgentId) -> None: ...
