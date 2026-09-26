"""The org area in memory."""

from forge.domain.errors import Conflict
from forge.domain.identity import PLATFORM, User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope
from forge.forges.fake.state import Org, State


class FakeOrgs:
    def __init__(self, state: State) -> None:
        self._state = state

    async def create_org(self, name: OrgName, *, description: str) -> None:
        self._state.record("create_org", PLATFORM, name=name)
        self._state.check_up()
        if name in self._state.orgs:
            raise Conflict(f"org {name} already exists")
        self._state.orgs[name] = Org(name, description)

    async def update_org(self, name: OrgName, *, description: str) -> None:
        self._state.record("update_org", PLATFORM, name=name)
        self._state.org(name).description = description

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        self._state.record("grant_role", PLATFORM, user_id=user_id, scope=scope, role=role)
        self._state.user(user_id)
        self._state.org(scope.org).roles.setdefault((scope, role), set()).add(user_id)

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        self._state.record("revoke_role", PLATFORM, user_id=user_id, scope=scope, role=role)
        self._state.org(scope.org).roles.get((scope, role), set()).discard(user_id)

    async def roles_of(self, user_id: int) -> tuple[RoleGrant, ...]:
        self._state.record("roles_of", PLATFORM, user_id=user_id)
        self._state.check_up()
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
