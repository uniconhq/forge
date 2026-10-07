"""Roles at Forgejo are teams: one per role per scope, named
`<org>-admin`, `<org>.<contest>-manager` and so on, plus the org account's
team `<org>-ci`. An org-level team covers every repository in the org;
contest and task teams are attached to the repositories of their scope, and
a contest's teams to its tasks' repositories too, which is how a role at a
contest reaches its tasks at the forge.

No organiser's team is a repository admin at the forge. A repository admin
may edit and delete the repository's protected tags and branches (measured
on Forgejo 15.0.8), which would let them make a publication or rewrite
history, so the admin and manager roles are both `write` there and the
platform holds the difference between them. Only the org account's team is
an admin, because the CI accepts nothing less from whoever activates a
repository. A team found with any other permission is put back.
"""

from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.roles import Role, Scope, ScopeKind
from forge.forges.forgejo.http import Http, json_of, segment

CI_TEAM_SUFFIX = "ci"
TEAM_PERMISSIONS = {Role.ADMIN: "write", Role.MANAGER: "write", Role.OBSERVER: "read"}
CI_TEAM_PERMISSION = "admin"
UNITS = ["repo.code", "repo.issues", "repo.releases"]
SEARCH_LIMIT = 50


def team_name(scope: Scope, role: Role) -> str:
    return f"{_segment(scope)}-{role.value}"


def ci_team_name(org: str) -> str:
    return f"{org}-{CI_TEAM_SUFFIX}"


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
        made only if it is not there and given back its permission if that
        has changed.
        """
        for role in Role:
            await self.ensure(Scope(org), role)
        team = await self.find(org, ci_team_name(org))
        if team is None:
            await self._create(org, ci_team_name(org), CI_TEAM_PERMISSION, all_repos=True)
        elif team.get("permission") != CI_TEAM_PERMISSION:
            await self._set_permission(team, CI_TEAM_PERMISSION)

    async def ensure(self, scope: Scope, role: Role) -> dict[str, Any]:
        """The role's team at the scope, made if it is not there and given
        back its permission if that has changed.
        """
        name = team_name(scope, role)
        permission = TEAM_PERMISSIONS[role]
        team = await self.find(scope.org, name)
        if team is None:
            return await self._create(
                scope.org, name, permission, all_repos=scope.kind is ScopeKind.ORG
            )
        if team.get("permission") != permission:
            await self._set_permission(team, permission)
        return team

    async def find(self, org: str, name: str) -> dict[str, Any] | None:
        try:
            response = await self._http.call(
                PLATFORM,
                "GET",
                f"/api/v1/orgs/{segment(org)}/teams/search",
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

    async def of_user(self, username: str) -> list[dict[str, Any]]:
        """Every team a user belongs to, across every org, in one listing the
        platform account reads on their behalf.
        """
        return await self._http.get_all(PLATFORM, "/api/v1/user/teams", sudo=username)

    async def members(self, team_id: int) -> list[dict[str, Any]]:
        return await self._http.get_all(PLATFORM, f"/api/v1/teams/{team_id}/members")

    async def add_member(self, team_id: int, username: str) -> None:
        await self._http.call(
            PLATFORM, "PUT", f"/api/v1/teams/{team_id}/members/{segment(username)}"
        )

    async def remove_member(self, team_id: int, username: str) -> None:
        await self._http.call(
            PLATFORM, "DELETE", f"/api/v1/teams/{team_id}/members/{segment(username)}"
        )

    async def delete_scope(self, scope: Scope) -> None:
        """Delete the role teams of a contest or a task, each one that is
        there.
        """
        for role in Role:
            team = await self.find(scope.org, team_name(scope, role))
            if team is None:
                continue
            try:
                await self._http.call(PLATFORM, "DELETE", f"/api/v1/teams/{team['id']}")
            except NotFound:
                continue

    async def attach_scope(self, scope: Scope, repo: str) -> int:
        """Put a repository under the role teams of its scope and of every
        contest above it, creating the teams as needed, so a contest's roles
        reach its tasks as well as the contest itself. An org's teams need
        nothing: they cover every repository. A team already attached is
        left alone; returns how many had to be attached.
        """
        attached = 0
        for at in scope.lineage():
            if at.kind is ScopeKind.ORG:
                continue
            for role in Role:
                team = await self.ensure(at, role)
                path = f"/api/v1/teams/{team['id']}/repos/{segment(scope.org)}/{segment(repo)}"
                try:
                    await self._http.call(PLATFORM, "GET", path)
                except NotFound:
                    await self._http.call(PLATFORM, "PUT", path)
                    attached += 1
        return attached

    async def _set_permission(self, team: dict[str, Any], permission: str) -> None:
        await self._http.call(
            PLATFORM,
            "PATCH",
            f"/api/v1/teams/{team['id']}",
            json={
                "name": team["name"],
                "permission": permission,
                "includes_all_repositories": bool(team.get("includes_all_repositories")),
                "can_create_org_repo": False,
                "units": UNITS,
            },
        )

    async def _create(
        self, org: str, name: str, permission: str, *, all_repos: bool
    ) -> dict[str, Any]:
        return json_of(
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/orgs/{segment(org)}/teams",
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
