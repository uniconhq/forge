"""Organiser roles and the scopes they are held at. Roles live at the forge and
are read live; this module holds the vocabulary and the inheritance rule: a
role at an org covers every contest and task in it, and a role at a contest
covers every task in it. Admin outranks manager, which outranks observer.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    OBSERVER = "observer"


RANK = {Role.ADMIN: 3, Role.MANAGER: 2, Role.OBSERVER: 1}


class ScopeKind(StrEnum):
    ORG = "org"
    CONTEST = "contest"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class Scope:
    """An org, a contest in an org, or a task in a contest."""

    org: str
    contest: str | None = None
    task: str | None = None

    @property
    def kind(self) -> ScopeKind:
        if self.task is not None:
            return ScopeKind.TASK
        if self.contest is not None:
            return ScopeKind.CONTEST
        return ScopeKind.ORG

    @property
    def name(self) -> str:
        return "/".join(part for part in (self.org, self.contest, self.task) if part)

    def lineage(self) -> tuple[Scope, ...]:
        """This scope and every scope above it, broadest first: the org, then
        the contest if there is one, then the task if there is one. `a` is in
        `b.lineage()` exactly when `a.covers(b)`.
        """
        scopes = [Scope(self.org)]
        if self.contest is not None:
            scopes.append(Scope(self.org, self.contest))
        if self.task is not None:
            scopes.append(self)
        return tuple(scopes)

    def covers(self, other: Scope) -> bool:
        """Whether a role held here reaches `other`."""
        if self.org != other.org:
            return False
        if self.contest is not None and self.contest != other.contest:
            return False
        return self.task is None or self.task == other.task


@dataclass(frozen=True, slots=True)
class RoleGrant:
    scope: Scope
    role: Role


def holds(grants: Iterable[RoleGrant], scope: Scope, role: Role) -> bool:
    """Whether the grants amount to at least `role` at `scope`, counting roles
    inherited from broader scopes and higher roles."""
    return any(grant.scope.covers(scope) and RANK[grant.role] >= RANK[role] for grant in grants)
