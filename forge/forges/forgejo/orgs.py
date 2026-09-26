"""Orgs and roles at Forgejo. An org is a `limited` organization owned by the
platform account with four teams: one per role and one for the org account.
A role at a contest or task is a team named for that scope, attached to the
scope's repositories.
"""

from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope, ScopeKind
from forge.forges.forgejo.base import CI_ADMIN_ACCOUNT, PLATFORM_ACCOUNT, ForgejoBase, json_of
from forge.forges.forgejo.users import user_from

CI_TEAM_SUFFIX = "ci"
OWNERS_TEAM = "Owners"
LABELS = {
    "announcement": "1d76db",
    "clarification": "fbca04",
    "answered": "0e8a16",
}
TEAM_PERMISSIONS = {
    Role.ADMIN: "admin",
    Role.MANAGER: "write",
    Role.OBSERVER: "read",
}
UNITS = ["repo.code", "repo.issues", "repo.releases"]


def team_name(scope: Scope, role: Role) -> str:
    return f"{_scope_segment(scope)}-{role.value}"


def ci_team_name(org: str) -> str:
    return f"{org}-{CI_TEAM_SUFFIX}"


def org_account_name(org: str) -> str:
    return f"{CI_ADMIN_ACCOUNT}-{org}"


def _scope_segment(scope: Scope) -> str:
    return ".".join(part for part in (scope.org, scope.contest, scope.task) if part)


def scope_of_team(org: str, name: str) -> tuple[Scope, Role] | None:
    """The scope and role a team name stands for, or none for a team that is
    not a role.
    """
    head, _, role_name = name.rpartition("-")
    if role_name not in {role.value for role in Role} or not head:
        return None
    parts = head.split(".")
    if parts[0] != org or len(parts) > 3:
        return None
    scope = Scope(org, parts[1] if len(parts) > 1 else None, parts[2] if len(parts) > 2 else None)
    return scope, Role(role_name)


class OrgOps(ForgejoBase):
    async def create_org(self, name: OrgName, *, description: str) -> None:
        await self._http.call(
            PLATFORM,
            "POST",
            "/api/v1/orgs",
            json={
                "username": name,
                "description": description,
                "visibility": "limited",
                "repo_admin_change_team_access": False,
            },
        )
        for role in Role:
            await self._create_team(name, team_name(Scope(name), role), role, all_repos=True)
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/orgs/{name}/teams",
            json={
                "name": ci_team_name(name),
                "permission": "admin",
                "includes_all_repositories": True,
                "can_create_org_repo": False,
                "units": UNITS,
            },
        )
        for label, colour in LABELS.items():
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/orgs/{name}/labels",
                json={"name": label, "color": colour},
            )

    async def update_org(self, name: OrgName, *, description: str) -> None:
        await self._http.call(
            PLATFORM, "PATCH", f"/api/v1/orgs/{name}", json={"description": description}
        )

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        team = await self._ensure_team(scope, role)
        username = await self._username(user_id)
        await self._http.call(PLATFORM, "PUT", f"/api/v1/teams/{team['id']}/members/{username}")

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        team = await self._find_team(scope.org, team_name(scope, role))
        if team is None:
            return
        username = await self._username(user_id)
        await self._http.call(PLATFORM, "DELETE", f"/api/v1/teams/{team['id']}/members/{username}")

    async def roles_of(self, user_id: int) -> tuple[RoleGrant, ...]:
        username = await self._username(user_id)
        grants: list[RoleGrant] = []
        for org in await self._http.get_all(PLATFORM, f"/api/v1/admin/users/{username}/orgs"):
            org_name = str(org["username"])
            for team in await self._http.get_all(PLATFORM, f"/api/v1/orgs/{org_name}/teams"):
                found = scope_of_team(org_name, str(team["name"]))
                if found is None:
                    continue
                if await self._is_member(int(team["id"]), username):
                    grants.append(RoleGrant(found[0], found[1]))
        return tuple(grants)

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        team = await self._find_team(scope.org, team_name(scope, role))
        if team is None:
            return ()
        members = await self._http.get_all(PLATFORM, f"/api/v1/teams/{team['id']}/members")
        return tuple(user_from(member) for member in members)

    async def attach_scope_teams(self, scope: Scope, repo: str) -> None:
        """Put a repository under the role teams of its scope, creating them
        as needed.
        """
        if scope.kind is ScopeKind.ORG:
            return
        for role in Role:
            team = await self._ensure_team(scope, role)
            await self._http.call(
                PLATFORM, "PUT", f"/api/v1/teams/{team['id']}/repos/{scope.org}/{repo}"
            )

    async def _ensure_team(self, scope: Scope, role: Role) -> dict[str, Any]:
        name = team_name(scope, role)
        team = await self._find_team(scope.org, name)
        if team is not None:
            return team
        return await self._create_team(scope.org, name, role, all_repos=scope.kind is ScopeKind.ORG)

    async def _create_team(
        self, org: str, name: str, role: Role, *, all_repos: bool
    ) -> dict[str, Any]:
        return json_of(
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/orgs/{org}/teams",
                json={
                    "name": name,
                    "permission": TEAM_PERMISSIONS[role],
                    "includes_all_repositories": all_repos,
                    "can_create_org_repo": False,
                    "units": UNITS,
                },
            )
        )

    async def _find_team(self, org: str, name: str) -> dict[str, Any] | None:
        try:
            teams = await self._http.get_all(PLATFORM, f"/api/v1/orgs/{org}/teams")
        except NotFound:
            return None
        return next((team for team in teams if team["name"] == name), None)

    async def _is_member(self, team_id: int, username: str) -> bool:
        try:
            await self._http.call(PLATFORM, "GET", f"/api/v1/teams/{team_id}/members/{username}")
        except NotFound:
            return False
        return True

    async def _username(self, user_id: int) -> str:
        found = json_of(
            await self._http.call(PLATFORM, "GET", "/api/v1/users/search", params={"uid": user_id})
        )
        people = found.get("data") or []
        if not people:
            raise NotFound(f"no user with id {user_id}")
        return str(people[0]["login"])


def is_platform_account(username: str) -> bool:
    return username == PLATFORM_ACCOUNT


def is_owners_team(name: str) -> bool:
    return name == OWNERS_TEAM
