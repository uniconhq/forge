"""Orgs and the roles held at them, at their contests and at their tasks.
Roles live at the host and are read live; the package stores none.
"""

from typing import Protocol

from forge.domain.identity import AsUser, User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope


class OrgPort(Protocol):
    async def create_org(self, name: OrgName, *, description: str) -> None:
        """Make the org itself, and nothing in it. `Conflict` when the name
        is taken. Making an org is several calls that can fail halfway, so
        the pieces are separate operations and the `provisioning` service
        records which have completed.
        """
        ...

    async def create_roles(self, name: OrgName) -> None:
        """Make the org's three roles and the org account's place in it. A
        role that exists is kept, so this can be run again.
        """
        ...

    async def create_thread_labels(self, name: OrgName) -> None:
        """Make the labels threads in the org are marked with. A label that
        exists is kept, so this can be run again.
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

    async def roles_of(self, as_: AsUser) -> tuple[RoleGrant, ...]:
        """Every role the user holds directly, at every scope, read with
        their own credential and no one else's.
        """
        ...

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        """Everyone holding the role directly at the scope."""
        ...
