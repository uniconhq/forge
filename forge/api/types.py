"""The shared types the actions take and return. The ids are opaque and built
from keys, never from names: a host never reads a name out of one. The
records that are shown to a person carry their names, as `Named`,
`ScopeNames` and `HeldRole`, and `names.scope_at` turns the names in an
address into a scope.
"""

from forge.domain.content import ConflictToken, Edit, Uploaded
from forge.domain.identity import User
from forge.domain.ids import ContestId, OrgId, PublicationId, TaskId, VersionId
from forge.domain.invites import Grant, InviteStatus, MailStatus
from forge.domain.names import Named, ScopeNames
from forge.domain.roles import HeldRole, Role, RoleGrant, Scope, ScopeKind
from forge.domain.sessions import Session
from forge.domain.teams import MemberStatus
from forge.domain.yaml_models import Problem

__all__ = [
    "ConflictToken",
    "ContestId",
    "Edit",
    "Grant",
    "HeldRole",
    "InviteStatus",
    "MailStatus",
    "MemberStatus",
    "Named",
    "OrgId",
    "Problem",
    "PublicationId",
    "Role",
    "RoleGrant",
    "Scope",
    "ScopeKind",
    "ScopeNames",
    "Session",
    "TaskId",
    "Uploaded",
    "User",
    "VersionId",
]
