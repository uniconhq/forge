"""The org area in memory."""

from forge.domain.errors import Conflict
from forge.domain.identity import PLATFORM, AsUser, User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope
from forge.forges.fake.state import Org, State

LABELS = ("announcement", "clarification", "answered")


class FakeOrgs:
    def __init__(self, state: State) -> None:
        self._state = state

    async def name_taken(self, name: OrgName) -> bool:
        self._state.record("name_taken", PLATFORM, name=name)
        self._state.check_up()
        return self._taken(name)

    async def platform_owns(self, name: OrgName) -> bool:
        """The fake makes an org only through `create_org`, as the platform,
        so every org it holds is the platform's.
        """
        self._state.record("platform_owns", PLATFORM, name=name)
        self._state.check_up()
        return name in self._state.orgs

    async def create_org(self, name: OrgName, *, description: str) -> None:
        self._state.record("create_org", PLATFORM, name=name)
        self._state.check_up()
        if self._taken(name):
            raise Conflict(f"user already exists [name: {name}]")
        self._state.orgs[name] = Org(name, description)

    async def create_roles(self, name: OrgName) -> None:
        self._state.record("create_roles", PLATFORM, name=name)
        self._state.check_up()
        org = self._state.org(name)
        for role in Role:
            org.roles.setdefault((Scope(name), role), set())
        org.roles_ready = True

    async def create_thread_labels(self, name: OrgName) -> None:
        self._state.record("create_thread_labels", PLATFORM, name=name)
        self._state.check_up()
        self._state.org(name).labels.update(LABELS)

    async def create_event_push(self, name: OrgName, *, url: str, secret: str) -> None:
        self._state.record("create_event_push", PLATFORM, name=name, url=url)
        self._state.check_up()
        org = self._state.org(name)
        if org.event_push is not None and org.event_push[0] == url:
            return
        org.event_push = (url, secret)

    async def ensure_account_membership(self, name: OrgName, user_id: int) -> bool:
        self._state.record("ensure_account_membership", PLATFORM, name=name, user_id=user_id)
        self._state.check_up()
        org = self._state.org(name)
        self._state.user(user_id)
        if user_id in org.account_members:
            return False
        org.account_members.add(user_id)
        return True

    async def update_org(
        self, name: OrgName, *, description: str, display_name: str | None = None
    ) -> None:
        self._state.record("update_org", PLATFORM, name=name)
        self._state.check_up()
        org = self._state.org(name)
        org.description = description
        if display_name is not None:
            org.display_name = display_name

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        self._state.record("grant_role", PLATFORM, user_id=user_id, scope=scope, role=role)
        self._state.user(user_id)
        self._state.org(scope.org).roles.setdefault((scope, role), set()).add(user_id)

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        self._state.record("revoke_role", PLATFORM, user_id=user_id, scope=scope, role=role)
        self._state.user(user_id)
        self._state.org(scope.org).roles.get((scope, role), set()).discard(user_id)

    async def roles_of(self, as_: AsUser) -> tuple[RoleGrant, ...]:
        self._state.record("roles_of", as_, user_id=as_.user_id)
        self._state.check_up()
        user_id = self._state.author(as_)
        return self._grants_of(user_id)

    async def roles_of_user(self, user_id: int) -> tuple[RoleGrant, ...]:
        self._state.record("roles_of_user", PLATFORM, user_id=user_id)
        self._state.check_up()
        self._state.user(user_id)
        return self._grants_of(user_id)

    def _grants_of(self, user_id: int | None) -> tuple[RoleGrant, ...]:
        return tuple(
            RoleGrant(scope, role)
            for org in self._state.orgs.values()
            for (scope, role), members in org.roles.items()
            if user_id in members
        )

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        self._state.record("holders_of", PLATFORM, scope=scope, role=role)
        members = self._state.org(scope.org).roles.get((scope, role), set())
        return tuple(self._state.user(member) for member in sorted(members))

    def _taken(self, name: str) -> bool:
        """People and orgs share one namespace, compared without case."""
        lowered = name.lower()
        return lowered in {org.lower() for org in self._state.orgs} or any(
            user.username.lower() == lowered for user in self._state.users.values()
        )
