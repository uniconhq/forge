"""The grading and compute areas in memory."""

import secrets
from collections.abc import Mapping

from forge.domain.errors import NotFound
from forge.domain.grading import Enrolment, Run, RunStatus
from forge.domain.identity import PLATFORM, AsOrgAccount
from forge.domain.ids import AgentId, OrgName, RunId, TaskId
from forge.forges.fake import ids
from forge.forges.fake.state import State


class FakeGrading:
    def __init__(self, state: State) -> None:
        self._state = state

    async def register(self, task: TaskId) -> None:
        self._state.record("register", AsOrgAccount(ids.task_parts(task)[0]), task=task)
        self._task_repo(task)

    async def start_run(
        self, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        org = ids.task_parts(task)[0]
        self._state.record(
            "start_run",
            AsOrgAccount(org),
            task=task,
            variables=dict(variables),
            compute_label=compute_label,
        )
        self._state.check_up()
        self._task_repo(task)
        run = Run(id=RunId(f"{task}/{len(self._state.runs) + 1}"), status=RunStatus.PENDING)
        self._state.runs[run.id] = run
        return run.id

    async def read_run(self, run: RunId) -> Run:
        self._state.record("read_run", PLATFORM, run=run)
        if run not in self._state.runs:
            raise NotFound(f"no run {run}")
        return self._state.runs[run]

    async def cancel_run(self, run: RunId) -> None:
        self._state.record("cancel_run", PLATFORM, run=run)
        if run not in self._state.runs:
            raise NotFound(f"no run {run}")
        self._state.runs[run] = Run(id=run, status=RunStatus.CANCELLED)

    def _task_repo(self, task: TaskId) -> None:
        org, contest_name, name = ids.task_parts(task)
        self._state.repo(org, ids.task_repo(contest_name, name))


class FakeComputes:
    def __init__(self, state: State) -> None:
        self._state = state

    async def enrol_agent(self, org: OrgName | None, label: str) -> Enrolment:
        self._state.record("enrol_agent", PLATFORM, org=org, label=label)
        agent = AgentId(str(len(self._state.agents) + 1))
        token = secrets.token_urlsafe(16)
        self._state.agents[agent] = (org, label, token)
        return Enrolment(agent=agent, token=token)

    async def revoke_agent(self, org: OrgName | None, agent: AgentId) -> None:
        self._state.record("revoke_agent", PLATFORM, org=org, agent=agent)
        if agent not in self._state.agents:
            raise NotFound(f"no agent {agent}")
        del self._state.agents[agent]
