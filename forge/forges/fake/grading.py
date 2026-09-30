"""The grading and compute areas in memory.

The fake CI keeps every run it was asked to start with its variables, and
`finish` leaves a run as the CI would once it ends. `refuse_starts` makes
that many starts answer without a run, keeping a run that ended at once, as
the real CI does when the platform's extension refuses to say what the run
is, and `lose_start_answer` starts the next run and then fails as if its
answer were lost.
"""

import secrets
from collections.abc import Mapping
from datetime import datetime

from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable
from forge.domain.grading import Enrolment, GradingRun, Run, RunStatus
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount
from forge.domain.ids import AgentId, OrgName, RunId, TaskId
from forge.forges.fake.state import StartedRun, State
from forge.forges.ids import location, parse_task

GRADING_VARIABLE = "UNICON_GRADING_ID"


class FakeGrading:
    def __init__(self, state: State) -> None:
        self._state = state

    async def activate(self, as_: AsOrgAccount, task: TaskId) -> None:
        self._state.record("activate", as_, task=task)
        _acting_for(as_, task)
        self._task_repo(task)

    def run_variables(self, run: GradingRun) -> Mapping[str, str]:
        return {
            GRADING_VARIABLE: str(run.grading),
            "UNICON_ENVELOPE_URL": run.envelope_url,
            "UNICON_PUBLICATION_COMMIT": str(run.publication_version),
            "UNICON_SUBMISSION": str(run.submission),
            "UNICON_SUBMISSION_COMMIT": str(run.submission_version),
            "UNICON_COMPUTE": run.compute,
        }

    async def start_run(self, as_: AsOrgAccount, run: GradingRun) -> RunId:
        variables = dict(self.run_variables(run))
        self._state.record("start_run", as_, task=run.task, variables=variables)
        self._state.check_up()
        _acting_for(as_, run.task)
        self._task_repo(run.task)
        made = RunId(f"{run.task}/{len(self._state.runs) + 1}")
        refused = self._state.refuse_starts > 0
        status = RunStatus.FAILED if refused else RunStatus.PENDING
        self._state.runs[made] = Run(id=made, status=status)
        self._state.started[made] = StartedRun(str(run.task), variables, self._state.clock.now())
        if refused:
            self._state.refuse_starts -= 1
            raise Rejected("the CI answered 204 without a run")
        if self._state.lose_start_answer:
            self._state.lose_start_answer = False
            raise Unavailable("the CI's answer to the start was lost")
        return made

    async def find_run(self, as_: AsOrgAccount, run: GradingRun, *, since: datetime) -> Run | None:
        self._state.record("find_run", as_, task=run.task, grading=str(run.grading))
        self._state.check_up()
        _acting_for(as_, run.task)
        found = [
            made
            for made, started in self._state.started.items()
            if started.task == run.task
            and started.variables.get(GRADING_VARIABLE) == str(run.grading)
            and started.at >= since
        ]
        return self._state.runs[found[-1]] if found else None

    async def read_run(self, run: RunId) -> Run:
        self._state.record("read_run", PLATFORM, run=run)
        self._state.check_up()
        if run not in self._state.runs:
            raise NotFound(f"no run {run}")
        return self._state.runs[run]

    async def cancel_run(self, run: RunId) -> None:
        self._state.record("cancel_run", PLATFORM, run=run)
        self._state.check_up()
        if run not in self._state.runs:
            raise NotFound(f"no run {run}")
        self._state.runs[run] = Run(id=run, status=RunStatus.CANCELLED)

    def finish(self, run: RunId, status: RunStatus) -> None:
        """Leave the run as the CI would once it ends, or while it runs."""
        self._state.runs[run] = Run(id=run, status=status)

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
