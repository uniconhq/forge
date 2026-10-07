"""Orgs and the roles held at them, at their contests and at their tasks.
Roles live at the host and are read live; the package stores none. An org
also has its event push, the one signed call the host makes to the platform
when something in the org changes, and its service account's place in it.

An org is made and found at the host by its id, the key it is filed under,
never by the name people call it; that name is the platform's own.
"""

from typing import Protocol

from forge.domain.identity import AsUser, User
from forge.domain.ids import OrgId
from forge.domain.names import OrgProfile
from forge.domain.roles import Role, RoleGrant, Scope


class OrgPort(Protocol):
    async def name_taken(self, name: str) -> bool:
        """Whether the host already has a user or an org called that, so a
        new org's name never means a person too.
        """
        ...

    async def create_org(self, name: OrgId, *, description: str) -> None:
        """Make the org itself under its id, and nothing in it. `Conflict` when
        the id is taken. Making an org takes this and the operations after it,
        each a call of its own.
        """
        ...

    async def create_roles(self, name: OrgId) -> None:
        """Make the org's three roles and the org account's place in it. A
        role that exists is kept, so this can be run again.
        """
        ...

    async def create_thread_labels(self, name: OrgId) -> None:
        """Make the labels threads in the org are marked with. A label that
        exists is kept, so this can be run again.
        """
        ...

    async def create_event_push(self, name: OrgId, *, url: str, secret: str) -> None:
        """Make the org's one event push: every change in the org is sent to
        `url`, signed with `secret`. A push to that URL that exists is kept,
        so this can be run again.
        """
        ...

    async def ensure_account_membership(self, name: OrgId, user_id: int) -> bool:
        """Put the org's service account in its place in the org, the one
        `create_roles` made, and say whether it had to be put back.
        """
        ...

    async def remove_account_membership(self, name: OrgId, user_id: int) -> None:
        """Take the org's service account out of its place in the org, which
        the host asks for before the account itself may be deleted. An
        account not there, or an org not there, changes nothing.
        """
        ...

    async def delete_org(self, name: OrgId) -> None:
        """Remove the org with everything made in it by the operations above:
        its roles and who holds them, its labels and its event push. An org
        that still holds a contest, a task or any other place is refused and
        kept. An org not there changes nothing.
        """
        ...

    async def update_org(
        self, name: OrgId, *, description: str, display_name: str | None = None
    ) -> None:
        """`NotFound` when there is no such org."""
        ...

    async def read_org(self, name: OrgId) -> OrgProfile:
        """The org's own fields, read as the platform. `NotFound` when there
        is no such org.
        """
        ...

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        """Give the user the role at the scope; granting a role already held
        changes nothing.
        """
        ...

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        """Take the role away; revoking a role not held changes nothing. The
        service account's place in the org is not a role, and this never
        touches it.
        """
        ...

    async def roles_of(self, as_: AsUser) -> tuple[RoleGrant, ...]:
        """Every role the user holds directly, at every scope, read with
        their own credential and no one else's.
        """
        ...

    async def roles_of_user(self, user_id: int) -> tuple[RoleGrant, ...]:
        """Every role a user holds directly, at every scope, read as the
        platform account in one call, for the rules that ask about someone
        other than the person signed in. `NotFound` when there is no such
        user.
        """
        ...

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        """Everyone holding the role directly at the scope, as the forge
        lists them. Telling a person from a service account is the
        platform's business, by the account's id.
        """
        ...
