"""The org area over Forgejo. An org is a `limited` organization owned by the
platform account, with the three role teams, the org account's team and the
labels threads are marked with. The three are separate operations, each
safe to run again, so provisioning can record and resume them one by one.
"""

from forge.domain.identity import PLATFORM, AsUser, User
from forge.domain.ids import OrgName
from forge.domain.roles import Role, RoleGrant, Scope
from forge.forges.forgejo.http import Http
from forge.forges.forgejo.teams import Teams, scope_of_team, team_name
from forge.forges.forgejo.users import Users, user_from

LABELS = {"announcement": "1d76db", "clarification": "fbca04", "answered": "0e8a16"}


class ForgejoOrgs:
    def __init__(self, http: Http, teams: Teams, users: Users) -> None:
        self._http = http
        self._teams = teams
        self._users = users

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

    async def create_roles(self, name: OrgName) -> None:
        await self._teams.ensure_role_teams(name)

    async def create_thread_labels(self, name: OrgName) -> None:
        present = {
            str(label["name"])
            for label in await self._http.get_all(PLATFORM, f"/api/v1/orgs/{name}/labels")
        }
        for label, colour in LABELS.items():
            if label in present:
                continue
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
        team = await self._teams.ensure(scope, role)
        await self._teams.add_member(int(team["id"]), await self._users.username_of(user_id))

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        team = await self._teams.find(scope.org, team_name(scope, role))
        if team is None:
            return
        await self._teams.remove_member(int(team["id"]), await self._users.username_of(user_id))

    async def roles_of(self, as_: AsUser) -> tuple[RoleGrant, ...]:
        grants = []
        for team in await self._teams.of_caller(as_):
            org = str((team.get("organization") or {}).get("username", ""))
            found = scope_of_team(org, str(team["name"]))
            if found is not None:
                grants.append(RoleGrant(found[0], found[1]))
        return tuple(grants)

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        team = await self._teams.find(scope.org, team_name(scope, role))
        if team is None:
            return ()
        return tuple(user_from(member) for member in await self._teams.members(int(team["id"])))
