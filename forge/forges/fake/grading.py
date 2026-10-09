"""The grading and compute areas in memory.

The fake CI keeps every run it was asked to start with its variables, and
signs the question it asks the platform about one the way a real CI signs
its questions, with a key of its own: `config_request` is that question,
which a test hands to `answer` as the CI would.
`X-Fake-Signature` is the hex
HMAC-SHA256 of the key over the creation time, a newline and the body, and
`X-Fake-Created` that time; a
request whose signature is wrong, or made more than five minutes from now,
is refused like one the CI did not sign. `refuse_starts` makes that many
starts answer without a run, as the real CI does when the platform's
extension refuses to say what the run is, and
`lose_start_answer` starts the next run and then fails as if its answer
were lost. A CI credential in `revoked_ci_tokens` is refused, as the CI
refuses one it no longer holds.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable
from forge.domain.grading import (
    Enrolment,
    GradingRun,
    InboundAnswer,
    InboundRequest,
    RunLookup,
    RunPlaces,
    RunSpec,
    RunState,
)
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount
from forge.domain.ids import AgentId, OrgId, RunId, TaskId
from forge.forges.fake.state import StartedRun, State
from forge.forges.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    location,
    parse_publication,
    parse_submission,
    parse_task,
)

GRADING_VARIABLE = "UNICON_GRADING_ID"
SIGNATURE_HEADER = "X-Fake-Signature"
CREATED_HEADER = "X-Fake-Created"
FRESHNESS = timedelta(minutes=5)
CLONE_URL = "http://forge.test"
TASK_CHECKOUT = "/woodpecker/task"
SUBMISSION_CHECKOUT = "/woodpecker/submission"


@dataclass(frozen=True, slots=True)
class _Ask:
    task: TaskId
    grading: str | None
    variables: Mapping[str, str]
    clone_url: str


def run_variables(run: GradingRun) -> dict[str, str]:
    """The variables the fake CI starts `run` with."""
    return {
        GRADING_VARIABLE: str(run.grading),
        "UNICON_ENVELOPE_URL": run.envelope_url,
        "UNICON_PUBLICATION_COMMIT": str(run.publication_version),
        "UNICON_SUBMISSION": str(run.submission),
        "UNICON_SUBMISSION_COMMIT": str(run.submission_version),
        "UNICON_COMPUTE": run.compute,
    }


class FakeGrading:
    def __init__(self, state: State) -> None:
        self._state = state

    async def activate(self, as_: AsOrgAccount, task: TaskId) -> None:
        self._state.record("activate", as_, task=task)
        _acting_for(self._state, as_, task)
        self._task_repo(task)
        self._state.activated.add(task)

    async def deactivate(self, as_: AsOrgAccount, task: TaskId) -> None:
        self._state.record("deactivate", as_, task=task)
        self._state.check_up()
        _acting_for(self._state, as_, task)
        self._state.activated.discard(task)

    async def start_run(self, as_: AsOrgAccount, run: GradingRun, spec: RunSpec) -> RunId:
        variables = run_variables(run)
        self._state.record("start_run", as_, task=run.task, variables=variables, spec=spec)
        self._state.check_up()
        _acting_for(self._state, as_, run.task)
        self._task_repo(run.task)
        if self._state.refuse_starts > 0:
            self._state.refuse_starts -= 1
            raise Rejected("the CI answered 204 without a run")
        made = RunId(f"{run.task}/{len(self._state.runs) + 1}")
        self._state.runs[made] = StartedRun(str(run.task), variables, self._state.clock.now())
        if self._state.lose_start_answer:
            self._state.lose_start_answer = False
            raise Unavailable("the CI's answer to the start was lost")
        return made

    async def run_state(self, run: RunId) -> RunState:
        self._state.record("run_state", CI_ADMIN, run=run)
        found = self._state.runs.get(run)
        if found is None:
            return RunState.LOST
        return RunState.FINISHED if found.cancelled else found.ci_state

    async def cancel_run(self, run: RunId) -> None:
        self._state.record("cancel_run", PLATFORM, run=run)
        self._state.check_up()
        if run not in self._state.runs:
            raise NotFound(f"no run {run}")
        self._state.runs[run].cancelled = True

    async def answer(
        self, request: InboundRequest, lookup: RunLookup, *, now: datetime
    ) -> InboundAnswer:
        self._state.record("answer", PLATFORM)
        ask = self._ask(request, now)
        run, spec = await lookup(ask.grading, ask.task)
        if dict(ask.variables) != run_variables(run):
            raise Rejected("the run was not started with the variables its grading starts it with")
        return self._answer(run, ask, spec)

    def _ask(self, request: InboundRequest, now: datetime) -> _Ask:
        headers = {name.lower(): value for name, value in request.headers.items()}
        created = headers.get(CREATED_HEADER.lower(), "")
        signature = headers.get(SIGNATURE_HEADER.lower(), "")
        expected = _sign(self._state.ci_key, created, request.body)
        if not hmac.compare_digest(expected, signature):
            raise Forbidden("the request is not signed by the CI")
        if not created.isdigit() or abs(now.timestamp() - int(created)) > FRESHNESS.total_seconds():
            raise Forbidden("the request is stale")
        try:
            document = json.loads(request.body)
        except ValueError:
            raise Rejected("the CI's request is not JSON") from None
        if not isinstance(document, dict) or not isinstance(document.get("task"), str):
            raise Rejected("the CI's request names no task")
        variables = document.get("variables") or {}
        return _Ask(
            task=TaskId(document["task"]),
            grading=variables.get(GRADING_VARIABLE),
            variables=variables,
            clone_url=document["clone_url"],
        )

    def _answer(self, run: GradingRun, ask: _Ask, spec: RunSpec) -> InboundAnswer:
        places = self.run_places(run)
        clone_image, harness_image = spec.clone_image, spec.harness_image
        cache = [f"unicon-lfs-{places.task['org']}:/lfs-cache"]
        document = {
            "labels": run.compute,
            "clone": [
                {
                    "name": "task",
                    "image": clone_image,
                    **places.task,
                    **places.publication,
                    "volumes": cache,
                },
                {"name": "submission", "image": clone_image, **places.submission, "volumes": cache},
            ],
            "steps": [
                {
                    "name": "grade",
                    "image": harness_image,
                    "volumes": ["unicon-filter:/run/unicon:ro"],
                }
            ],
            "clone_url": ask.clone_url,
        }
        return InboundAnswer(
            body=json.dumps(document, sort_keys=True).encode(), content_type="application/json"
        )

    def run_places(self, run: GradingRun) -> RunPlaces:
        task, publication = parse_publication(run.publication)
        workspace, task_name, number = parse_submission(run.submission)
        return RunPlaces(
            task={"org": task.org, "repo": task.repo},
            publication={
                "tag": f"{PUBLISHED_PREFIX}{publication}",
                "commit": str(run.publication_version),
            },
            submission={
                "org": workspace.org,
                "repo": workspace.submission_repo(task_name),
                "tag": f"{SUBMISSION_PREFIX}{number}",
                "commit": str(run.submission_version),
            },
            checkouts={"task": TASK_CHECKOUT, "submission": SUBMISSION_CHECKOUT},
        )

    def config_request(
        self,
        task: TaskId,
        variables: Mapping[str, str],
        *,
        now: datetime,
        body: bytes | None = None,
        key: bytes | None = None,
    ) -> InboundRequest:
        """The question the fake CI asks the platform about a run of the task
        started with `variables`, signed at `now`, as the platform's extension
        receives it; the CI asks it while the start is under way. `body`
        replaces what is sent after it is signed, and `key` signs with
        another key, for a test of a request the CI did not make.
        """
        sent = json.dumps(
            {"task": str(task), "variables": dict(variables), "clone_url": CLONE_URL}
        ).encode()
        created = str(int(now.timestamp()))
        return InboundRequest(
            method="POST",
            target="/api/v1/ci/config",
            headers={
                CREATED_HEADER: created,
                SIGNATURE_HEADER: _sign(key or self._state.ci_key, created, sent),
                "Content-Type": "application/json",
            },
            body=sent if body is None else body,
        )

    async def create_ci_user(self, username: str) -> int:
        self._state.record("create_ci_user", CI_ADMIN, username=username)
        self._state.check_up()
        if username not in self._state.ci_users:
            self._state.ci_users[username] = len(self._state.ci_users) + 1
        return self._state.ci_users[username]

    async def delete_ci_user(self, username: str) -> None:
        self._state.record("delete_ci_user", CI_ADMIN, username=username)
        self._state.check_up()
        self._state.ci_users.pop(username, None)
        for token in [token for token, owner in self._state.ci_tokens.items() if owner == username]:
            del self._state.ci_tokens[token]

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
        return token

    def _task_repo(self, task: TaskId) -> None:
        self._state.repo(*location(task))


def _sign(key: bytes, created: str, body: bytes) -> str:
    return hmac.new(key, created.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def _acting_for(state: State, as_: AsOrgAccount, task: TaskId) -> None:
    org = parse_task(task).org
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")
    if as_.ci_token in state.revoked_ci_tokens:
        raise Forbidden("the CI no longer holds that credential")


class FakeComputes:
    def __init__(self, state: State) -> None:
        self._state = state

    async def enrol_agent(self, org: OrgId | None, label: str) -> Enrolment:
        self._state.record("enrol_agent", PLATFORM, org=org, label=label)
        agent = AgentId(str(len(self._state.agents) + 1))
        token = secrets.token_urlsafe(16)
        self._state.agents[agent] = (org, label, token)
        return Enrolment(agent=agent, token=token)

    async def revoke_agent(self, org: OrgId | None, agent: AgentId) -> None:
        self._state.record("revoke_agent", PLATFORM, org=org, agent=agent)
        if agent not in self._state.agents:
            raise NotFound(f"no agent {agent}")
        del self._state.agents[agent]
