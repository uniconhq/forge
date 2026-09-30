"""The grading and compute areas in memory.

The fake CI keeps every run it was asked to start with its variables, and
signs the question it asks the platform about one the way a real CI signs
its extension calls, with a key of its own: `config_request` is that
question, which a test hands to the extension as the CI would, and `finish`
leaves a run as the CI would once it ends. `X-Fake-Signature` is the hex
HMAC-SHA256 of the key over the creation time, a newline and the body, and
`X-Fake-Created` that time; a
request whose signature is wrong, or made more than five minutes from now,
is refused like one the CI did not sign. `refuse_starts` makes that many
starts answer without a run, keeping a run that ended at once, as the real
CI does when the platform's extension refuses to say what the run is, and
`lose_start_answer` starts the next run and then fails as if its answer
were lost.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta

from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable
from forge.domain.grading import (
    CiAnswer,
    CiRequest,
    ConfigAsk,
    Enrolment,
    GradingRun,
    Run,
    RunPlaces,
    RunStatus,
)
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount
from forge.domain.ids import AgentId, OrgName, RunId, TaskId
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

    async def read_config_request(self, request: CiRequest, *, now: datetime) -> ConfigAsk:
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
        return ConfigAsk(
            task=TaskId(document["task"]),
            grading=variables.get(GRADING_VARIABLE),
            variables=variables,
            clone_url=document["clone_url"],
        )

    def config_answer(
        self, run: GradingRun, ask: ConfigAsk, *, harness_image: str, clone_image: str
    ) -> CiAnswer:
        places = self.run_places(run)
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
        return CiAnswer(
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
    ) -> CiRequest:
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
        return CiRequest(
            method="POST",
            target="/api/v1/ci/config",
            headers={
                CREATED_HEADER: created,
                SIGNATURE_HEADER: _sign(key or self._state.ci_key, created, sent),
                "Content-Type": "application/json",
            },
            body=sent if body is None else body,
        )

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


def _sign(key: bytes, created: str, body: bytes) -> str:
    return hmac.new(key, created.encode() + b"\n" + body, hashlib.sha256).hexdigest()


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
