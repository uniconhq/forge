"""Orgs and the roles held at them, at their contests and at their tasks.
Roles live at the host and are read live; the package stores none.
"""

from typing import Protocol

from forge.domain.identity import User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope


class OrgPort(Protocol):
    async def create_org(self, name: OrgName, *, description: str) -> None:
        """Make an org with its three roles and its org account. `Conflict`
        when the name is taken.
        """
        ...

    async def update_org(self, name: OrgName, *, description: str) -> None:
        """`NotFound` when there is no such org."""
        ...

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        """Give the user the role at the scope; granting a role already held
        changes nothing.
        """
        ...

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        """Take the role away; revoking a role not held changes nothing."""
        ...

    async def roles_of(self, user_id: int) -> tuple[RoleGrant, ...]:
        """Every role the user holds directly, at every scope."""
        ...

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        """Everyone holding the role directly at the scope."""
        ...
