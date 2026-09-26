"""Computes: the machines that grade, enrolled at the CI as agents of one org
or of the platform.
"""

from typing import Protocol

from forge.domain.grading import Enrolment
from forge.domain.ids import AgentId, OrgName


class ComputePort(Protocol):
    async def enrol_agent(self, org: OrgName | None, label: str) -> Enrolment:
        """Enrol a machine as an agent of the org, or of the platform when
        `org` is none, and return the token it identifies itself with.
        """
        ...

    async def revoke_agent(self, org: OrgName | None, agent: AgentId) -> None:
        """Remove the agent; its next request for work is refused."""
        ...
