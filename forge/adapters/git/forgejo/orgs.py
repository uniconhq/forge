"""The org area over Forgejo. An org is a `limited` organization owned by the
platform account, with the three role teams, the org account's team, the
labels threads are marked with, and one org-level webhook signed with the
org's own secret, each made by an operation of its own. Deleting the org
takes all of them with it.
"""

from typing import Any

from forge.adapters.git.forgejo.http import Http, json_of, list_of, segment
from forge.adapters.git.forgejo.labels import LABELS
from forge.adapters.git.forgejo.teams import Teams, ci_team_name, scope_of_team, team_name
from forge.adapters.git.forgejo.users import Users, user_from
from forge.domain.errors import NotFound, Rejected
from forge.domain.identity import PLATFORM, AsUser, User
from forge.domain.ids import OrgId
from forge.domain.names import OrgProfile
from forge.domain.roles import Role, RoleGrant, Scope

HOOK_TYPE = "forgejo"
HOOK_EVENTS = ["push", "create", "delete", "issues", "issue_comment", "repository"]

OWNERS_TEAM = "Owners"


class ForgejoOrgs:
    def __init__(self, http: Http, teams: Teams, users: Users, *, platform_account: str) -> None:
        self._http = http
        self._teams = teams
        self._users = users
        self._platform_account = platform_account

    async def name_taken(self, name: str) -> bool:
        """Forgejo answers a user's and an org's name alike at `/users/<name>`,
        since an org is a kind of user there.
        """
        try:
            await self._http.call(PLATFORM, "GET", f"/api/v1/users/{segment(name)}")
        except NotFound:
            return False
        return True

    async def create_org(self, name: OrgId, *, description: str) -> None:
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

    async def create_roles(self, name: OrgId) -> None:
        await self._teams.ensure_role_teams(name)

    async def create_thread_labels(self, name: OrgId) -> None:
        present = {
            str(label["name"])
            for label in await self._http.get_all(PLATFORM, f"/api/v1/orgs/{segment(name)}/labels")
        }
        for label, colour in LABELS.items():
            if label in present:
                continue
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/orgs/{segment(name)}/labels",
                json={"name": label, "color": colour},
            )

    async def create_event_push(self, name: OrgId, *, url: str, secret: str) -> None:
        """One org-level webhook to `url`, kept if one is already there. Forgejo
        allows a webhook only to the hosts `ALLOWED_HOST_LIST` names, which is
        why the URL is the platform's internal one.
        """
        for hook in await self._http.get_all(PLATFORM, f"/api/v1/orgs/{segment(name)}/hooks"):
            if str((hook.get("config") or {}).get("url", "")) == url:
                return
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/orgs/{segment(name)}/hooks",
            json={
                "type": HOOK_TYPE,
                "active": True,
                "events": HOOK_EVENTS,
                "config": {"url": url, "content_type": "json", "secret": secret},
            },
        )

    async def ensure_account_membership(self, name: OrgId, user_id: int) -> bool:
        team = await self._teams.find(name, ci_team_name(name))
        if team is None:
            await self._teams.ensure_role_teams(name)
            team = await self._teams.find(name, ci_team_name(name))
        assert team is not None
        members = await self._teams.members(int(team["id"]))
        if any(int(member["id"]) == user_id for member in members):
            return False
        await self._teams.add_member(int(team["id"]), await self._users.username_of(user_id))
        return True

    async def remove_account_membership(self, name: OrgId, user_id: int) -> None:
        """Forgejo refuses to delete a user who is in a team, so the account
        leaves the org account's team first. The member is found by id in
        the team's listing and removed by the login listed with it.
        """
        team = await self._teams.find(name, ci_team_name(name))
        if team is None:
            return
        for member in await self._teams.members(int(team["id"])):
            if int(member["id"]) == user_id:
                await self._teams.remove_member(int(team["id"]), str(member["login"]))

    async def delete_org(self, name: OrgId) -> None:
        """Forgejo removes an org's teams with their members, its labels and
        its hooks along with it. It refuses an org that still owns a
        repository with a server error (measured on 15.0.8), which reads as
        a forge that is down, so the repositories are asked for first and
        any there is `Rejected`. An org's address answers 404 for a user, so
        this never reaches a person's account.
        """
        try:
            held = list_of(
                await self._http.call(
                    PLATFORM, "GET", f"/api/v1/orgs/{segment(name)}/repos", params={"limit": 1}
                )
            )
            if held:
                raise Rejected(f"the org {name} still owns repositories")
            await self._http.call(PLATFORM, "DELETE", f"/api/v1/orgs/{segment(name)}")
        except NotFound:
            return

    async def update_org(
        self, name: OrgId, *, description: str, display_name: str | None = None
    ) -> None:
        change: dict[str, Any] = {"description": description}
        if display_name is not None:
            change["full_name"] = display_name
        await self._http.call(PLATFORM, "PATCH", f"/api/v1/orgs/{segment(name)}", json=change)

    async def read_org(self, name: OrgId) -> OrgProfile:
        """Forgejo keeps the display name as `full_name`, empty while there is
        none.
        """
        found = json_of(await self._http.call(PLATFORM, "GET", f"/api/v1/orgs/{segment(name)}"))
        return OrgProfile(
            display_name=str(found.get("full_name") or "") or None,
            description=str(found.get("description") or ""),
        )

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        team = await self._teams.ensure(scope, role)
        await self._teams.add_member(int(team["id"]), await self._users.username_of(user_id))

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        username = await self._users.username_of(user_id)
        team = await self._teams.find(scope.org, team_name(scope, role))
        if team is None:
            return
        await self._teams.remove_member(int(team["id"]), username)

    async def roles_of(self, as_: AsUser) -> tuple[RoleGrant, ...]:
        return _grants_from(await self._teams.of_caller(as_))

    async def roles_of_user(self, user_id: int) -> tuple[RoleGrant, ...]:
        """Forgejo lists a user's teams across every org only to that user,
        so the platform account asks for the listing on their behalf with
        `sudo`, which an administrator's token may do: one read, whatever
        the number of orgs and scopes.
        """
        username = await self._users.username_of(user_id)
        return _grants_from(await self._teams.of_user(username))

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        team = await self._teams.find(scope.org, team_name(scope, role))
        if team is None:
            return ()
        return tuple(user_from(member) for member in await self._teams.members(int(team["id"])))


def _grants_from(teams: list[dict[str, Any]]) -> tuple[RoleGrant, ...]:
    """The roles a listing of teams stands for, leaving out every team that
    is not a role, the org account's among them.
    """
    grants = []
    for team in teams:
        org = str((team.get("organization") or {}).get("username", ""))
        found = scope_of_team(org, str(team["name"]))
        if found is not None:
            grants.append(RoleGrant(found[0], found[1]))
    return tuple(grants)
