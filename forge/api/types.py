"""The shared types the actions take and return, and `scope_of_place`, the
scope a contest or task id names, which is how a host reads the names back
out of the ids `contests.list` and `tasks.list` return.
"""

from forge.domain.content import ConflictToken, Edit
from forge.domain.identity import User
from forge.domain.ids import ContestId, OrgName, PublicationId, TaskId, VersionId
from forge.domain.roles import Role, RoleGrant, Scope, ScopeKind, scope_of_place
from forge.domain.sessions import Session
from forge.domain.yaml_models import Problem

__all__ = [
    "ConflictToken",
    "ContestId",
    "Edit",
    "OrgName",
    "Problem",
    "PublicationId",
    "Role",
    "RoleGrant",
    "Scope",
    "ScopeKind",
    "Session",
    "TaskId",
    "User",
    "VersionId",
    "scope_of_place",
]
