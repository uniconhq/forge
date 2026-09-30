"""Organiser roles and the scopes they are held at. Roles live at the forge and
are read live; this module holds the vocabulary and the inheritance rule: a
role at an org covers every contest and task in it, and a role at a contest
covers every task in it. Admin outranks manager, which outranks observer.

A contest and a task are each named two ways: as a `Scope`, where roles are
held, and as the id the port hands out, `<org>/<contest>` and
`<org>/<contest>/<task>`, which is what the tables store and what a place
with files is called. `contest_id_of`, `task_id_of`, `place_of` and their
inverses turn one into the other, and `workflow_id_of` gives the id a
workflow reference is read by; together they are the one place outside the
forge implementations that knows those shapes.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from forge.domain.errors import NotFound
from forge.domain.ids import ContestId, PrimitiveId, TaskId, WorkflowId
from forge.domain.workflow_definition import WorkflowRef


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


def contest_id_of(scope: Scope) -> ContestId:
    """The id of the contest a contest or task scope is in. An org scope is in
    no contest, and is refused.
    """
    if scope.contest is None:
        raise ValueError(f"{scope.name} is an org, not in a contest")
    return ContestId(f"{scope.org}/{scope.contest}")


def contest_scope(contest: ContestId) -> Scope:
    """The scope of the contest an id names. `NotFound` for an id of another
    shape.
    """
    scope = scope_of_place(contest)
    if scope.kind is not ScopeKind.CONTEST:
        raise NotFound(f"{contest} is not a contest")
    return scope


def contest_id_prefix(org: str) -> str:
    """What the id of every contest in the org starts with."""
    return f"{org}/"


def task_id_of(scope: Scope) -> TaskId:
    """The id of the task a task scope names. Any other scope is refused."""
    if scope.contest is None or scope.task is None:
        raise ValueError(f"{scope.name} is not a task")
    return TaskId(f"{scope.org}/{scope.contest}/{scope.task}")


def task_scope(task: TaskId) -> Scope:
    """The scope of the task an id names. `NotFound` for an id of another
    shape.
    """
    scope = scope_of_place(task)
    if scope.kind is not ScopeKind.TASK:
        raise NotFound(f"{task} is not a task")
    return scope


def place_of(scope: Scope) -> ContestId | TaskId:
    """The id of the contest or task a scope names, the place its files are
    in. An org has no files of its own, and is refused.
    """
    return task_id_of(scope) if scope.kind is ScopeKind.TASK else contest_id_of(scope)


def scope_of_place(place: str) -> Scope:
    """The scope of the contest or task an id names. `NotFound` for an id of
    neither shape, since it names nothing.
    """
    parts = place.split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise NotFound(f"{place} is neither a contest nor a task")
    return Scope(*parts)


def workflow_id_of(ref: WorkflowRef) -> WorkflowId:
    """The id of the workflow a reference names, `<owner>/<name>`; the
    version is given to the read beside it.
    """
    return WorkflowId(f"{ref.owner}/{ref.name}")


PRIMITIVE_OWNER = "unicon"
"""The one owner primitives have: the platform's own org."""


def primitive_id_of(ref: WorkflowRef) -> PrimitiveId | None:
    """The id of the primitive a reference names, `<name>`, or none when its
    owner is not the platform's org, which alone holds primitives.
    """
    return PrimitiveId(ref.name) if ref.owner == PRIMITIVE_OWNER else None
