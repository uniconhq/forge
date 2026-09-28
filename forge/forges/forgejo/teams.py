"""Roles at Forgejo are teams: one per role per scope, named
`<org>-admin`, `<org>.<contest>-manager` and so on, plus the org account's
team `<org>-ci`. An org-level team covers every repository in the org;
contest and task teams are attached to the repositories of their scope.
"""

from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.roles import Role, Scope, ScopeKind
from forge.forges.forgejo.http import Http, json_of
from forge.forges.forgejo.names import CI_ADMIN_ACCOUNT

CI_TEAM_SUFFIX = "ci"
TEAM_PERMISSIONS = {Role.ADMIN: "admin", Role.MANAGER: "write", Role.OBSERVER: "read"}
UNITS = ["repo.code", "repo.issues", "repo.releases"]
SEARCH_LIMIT = 50


def team_name(scope: Scope, role: Role) -> str:
    return f"{_segment(scope)}-{role.value}"


def ci_team_name(org: str) -> str:
    return f"{org}-{CI_TEAM_SUFFIX}"


def org_account_name(org: str) -> str:
    return f"{CI_ADMIN_ACCOUNT}-{org}"


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


class Teams:
    def __init__(self, http: Http) -> None:
        self._http = http

    async def ensure_role_teams(self, org: str) -> None:
        """The three org-level role teams and the org account's team, each
        made only if it is not there.
        """
        for role in Role:
            await self.ensure(Scope(org), role)
        if await self.find(org, ci_team_name(org)) is None:
            await self._create(org, ci_team_name(org), "admin", all_repos=True)

    async def ensure(self, scope: Scope, role: Role) -> dict[str, Any]:
        name = team_name(scope, role)
        team = await self.find(scope.org, name)
        if team is not None:
            return team
        return await self._create(
            scope.org, name, TEAM_PERMISSIONS[role], all_repos=scope.kind is ScopeKind.ORG
        )

    async def find(self, org: str, name: str) -> dict[str, Any] | None:
        try:
            response = await self._http.call(
                PLATFORM,
                "GET",
                f"/api/v1/orgs/{org}/teams/search",
                params={"q": name, "limit": SEARCH_LIMIT},
            )
        except NotFound:
            return None
        found: list[dict[str, Any]] = json_of(response).get("data") or []
        return next((team for team in found if team["name"] == name), None)

    async def of_caller(self, as_: AsUser) -> list[dict[str, Any]]:
        """Every team the caller belongs to, across every org, in one listing
        read with their own credential.
        """
        return await self._http.get_all(as_, "/api/v1/user/teams")

    async def members(self, team_id: int) -> list[dict[str, Any]]:
        return await self._http.get_all(PLATFORM, f"/api/v1/teams/{team_id}/members")

    async def add_member(self, team_id: int, username: str) -> None:
        await self._http.call(PLATFORM, "PUT", f"/api/v1/teams/{team_id}/members/{username}")

    async def remove_member(self, team_id: int, username: str) -> None:
        await self._http.call(PLATFORM, "DELETE", f"/api/v1/teams/{team_id}/members/{username}")

    async def attach_scope(self, scope: Scope, repo: str) -> None:
        """Put a repository under the role teams of its scope, creating them
        as needed. An org-level scope needs nothing: its teams cover every
        repository.
        """
        if scope.kind is ScopeKind.ORG:
            return
        for role in Role:
            team = await self.ensure(scope, role)
            await self._http.call(
                PLATFORM, "PUT", f"/api/v1/teams/{team['id']}/repos/{scope.org}/{repo}"
            )

    async def _create(
        self, org: str, name: str, permission: str, *, all_repos: bool
    ) -> dict[str, Any]:
        return json_of(
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/orgs/{org}/teams",
                json={
                    "name": name,
                    "permission": permission,
                    "includes_all_repositories": all_repos,
                    "can_create_org_repo": False,
                    "units": UNITS,
                },
            )
        )


def _segment(scope: Scope) -> str:
    return ".".join(part for part in (scope.org, scope.contest, scope.task) if part)
