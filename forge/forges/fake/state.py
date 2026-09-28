"""What the in-memory forge holds: users, orgs and their roles, repositories
with their files, versions and access, threads, runs and agents, plus the
record of every call made through the port and the knobs a test turns.
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from forge.domain.clock import Clock, SystemClock
from forge.domain.content import Change, ConflictToken, Files
from forge.domain.errors import Conflict, Forbidden, NotFound, Unavailable
from forge.domain.grading import Run
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
    owner: str
    name: str
    private: bool = True
    files: dict[str, bytes] = field(default_factory=dict)
    tokens: dict[str, ConflictToken] = field(default_factory=dict)
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
    roles_ready: bool = False
    labels: set[str] = field(default_factory=set)


class State:
    """The whole forge, shared by every area of the fake."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()
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

    def org(self, name: str) -> Org:
        try:
            return self.orgs[name]
        except KeyError:
            raise NotFound(f"no org {name}") from None

    def repo(self, owner: str, name: str) -> Repo:
        try:
            return self.repos[(owner, name)]
        except KeyError:
            raise NotFound(f"no {name} under {owner}") from None

    def create_repo(
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
            self.commit(repo, files, "Create", None)
        return repo

    def commit(self, repo: Repo, files: Files, message: str, author_id: int | None) -> str:
        version = secrets.token_hex(20)
        for path, content in files.items():
            repo.files[path] = content
            repo.tokens[path] = ConflictToken(hashlib.sha1(content).hexdigest())
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
        self, as_: Identity, repo: Repo, name: str, *, at: str | None = None
    ) -> None:
        if name.startswith(PROTECTED_PREFIXES) and not isinstance(as_, Platform):
            raise Forbidden(f"only the platform may create {name}")
        if name in repo.versions:
            raise Conflict(f"{name} already exists")
        repo.versions[name] = at or repo.head

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
        if repo.scope is None or repo.owner not in self.orgs:
            return False
        return any(
            scope.covers(repo.scope) and RANK[held] >= RANK[role] and user_id in members
            for (scope, held), members in self.orgs[repo.owner].roles.items()
        )
