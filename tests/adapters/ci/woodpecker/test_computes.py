"""The machines' enrolment at Woodpecker, as its administrator."""

from forge.adapters.ci.woodpecker import WoodpeckerCi
from forge.domain.ids import OrgId
from tests.adapters.conftest import Recorder, ok


async def test_an_org_agent_is_enrolled_under_its_org(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/orgs/lookup/acme", ok({"id": 42}))
    recorder.on("POST", "/api/orgs/42/agents", ok({"id": 9, "token": "t"}))
    recorder.on("POST", "/api/agents", ok({"id": 10, "token": "g"}))

    org_agent = await woodpecker.computes.enrol_agent(OrgId("acme"), "box")
    global_agent = await woodpecker.computes.enrol_agent(None, "pool")
    await woodpecker.computes.revoke_agent(OrgId("acme"), org_agent.agent)

    assert (org_agent.agent, org_agent.token) == ("9", "t")
    assert global_agent.agent == "10"
    assert "POST /api/orgs/42/agents" in recorder.calls()
    assert "DELETE /api/orgs/42/agents/9" in recorder.calls()
    assert recorder.sent("POST", "/api/orgs/42/agents") == [{"name": "box"}]
