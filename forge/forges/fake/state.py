"""What the in-memory forge holds: users, orgs and their roles, repositories
with their files at every version, their protected versions and who reaches
them, threads, runs and agents, plus the record of every call made through
the port and the knobs a test turns.
"""

import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from forge.domain.clock import Clock, SystemClock
from forge.domain.content import Change, ConflictToken, Files
from forge.domain.errors import Conflict, Forbidden, NotFound, Unavailable
from forge.domain.grading import RunState
from forge.domain.identity import AsUser, Credential, Identity, Platform, User
from forge.domain.ids import AgentId, RunId, ThreadId, VersionId
from forge.domain.roles import RANK, Role, Scope
from forge.domain.threads import Thread
from forge.forges.ids import PROTECTED_PREFIXES

CREDENTIAL_TTL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Call:
    """One call through the port: which operation, as whom, with what."""

    operation: str
    identity: Identity
    arguments: dict[str, Any]


@dataclass
class Repo:
    """One place at the fake forge. `scope` is the contest or task it belongs
    to, and `teams` the roles of which scopes reach it: an org's roles reach
    every place in the org, a contest's or a task's only the places they are
    attached to. `snapshots` holds every file at every version, and
    `touched` the paths each version wrote or removed. `reserved` is the
    protected versions' prefixes only the platform may create here, and
    `rewrites_refused` whether its history is kept from being rewritten.
    """

    owner: str
    name: str
    id: int = 0
    private: bool = True
    files: dict[str, bytes] = field(default_factory=dict)
    tokens: dict[str, ConflictToken] = field(default_factory=dict)
    history: list[Change] = field(default_factory=list)
    snapshots: dict[str, dict[str, bytes]] = field(default_factory=dict)
    touched: dict[str, set[str]] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    version_times: dict[str, datetime] = field(default_factory=dict)
    writers: set[int] = field(default_factory=set)
    readers: set[int] = field(default_factory=set)
    stars: set[int] = field(default_factory=set)
    marked: str | None = None
    scope: Scope | None = None
    teams: set[Scope] = field(default_factory=set)
    reserved: set[str] = field(default_factory=set)
    rewrites_refused: bool = False

    @property
    def head(self) -> str:
        return self.history[-1].version if self.history else ""


@dataclass
class StartedRun:
    """A run the fake CI was asked to start: the task it is of, the variables
    it was started with, when, whether it was cancelled, and where the CI
    has it, queued until a test says otherwise.
    """

    task: str
    variables: dict[str, str]
    at: datetime
    cancelled: bool = False
    ci_state: RunState = RunState.QUEUED


@dataclass
class Org:
    name: str
    description: str
    display_name: str | None = None
    roles: dict[tuple[Scope, Role], set[int]] = field(default_factory=dict)
    roles_ready: bool = False
    labels: set[str] = field(default_factory=set)
    account_members: set[int] = field(default_factory=set)
    event_push: tuple[str, str] | None = None


class State:
    """The whole forge, shared by every area of the fake."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()
        self.users: dict[int, User] = {}
        self.orgs: dict[str, Org] = {}
        self.repos: dict[tuple[str, str], Repo] = {}
        self.next_repo_id = 0
        self.threads: dict[ThreadId, Thread] = {}
        self.runs: dict[RunId, StartedRun] = {}
        self.ci_key = secrets.token_bytes(32)
        self.refuse_starts = 0
        self.lose_start_answer = False
        self.agents: dict[AgentId, tuple[str | None, str, str]] = {}
        self.published_at: dict[tuple[str, int], datetime] = {}
        self.calls: list[Call] = []
        self.codes: dict[str, tuple[int, str, str]] = {}
        self.credentials: dict[str, int] = {}
        self.refresh_tokens: dict[str, int] = {}
        self.passwords: dict[int, str] = {}
        self.tokens: dict[str, int] = {}
        self.ci_users: dict[str, int] = {}
        self.activated: set[str] = set()
        self.ci_tokens: dict[str, str] = {}
        self.revoked_ci_tokens: set[str] = set()
        self.unverified: set[str] = set()
        self.refreshes = 0
        self.refuse_refresh = False
        self.unavailable = False
        self.racing_submissions = 0
        self.lose_submission_answer = False
        self.signed_in_user_id = 0

    def record(self, operation: str, identity: Identity, **arguments: Any) -> None:
        self.calls.append(Call(operation, identity, arguments))

    def check_up(self) -> None:
        if self.unavailable:
            raise Unavailable("the fake forge is switched off")

    def user(self, user_id: int) -> User:
        try:
            return self.users[user_id]
        except KeyError:
            raise NotFound(f"no user with id {user_id}") from None

    def username(self, user_id: int) -> str:
        return self.user(user_id).username

    def user_named(self, username: str) -> User:
        for user in self.users.values():
            if user.username.lower() == username.lower():
                return user
        raise NotFound(f"no user named {username}")

    def new_user_id(self) -> int:
        return max(self.users, default=0) + 1

    def org(self, name: str) -> Org:
        try:
            return self.orgs[name]
        except KeyError:
            raise NotFound(f"no org {name}") from None

    def repo(self, owner: str, name: str) -> Repo:
        found = self.repos.get((owner, name))
        if found is not None:
            return found
        for (held, repo_name), repo in self.repos.items():
            if held.lower() == owner.lower() and repo_name.lower() == name.lower():
                return repo
        raise NotFound(f"no {name} under {owner}")

    def create_repo(
        self,
        as_: Identity,
        owner: str,
        name: str,
        files: Files,
        *,
        scope: Scope | None = None,
        marked: str | None = None,
    ) -> Repo:
        """A new repository, made only by the platform: the forge gives people
        no repositories of their own to create and no team the right to
        create one in an org, and refuses anyone else as `Forbidden`.
        """
        if not isinstance(as_, Platform):
            raise Forbidden(f"only the platform may create {owner}/{name}")
        if any(
            held.lower() == owner.lower() and repo_name.lower() == name.lower()
            for held, repo_name in self.repos
        ):
            raise Conflict(f"{owner}/{name} already exists")
        self.next_repo_id += 1
        repo = Repo(owner=owner, name=name, id=self.next_repo_id, scope=scope, marked=marked)
        self.repos[(owner, name)] = repo
        if files:
            self.commit(repo, files, "Create", None)
        return repo

    def commit(
        self,
        repo: Repo,
        files: Mapping[str, bytes | None],
        message: str,
        author_id: int | None,
    ) -> str:
        """One new version writing `files`, where none removes a file."""
        version = secrets.token_hex(20)
        for path, content in files.items():
            if content is None:
                repo.files.pop(path, None)
                repo.tokens.pop(path, None)
                continue
            repo.files[path] = content
            repo.tokens[path] = token_of(content)
        repo.snapshots[version] = dict(repo.files)
        repo.touched[version] = set(files)
        repo.history.append(
            Change(
                version=VersionId(version),
                author_id=author_id,
                message=message,
                at=self.clock.now(),
            )
        )
        return version

    def create_version(
        self, as_: Identity, repo: Repo, name: str, *, at: str | None = None, note: str = ""
    ) -> None:
        if name.startswith(PROTECTED_PREFIXES) and not isinstance(as_, Platform):
            raise Forbidden(f"only the platform may create {name}")
        if name in repo.versions:
            raise Conflict(f"{name} already exists")
        repo.versions[name] = at or repo.head
        repo.notes[name] = note
        repo.version_times[name] = self.clock.now()

    def version_files(self, repo: Repo, version: str) -> dict[str, bytes]:
        """Every file of the repository at one of its named versions."""
        if version not in repo.versions:
            raise NotFound(f"{repo.owner}/{repo.name} has no version {version}")
        return repo.snapshots.get(repo.versions[version], {})

    def files_at(self, repo: Repo, at: str | None) -> dict[str, bytes]:
        """Every file of the repository at a version, or at its head."""
        if at is None:
            return repo.files
        if at not in repo.snapshots:
            raise NotFound(f"{repo.owner}/{repo.name} has no version {at}")
        return repo.snapshots[at]

    def next_number(self, repo: Repo, prefix: str) -> int:
        return len([name for name in repo.versions if name.startswith(prefix)]) + 1

    def mint(self, user_id: int) -> Credential:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.credentials[access] = user_id
        self.refresh_tokens[refresh] = user_id
        return Credential(access, refresh, self.clock.now() + CREDENTIAL_TTL)

    def revoke_credentials(self, user_id: int) -> None:
        for access in [access for access, owner in self.credentials.items() if owner == user_id]:
            del self.credentials[access]
        for token in [token for token, owner in self.refresh_tokens.items() if owner == user_id]:
            del self.refresh_tokens[token]

    def author(self, as_: Identity) -> int | None:
        """The user a call is made as, checked against the credential it
        carries, or none for a service identity.
        """
        if isinstance(as_, AsUser):
            if self.credentials.get(as_.credential.access) != as_.user_id:
                raise Forbidden("the credential does not belong to this user")
            return as_.user_id
        return None

    def require_read(self, as_: Identity, repo: Repo) -> None:
        user_id = self.author(as_)
        if user_id is None or self.may_read(user_id, repo):
            return
        raise Forbidden(f"{self.username(user_id)} may not read {repo.owner}/{repo.name}")

    def require_write(self, as_: Identity, repo: Repo) -> None:
        user_id = self.author(as_)
        if user_id is None or self.may_write(user_id, repo):
            return
        raise Forbidden(f"{self.username(user_id)} may not write {repo.owner}/{repo.name}")

    def may_read(self, user_id: int, repo: Repo) -> bool:
        return (
            not repo.private
            or user_id in repo.readers
            or self.may_write(user_id, repo)
            or self.holds(user_id, repo, Role.OBSERVER)
        )

    def may_write(self, user_id: int, repo: Repo) -> bool:
        return (
            user_id in repo.writers
            or repo.owner == self.username(user_id)
            or self.holds(user_id, repo, Role.MANAGER)
        )

    def holds(self, user_id: int, repo: Repo, role: Role) -> bool:
        """Whether the user holds `role` in a way that reaches the repository:
        at its org, or at a contest or task whose roles are attached to it.
        """
        if repo.scope is None or repo.owner not in self.orgs:
            return False
        return any(
            scope.covers(repo.scope)
            and (scope.contest is None or scope in repo.teams)
            and RANK[held] >= RANK[role]
            and user_id in members
            for (scope, held), members in self.orgs[repo.owner].roles.items()
        )


def token_of(content: bytes) -> ConflictToken:
    """The token a file is read with: the same for the same content."""
    return ConflictToken(hashlib.sha1(content).hexdigest())
