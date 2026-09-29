"""Orgs and the roles held at them, at their contests and at their tasks.
Roles live at the host and are read live; the package stores none. An org
also has its event push, the one signed call the host makes to the platform
when something in the org changes, and its service account's place in it.
"""

from typing import Protocol

from forge.domain.identity import AsUser, User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope


class OrgPort(Protocol):
    async def name_taken(self, name: OrgName) -> bool:
        """Whether the host already has a user or an org of that name. People
        and orgs share one namespace at the host, so a new org's name is
        checked against both.
        """
        ...

    async def platform_owns(self, name: OrgName) -> bool:
        """Whether an org of that name is there and the platform account owns
        it, which is how a rerun tells an org an earlier try made from a name
        someone else took in between.
        """
        ...

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

    async def create_event_push(self, name: OrgName, *, url: str, secret: str) -> None:
        """Make the org's one event push: every change in the org is sent to
        `url`, signed with `secret`. A push to that URL that exists is kept,
        so this can be run again.
        """
        ...

    async def ensure_account_membership(self, name: OrgName, user_id: int) -> bool:
        """Put the org's service account in its place in the org, the one
        `create_roles` made, and say whether it had to be put back.
        """
        ...

    async def update_org(
        self, name: OrgName, *, description: str, display_name: str | None = None
    ) -> None:
        """`NotFound` when there is no such org."""
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
