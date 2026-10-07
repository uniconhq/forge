"""The compute area over Woodpecker: agents enrolled under an org, which the
CI hands only that org's runs, or under the platform, which take any org's.
"""

from forge.domain.grading import Enrolment
from forge.domain.identity import CI_ADMIN
from forge.domain.ids import AgentId, OrgId
from forge.forges.forgejo.http import Http, json_of, segment


class WoodpeckerComputes:
    def __init__(self, ci: Http) -> None:
        self._ci = ci

    async def enrol_agent(self, org: OrgId | None, label: str) -> Enrolment:
        created = json_of(
            await self._ci.call(
                CI_ADMIN, "POST", await self._agents_path(org), json={"name": label}
            )
        )
        return Enrolment(agent=AgentId(str(created["id"])), token=str(created["token"]))

    async def revoke_agent(self, org: OrgId | None, agent: AgentId) -> None:
        await self._ci.call(CI_ADMIN, "DELETE", f"{await self._agents_path(org)}/{agent}")

    async def _agents_path(self, org: OrgId | None) -> str:
        if org is None:
            return "/api/agents"
        found = json_of(await self._ci.call(CI_ADMIN, "GET", f"/api/orgs/lookup/{segment(org)}"))
        return f"/api/orgs/{int(found['id'])}/agents"
