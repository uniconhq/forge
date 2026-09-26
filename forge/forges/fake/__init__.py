"""The port in memory, for tests. It implements every operation with no
network, records every call with the identity it was made under, and refuses
the three things a real forge refuses: a write whose conflict check is stale,
a protected version created by anything but the platform, and a read the user
has no access to.
"""

import base64
import hashlib
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from forge.domain.content import Change, EntryKind, File, Files, TreeEntry
from forge.domain.errors import Conflict, Forbidden, NotFound, Unavailable
from forge.domain.grading import Enrolment, Run, RunStatus
from forge.domain.identity import (
    PLATFORM,
    AsOrgAccount,
    AsUser,
    Credential,
    Identity,
    Platform,
    User,
)
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
from forge.domain.names import WorkspaceOwner, owner_from_segment
from forge.domain.roles import RANK, Role, RoleGrant, Scope
from forge.domain.threads import Comment, Thread, ThreadKind, ThreadPlace
from forge.domain.workflows import Primitive, Visibility, Workflow
from forge.port import ContentPlace, SignedIn

CREDENTIAL_TTL = timedelta(hours=1)
PLATFORM_ORG = "unicon"
PUBLISHED_PREFIX = "published/"
SUBMISSION_PREFIX = "submission/"
PROTECTED_PREFIXES = (PUBLISHED_PREFIX, SUBMISSION_PREFIX)
WORKSPACE_MARK = "@"


@dataclass(frozen=True, slots=True)
class Call:
    """One call through the port: which operation, as whom, with what."""

    operation: str
    identity: Identity
    arguments: dict[str, Any]


@dataclass
class Repo:
    owner: str
    name: str
    private: bool = True
    files: dict[str, bytes] = field(default_factory=dict)
    file_versions: dict[str, str] = field(default_factory=dict)
    history: list[Change] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    writers: set[int] = field(default_factory=set)
    readers: set[int] = field(default_factory=set)
    stars: set[int] = field(default_factory=set)
    marked: str | None = None
    scope: Scope | None = None

    @property
    def head(self) -> str:
        return self.history[-1].version if self.history else ""


@dataclass
class Org:
    name: str
    description: str
    roles: dict[tuple[Scope, Role], set[int]] = field(default_factory=dict)


class FakeForge:
    def __init__(
        self, *, public_url: str = "http://forge.test", sign_in_redirect_uri: str = ""
    ) -> None:
        self._public_url = public_url
        self._redirect_uri = sign_in_redirect_uri
        self.users: dict[int, User] = {}
        self.orgs: dict[str, Org] = {}
        self.repos: dict[tuple[str, str], Repo] = {}
        self.threads: dict[ThreadId, Thread] = {}
        self.runs: dict[RunId, Run] = {}
        self.agents: dict[AgentId, tuple[str | None, str, str]] = {}
        self.calls: list[Call] = []
        self.codes: dict[str, tuple[int, str, str]] = {}
        self.credentials: dict[str, int] = {}
        self.refresh_tokens: dict[str, int] = {}
        self.refreshes = 0
        self.refuse_refresh = False
        self.unavailable = False
        self.signed_in_user_id = 0

    @property
    def name(self) -> str:
        return "fake"

    def add_user(self, user_id: int, username: str, **fields: Any) -> User:
        user = User(id=user_id, username=username, **fields)
        self.users[user_id] = user
        return user

    def calls_to(self, operation: str) -> list[Call]:
        return [call for call in self.calls if call.operation == operation]

    def reset_calls(self) -> None:
        self.calls.clear()

    def authorize(self, code_challenge: str, nonce: str) -> str:
        """What the browser does on the consent page: the signed-in user
        approves and a code is issued for this attempt.
        """
        code = secrets.token_urlsafe(16)
        self.codes[code] = (self.signed_in_user_id, code_challenge, nonce)
        return code

    def consent_redirect(self, sign_in_url: str) -> str:
        """Follow the sign-in URL the way a browser would and return where the
        forge sends it back to.
        """
        query = parse_qs(urlsplit(sign_in_url).query)
        code = self.authorize(query["code_challenge"][0], query["nonce"][0])
        return f"{self._redirect_uri}?{urlencode({'code': code, 'state': query['state'][0]})}"

    def mint(self, user_id: int) -> Credential:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.credentials[access] = user_id
        self.refresh_tokens[refresh] = user_id
        return Credential(access, refresh, datetime.now(UTC) + CREDENTIAL_TTL)

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        query = urlencode(
            {
                "state": state,
                "code_challenge": code_challenge,
                "nonce": nonce,
                "redirect_uri": self._redirect_uri,
            }
        )
        return f"{self._public_url}/login/oauth/authorize?{query}"

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        self._record("complete_sign_in", PLATFORM, code=code)
        self._check_up()
        entry = self.codes.pop(code, None)
        if entry is None:
            raise Forbidden("the code is not accepted")
        user_id, challenge, nonce = entry
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        if base64.urlsafe_b64encode(digest).decode().rstrip("=") != challenge:
            raise Forbidden("the verifier does not match")
        return SignedIn(user=self._user(user_id), credential=self.mint(user_id), nonce=nonce)

    async def refresh_credential(self, credential: Credential) -> Credential:
        self._record("refresh_credential", PLATFORM)
        self._check_up()
        if self.refuse_refresh:
            raise Forbidden("the forge will not renew this credential")
        user_id = self.refresh_tokens.pop(credential.refresh, None)
        if user_id is None:
            raise Forbidden("unknown refresh token")
        self.credentials.pop(credential.access, None)
        self.refreshes += 1
        return self.mint(user_id)

    async def user_of(self, credential: Credential) -> User:
        self._record("user_of", PLATFORM)
        self._check_up()
        user_id = self.credentials.get(credential.access)
        if user_id is None:
            raise Forbidden("unknown credential")
        return self._user(user_id)

    async def find_user(self, user_id: int) -> User:
        self._record("find_user", PLATFORM, user_id=user_id)
        self._check_up()
        return self._user(user_id)

    async def deactivate_user(self, user_id: int) -> None:
        self._record("deactivate_user", PLATFORM, user_id=user_id)
        self._check_up()
        user = self._user(user_id)
        self.users[user_id] = User(
            id=user.id,
            username=user.username,
            name=user.name,
            email=user.email,
            avatar_url=user.avatar_url,
            active=False,
        )
        self._revoke_credentials(user_id)

    async def delete_user(self, user_id: int) -> None:
        self._record("delete_user", PLATFORM, user_id=user_id)
        self._check_up()
        username = self._username(user_id)
        del self.users[user_id]
        self._revoke_credentials(user_id)
        for org in self.orgs.values():
            for members in org.roles.values():
                members.discard(user_id)
        for key in [key for key, repo in self.repos.items() if repo.owner == username]:
            del self.repos[key]

    async def create_org(self, name: OrgName, *, description: str) -> None:
        self._record("create_org", PLATFORM, name=name)
        self._check_up()
        if name in self.orgs:
            raise Conflict(f"org {name} already exists")
        self.orgs[name] = Org(name, description)

    async def update_org(self, name: OrgName, *, description: str) -> None:
        self._record("update_org", PLATFORM, name=name)
        self._org(name).description = description

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        self._record("grant_role", PLATFORM, user_id=user_id, scope=scope, role=role)
        self._user(user_id)
        self._org(scope.org).roles.setdefault((scope, role), set()).add(user_id)

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        self._record("revoke_role", PLATFORM, user_id=user_id, scope=scope, role=role)
        self._org(scope.org).roles.get((scope, role), set()).discard(user_id)

    async def roles_of(self, user_id: int) -> tuple[RoleGrant, ...]:
        self._record("roles_of", PLATFORM, user_id=user_id)
        self._check_up()
        return tuple(
            RoleGrant(scope, role)
            for org in self.orgs.values()
            for (scope, role), members in org.roles.items()
            if user_id in members
        )

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        self._record("holders_of", PLATFORM, scope=scope, role=role)
        members = self._org(scope.org).roles.get((scope, role), set())
        return tuple(self._user(member) for member in sorted(members))

    async def create_contest(self, as_: Identity, org: str, name: str, files: Files) -> ContestId:
        self._record("create_contest", as_, org=org, name=name)
        self._org(org)
        repo = self._create_repo(org, f"{name}.contest", files, scope=Scope(org, name))
        return ContestId(f"{repo.owner}/{name}")

    async def create_task(
        self, as_: Identity, contest: ContestId, name: str, files: Files
    ) -> TaskId:
        self._record("create_task", as_, contest=contest, name=name)
        org, contest_name = contest.split("/")
        self._create_repo(
            org, f"{contest_name}.{name}.task", files, scope=Scope(org, contest_name, name)
        )
        return TaskId(f"{contest}/{name}")

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, version: VersionId | None = None
    ) -> File:
        self._record("read_file", as_, place=place, path=path)
        repo = self._content_repo(place)
        self._require_read(as_, repo)
        if path not in repo.files:
            raise NotFound(f"{path} is not in {place}")
        return File(
            path=path, content=repo.files[path], version=VersionId(repo.file_versions[path])
        )

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
        self._record("write_file", as_, place=place, path=path, expected_version=expected_version)
        repo = self._content_repo(place)
        self._require_write(as_, repo)
        current = repo.file_versions.get(path)
        if current is not None and expected_version != current:
            raise Conflict(f"{path} has changed since version {expected_version}")
        if current is None and expected_version is not None:
            raise Conflict(f"{path} does not exist yet")
        return VersionId(self._commit(repo, {path: content}, message, self._author(as_)))

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        self._record("list_tree", as_, place=place, path=path)
        repo = self._content_repo(place)
        self._require_read(as_, repo)
        prefix = f"{path.rstrip('/')}/" if path else ""
        seen: dict[str, TreeEntry] = {}
        for file_path in sorted(repo.files):
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :]
            if "/" in rest:
                directory = prefix + rest.split("/", 1)[0]
                seen.setdefault(directory, TreeEntry(directory, EntryKind.DIRECTORY))
            else:
                seen[file_path] = TreeEntry(file_path, EntryKind.FILE, len(repo.files[file_path]))
        return tuple(seen.values())

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]:
        self._record("history", as_, place=place, path=path)
        repo = self._content_repo(place)
        self._require_read(as_, repo)
        return tuple(reversed(repo.history))

    async def open_workspace(
        self,
        contest: ContestId,
        owner: WorkspaceOwner,
        member_ids: Sequence[int],
        tasks: Sequence[TaskId],
    ) -> WorkspaceId:
        self._record(
            "open_workspace", PLATFORM, contest=contest, owner=owner, member_ids=list(member_ids)
        )
        org, contest_name = contest.split("/")
        desk = self._create_repo(org, f"{owner.segment}.desk", {})
        desk.writers.update(member_ids)
        for task in tasks:
            task_name = task.split("/")[2]
            sub = self._create_repo(org, f"{contest_name}.{task_name}.{owner.segment}.sub", {})
            sub.writers.update(member_ids)
        return WorkspaceId(f"{contest}/{WORKSPACE_MARK}{owner.segment}")

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        self._record("close_workspace", PLATFORM, workspace=workspace, member_ids=list(member_ids))
        for repo in self._workspace_repos(workspace):
            repo.writers.difference_update(member_ids)

    async def list_submissions(
        self, workspace: WorkspaceId, task: TaskId
    ) -> tuple[SubmissionId, ...]:
        self._record("list_submissions", PLATFORM, workspace=workspace, task=task)
        repo = self._submission_repo(workspace, task)
        numbers = sorted(
            int(name.removeprefix(SUBMISSION_PREFIX))
            for name in repo.versions
            if name.startswith(SUBMISSION_PREFIX)
        )
        task_name = task.split("/")[2]
        return tuple(SubmissionId(f"{workspace}/{task_name}#{number}") for number in numbers)

    async def record_submission(
        self, workspace: WorkspaceId, task: TaskId, files: Files, *, submitter_id: int
    ) -> SubmissionId:
        self._record(
            "record_submission", PLATFORM, workspace=workspace, task=task, submitter_id=submitter_id
        )
        repo = self._submission_repo(workspace, task)
        self._commit(repo, files, "Submit", submitter_id)
        number = self._next_number(repo, SUBMISSION_PREFIX)
        self._create_version(PLATFORM, repo, f"{SUBMISSION_PREFIX}{number}")
        return SubmissionId(f"{workspace}/{task.split('/')[2]}#{number}")

    async def publish(self, task: TaskId, files: Files) -> PublicationId:
        self._record("publish", PLATFORM, task=task)
        repo = self._task_repo(task)
        self._commit(repo, files, "Publish", None)
        number = self._next_number(repo, PUBLISHED_PREFIX)
        self._create_version(PLATFORM, repo, f"{PUBLISHED_PREFIX}{number}")
        return PublicationId(f"{task}#{number}")

    async def list_publications(self, task: TaskId) -> tuple[PublicationId, ...]:
        self._record("list_publications", PLATFORM, task=task)
        repo = self._task_repo(task)
        numbers = sorted(
            int(name.removeprefix(PUBLISHED_PREFIX))
            for name in repo.versions
            if name.startswith(PUBLISHED_PREFIX)
        )
        return tuple(PublicationId(f"{task}#{number}") for number in numbers)

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId:
        self._record("post_thread", as_, place=place, kind=kind)
        repo = self._thread_repo(place)
        self._require_read(as_, repo)
        thread_id = ThreadId(f"{repo.owner}/{repo.name}#{len(self.threads) + 1}")
        self.threads[thread_id] = Thread(
            id=thread_id,
            kind=kind,
            title=title,
            body=body,
            author_id=self._author(as_),
            created_at=datetime.now(UTC),
            closed=False,
            answered=False,
        )
        return thread_id

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind
    ) -> tuple[Thread, ...]:
        self._record("list_threads", as_, place=place, kind=kind)
        repo = self._thread_repo(place)
        self._require_read(as_, repo)
        prefix = f"{repo.owner}/{repo.name}#"
        return tuple(
            thread
            for thread_id, thread in self.threads.items()
            if thread_id.startswith(prefix) and thread.kind is kind
        )

    async def edit_thread(self, as_: Identity, thread: ThreadId, *, title: str, body: str) -> None:
        self._record("edit_thread", as_, thread=thread)
        found = self._thread(as_, thread)
        self.threads[thread] = Thread(
            id=found.id,
            kind=found.kind,
            title=title,
            body=body,
            author_id=found.author_id,
            created_at=found.created_at,
            closed=found.closed,
            answered=found.answered,
            comments=found.comments,
        )

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None:
        self._record("close_thread", as_, thread=thread)
        found = self._thread(as_, thread)
        self.threads[thread] = _with(found, closed=True)

    async def comment(
        self, as_: Identity, thread: ThreadId, body: str, *, answered: bool = False
    ) -> None:
        self._record("comment", as_, thread=thread, answered=answered)
        found = self._thread(as_, thread)
        comment = Comment(
            id=str(len(found.comments) + 1),
            author_id=self._author(as_),
            body=body,
            at=datetime.now(UTC),
        )
        self.threads[thread] = _with(
            found,
            comments=(*found.comments, comment),
            answered=found.answered or answered,
            closed=found.closed or answered,
        )

    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId:
        self._record("create_workflow", as_, owner=owner, name=name, visibility=visibility)
        repo = self._create_repo(owner, f"{name}.workflow", files, marked="workflow")
        repo.private = visibility is not Visibility.PUBLIC
        author = self._author(as_)
        if author is not None:
            repo.writers.add(author)
        return WorkflowId(f"{owner}/{name}")

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None:
        self._record("set_workflow_visibility", as_, workflow=workflow, visibility=visibility)
        repo = self._workflow_repo(workflow)
        self._require_write(as_, repo)
        repo.private = visibility is not Visibility.PUBLIC

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        self._record("share_workflow", as_, workflow=workflow, user_id=user_id)
        repo = self._workflow_repo(workflow)
        self._require_write(as_, repo)
        repo.readers.add(user_id)

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        self._record("unshare_workflow", as_, workflow=workflow, user_id=user_id)
        repo = self._workflow_repo(workflow)
        self._require_write(as_, repo)
        repo.readers.discard(user_id)

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str
    ) -> None:
        self._record("create_workflow_version", as_, workflow=workflow, version=version)
        repo = self._workflow_repo(workflow)
        self._require_write(as_, repo)
        self._create_version(as_, repo, version)

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File:
        self._record("read_workflow_file", as_, workflow=workflow, version=version, path=path)
        repo = self._workflow_repo(workflow)
        self._require_read(as_, repo)
        if version not in repo.versions or path not in repo.files:
            raise NotFound(f"{path} at {version} is not in {workflow}")
        return File(path=path, content=repo.files[path], version=VersionId(repo.versions[version]))

    async def search_public_workflows(self, query: str) -> tuple[Workflow, ...]:
        self._record("search_public_workflows", PLATFORM, query=query)
        return tuple(
            self._workflow(repo)
            for repo in self.repos.values()
            if repo.marked == "workflow" and not repo.private and query in repo.name
        )

    async def star_workflow(self, as_: Identity, workflow: WorkflowId) -> None:
        self._record("star_workflow", as_, workflow=workflow)
        repo = self._workflow_repo(workflow)
        self._require_read(as_, repo)
        author = self._author(as_)
        if author is not None:
            repo.stars.add(author)

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        self._record("copy_workflow", as_, source=source, version=version, owner=owner, name=name)
        origin = self._workflow_repo(source)
        self._require_read(as_, origin)
        if version not in origin.versions:
            raise NotFound(f"{source} has no version {version}")
        return await self.create_workflow(as_, owner, name, dict(origin.files), Visibility.PRIVATE)

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]:
        self._record("workflows_owned_by", PLATFORM, user_id=user_id)
        username = self._username(user_id)
        return tuple(
            self._workflow(repo)
            for repo in self.repos.values()
            if repo.marked == "workflow" and repo.owner == username
        )

    async def list_primitives(self) -> tuple[Primitive, ...]:
        self._record("list_primitives", PLATFORM)
        return tuple(
            Primitive(
                id=PrimitiveId(repo.name.removesuffix(".primitive")),
                name=repo.name.removesuffix(".primitive"),
                versions=tuple(repo.versions),
            )
            for repo in self.repos.values()
            if repo.marked == "primitive"
        )

    async def read_primitive_declaration(self, primitive: PrimitiveId, version: str) -> bytes:
        self._record("read_primitive_declaration", PLATFORM, primitive=primitive, version=version)
        repo = self._repo(PLATFORM_ORG, f"{primitive}.primitive")
        if version not in repo.versions or "primitive.yaml" not in repo.files:
            raise NotFound(f"{primitive} has no declaration at {version}")
        return repo.files["primitive.yaml"]

    async def register_for_grading(self, task: TaskId) -> None:
        self._record("register_for_grading", AsOrgAccount(task.split("/")[0]), task=task)
        self._task_repo(task)

    async def start_run(
        self, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        org = task.split("/")[0]
        self._record(
            "start_run",
            AsOrgAccount(org),
            task=task,
            variables=dict(variables),
            compute_label=compute_label,
        )
        self._check_up()
        self._task_repo(task)
        run = Run(id=RunId(f"{task}/{len(self.runs) + 1}"), status=RunStatus.PENDING)
        self.runs[run.id] = run
        return run.id

    async def read_run(self, run: RunId) -> Run:
        self._record("read_run", PLATFORM, run=run)
        if run not in self.runs:
            raise NotFound(f"no run {run}")
        return self.runs[run]

    async def cancel_run(self, run: RunId) -> None:
        self._record("cancel_run", PLATFORM, run=run)
        if run not in self.runs:
            raise NotFound(f"no run {run}")
        self.runs[run] = Run(id=run, status=RunStatus.CANCELLED)

    async def enrol_agent(self, org: OrgName | None, label: str) -> Enrolment:
        self._record("enrol_agent", PLATFORM, org=org, label=label)
        agent = AgentId(str(len(self.agents) + 1))
        token = secrets.token_urlsafe(16)
        self.agents[agent] = (org, label, token)
        return Enrolment(agent=agent, token=token)

    async def revoke_agent(self, agent: AgentId) -> None:
        self._record("revoke_agent", PLATFORM, agent=agent)
        if agent not in self.agents:
            raise NotFound(f"no agent {agent}")
        del self.agents[agent]

    def add_primitive(self, name: str, versions: Mapping[str, bytes]) -> None:
        """Seed a primitive the way bootstrap does, one declaration per version."""
        repo = self._create_repo(PLATFORM_ORG, f"{name}.primitive", {}, marked="primitive")
        repo.private = False
        for version, declaration in versions.items():
            self._commit(repo, {"primitive.yaml": declaration}, version, None)
            self._create_version(PLATFORM, repo, version)

    def _record(self, operation: str, identity: Identity, **arguments: Any) -> None:
        self.calls.append(Call(operation, identity, arguments))

    def _check_up(self) -> None:
        if self.unavailable:
            raise Unavailable("the fake forge is switched off")

    def _user(self, user_id: int) -> User:
        try:
            return self.users[user_id]
        except KeyError:
            raise NotFound(f"no user with id {user_id}") from None

    def _username(self, user_id: int) -> str:
        return self._user(user_id).username

    def _org(self, name: str) -> Org:
        try:
            return self.orgs[name]
        except KeyError:
            raise NotFound(f"no org {name}") from None

    def _repo(self, owner: str, name: str) -> Repo:
        try:
            return self.repos[(owner, name)]
        except KeyError:
            raise NotFound(f"no {name} under {owner}") from None

    def _create_repo(
        self,
        owner: str,
        name: str,
        files: Files,
        *,
        scope: Scope | None = None,
        marked: str | None = None,
    ) -> Repo:
        if (owner, name) in self.repos:
            raise Conflict(f"{owner}/{name} already exists")
        repo = Repo(owner=owner, name=name, scope=scope, marked=marked)
        self.repos[(owner, name)] = repo
        if files:
            self._commit(repo, files, "Create", None)
        return repo

    def _commit(self, repo: Repo, files: Files, message: str, author_id: int | None) -> str:
        version = secrets.token_hex(20)
        for path, content in files.items():
            repo.files[path] = content
            repo.file_versions[path] = hashlib.sha1(content).hexdigest()
        repo.history.append(
            Change(
                version=VersionId(version),
                author_id=author_id,
                message=message,
                at=datetime.now(UTC),
            )
        )
        return version

    def _create_version(self, as_: Identity, repo: Repo, name: str) -> None:
        if name.startswith(PROTECTED_PREFIXES) and not isinstance(as_, Platform):
            raise Forbidden(f"only the platform may create {name}")
        if name in repo.versions:
            raise Conflict(f"{name} already exists")
        repo.versions[name] = repo.head

    def _next_number(self, repo: Repo, prefix: str) -> int:
        return len([name for name in repo.versions if name.startswith(prefix)]) + 1

    def _content_repo(self, place: ContentPlace) -> Repo:
        parts = place.split("/")
        if len(parts) == 2:
            return self._repo(parts[0], f"{parts[1]}.contest")
        return self._task_repo(TaskId(place))

    def _task_repo(self, task: TaskId) -> Repo:
        org, contest, name = task.split("/")
        return self._repo(org, f"{contest}.{name}.task")

    def _submission_repo(self, workspace: WorkspaceId, task: TaskId) -> Repo:
        org, contest, owner = workspace.split("/")
        return self._repo(
            org, f"{contest}.{task.split('/')[2]}.{owner.removeprefix(WORKSPACE_MARK)}.sub"
        )

    def _workspace_repos(self, workspace: WorkspaceId) -> list[Repo]:
        org, contest, owner = workspace.split("/")
        segment = owner.removeprefix(WORKSPACE_MARK)
        return [
            repo
            for (repo_owner, name), repo in self.repos.items()
            if repo_owner == org
            and (
                name == f"{segment}.desk"
                or (name.startswith(f"{contest}.") and name.endswith(f".{segment}.sub"))
            )
        ]

    def _thread_repo(self, place: ThreadPlace) -> Repo:
        parts = place.split("/")
        if len(parts) == 3 and parts[2].startswith(WORKSPACE_MARK):
            segment = parts[2].removeprefix(WORKSPACE_MARK)
            owner_from_segment(segment)
            return self._repo(parts[0], f"{segment}.desk")
        if len(parts) == 2:
            return self._content_repo(ContestId(place))
        return self._content_repo(TaskId(place))

    def _workflow_repo(self, workflow: WorkflowId) -> Repo:
        owner, name = workflow.split("/")
        return self._repo(owner, f"{name}.workflow")

    def _workflow(self, repo: Repo) -> Workflow:
        if not repo.private:
            visibility = Visibility.PUBLIC
        elif repo.readers:
            visibility = Visibility.SHARED
        else:
            visibility = Visibility.PRIVATE
        name = repo.name.removesuffix(".workflow")
        return Workflow(
            id=WorkflowId(f"{repo.owner}/{name}"),
            owner=repo.owner,
            name=name,
            visibility=visibility,
            stars=len(repo.stars),
            versions=tuple(repo.versions),
        )

    def _thread(self, as_: Identity, thread: ThreadId) -> Thread:
        found = self.threads.get(thread)
        if found is None:
            raise NotFound(f"no thread {thread}")
        owner, name = thread.rsplit("#", 1)[0].split("/")
        self._require_read(as_, self._repo(owner, name))
        return found

    def _author(self, as_: Identity) -> int | None:
        if isinstance(as_, AsUser):
            if self.credentials.get(as_.credential.access) != as_.user_id:
                raise Forbidden("the credential does not belong to this user")
            return as_.user_id
        return None

    def _require_read(self, as_: Identity, repo: Repo) -> None:
        if not isinstance(as_, AsUser):
            return
        user_id = self._author(as_)
        if user_id is None or self._may_read(user_id, repo):
            return
        raise Forbidden(f"{self._username(user_id)} may not read {repo.owner}/{repo.name}")

    def _require_write(self, as_: Identity, repo: Repo) -> None:
        if not isinstance(as_, AsUser):
            return
        user_id = self._author(as_)
        if user_id is None or self._may_write(user_id, repo):
            return
        raise Forbidden(f"{self._username(user_id)} may not write {repo.owner}/{repo.name}")

    def _may_read(self, user_id: int, repo: Repo) -> bool:
        return (
            not repo.private
            or user_id in repo.readers
            or self._may_write(user_id, repo)
            or self._holds(user_id, repo, Role.OBSERVER)
        )

    def _may_write(self, user_id: int, repo: Repo) -> bool:
        return (
            user_id in repo.writers
            or repo.owner == self._username(user_id)
            or self._holds(user_id, repo, Role.MANAGER)
        )

    def _holds(self, user_id: int, repo: Repo, role: Role) -> bool:
        if repo.scope is None or repo.owner not in self.orgs:
            return False
        return any(
            scope.covers(repo.scope) and RANK[held] >= RANK[role] and user_id in members
            for (scope, held), members in self.orgs[repo.owner].roles.items()
        )

    def _revoke_credentials(self, user_id: int) -> None:
        for access in [access for access, owner in self.credentials.items() if owner == user_id]:
            del self.credentials[access]
        for refresh in [token for token, owner in self.refresh_tokens.items() if owner == user_id]:
            del self.refresh_tokens[refresh]


def _with(thread: Thread, **changes: Any) -> Thread:
    values: dict[str, Any] = {
        "id": thread.id,
        "kind": thread.kind,
        "title": thread.title,
        "body": thread.body,
        "author_id": thread.author_id,
        "created_at": thread.created_at,
        "closed": thread.closed,
        "answered": thread.answered,
        "comments": thread.comments,
    }
    values.update(changes)
    return Thread(**values)
