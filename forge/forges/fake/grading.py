"""The grading and compute areas in memory."""

import secrets
from collections.abc import Mapping

from forge.domain.errors import Forbidden, NotFound
from forge.domain.grading import Enrolment, Run, RunStatus
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount
from forge.domain.ids import AgentId, OrgName, RunId, TaskId
from forge.forges.fake.state import State
from forge.forges.ids import location, parse_task


class FakeGrading:
    def __init__(self, state: State) -> None:
        self._state = state

    async def register(self, as_: AsOrgAccount, task: TaskId) -> None:
        self._state.record("register", as_, task=task)
        _acting_for(as_, task)
        self._task_repo(task)

    async def start_run(
        self, as_: AsOrgAccount, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        self._state.record(
            "start_run",
            as_,
            task=task,
            variables=dict(variables),
            compute_label=compute_label,
        )
        self._state.check_up()
        _acting_for(as_, task)
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

    async def create_ci_user(self, username: str) -> int:
        self._state.record("create_ci_user", CI_ADMIN, username=username)
        self._state.check_up()
        if username not in self._state.ci_users:
            self._state.ci_users[username] = len(self._state.ci_users) + 1
        return self._state.ci_users[username]

    async def mint_ci_token(self, username: str, forge_password: str) -> str:
        """The sign-in dance in memory: the password must be the account's
        at the forge and the CI must have been told about the account.
        """
        self._state.record("mint_ci_token", PLATFORM, username=username)
        self._state.check_up()
        user = self._state.user_named(username)
        if self._state.passwords.get(user.id) != forge_password:
            raise Forbidden(f"the forge did not accept the sign-in as {username}")
        if username not in self._state.ci_users:
            raise Forbidden(f"the CI admits no user named {username}")
        token = secrets.token_urlsafe(16)
        self._state.ci_tokens[token] = username
        self._state.ci_dead.discard(username)
        return token

    async def ci_user_is_alive(self, as_: AsOrgAccount) -> bool:
        self._state.record("ci_user_is_alive", as_, org=as_.org)
        self._state.check_up()
        username = self._state.ci_tokens.get(as_.ci_token)
        return username is not None and username not in self._state.ci_dead

    def _task_repo(self, task: TaskId) -> None:
        self._state.repo(*location(task))


def _acting_for(as_: AsOrgAccount, task: TaskId) -> None:
    org = parse_task(task).org
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")


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
