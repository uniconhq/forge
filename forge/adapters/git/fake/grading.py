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

With `asks` off the fake CI is handed every run whole, as a CI that is
pushed to is, and never asks what one is: its `answer` is `NotFound`, so
the services are tested both with and without the question.

What an org account holds at the fake CI is written the way Woodpecker's
is, its user id, its token, when it signed in and the account's id at the
forge, so a row the migration moved reads here too; `token_in` reads the
token out of one for a test.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from forge.adapters.git.fake.state import StartedRun, State
from forge.adapters.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    location,
    parse_publication,
    parse_submission,
    parse_task,
)
from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable, VariablesDiffer
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
from forge.domain.identity import PLATFORM, AsOrgAccount, CiState, OrgAccountRef
from forge.domain.ids import AgentId, OrgId, RunId, TaskId
from forge.domain.names import service_account_name

GRADING_VARIABLE = "UNICON_GRADING_ID"
SIGNATURE_HEADER = "X-Fake-Signature"
CREATED_HEADER = "X-Fake-Created"
FRESHNESS = timedelta(minutes=5)
CLONE_URL = "http://forge.test"
TASK_CHECKOUT = "/woodpecker/task"
SUBMISSION_CHECKOUT = "/woodpecker/submission"
SIGN_IN_SHARE = 2 / 3


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


def token_in(state: CiState) -> str:
    """The CI token an org account's state holds."""
    return str(json.loads(state)["token"])


def _signed_in_at(state: CiState) -> datetime | None:
    found = json.loads(state)["signed_in_at"]
    return datetime.fromisoformat(found) if found else None


def _state(
    user_id: int | None, token: str, signed_in_at: datetime, account_id: int | None
) -> CiState:
    return CiState(
        json.dumps(
            {
                "user_id": user_id,
                "token": token,
                "signed_in_at": signed_in_at.isoformat(),
                "account_id": account_id,
            }
        )
    )


class FakeGrading:
    def __init__(
        self, state: State, *, login_lifetime: timedelta = timedelta(days=30), asks: bool = True
    ) -> None:
        self._state = state
        self.login_lifetime = login_lifetime
        self.asks = asks

    async def set_up_org(self, org: OrgId, account: OrgAccountRef) -> CiState:
        """The account's user at the fake CI, then the sign-in dance in
        memory: the password must be the account's at the forge.
        """
        self._state.record("set_up_org", PLATFORM, org=org, username=account.username)
        self._state.check_up()
        user_id = self._state.ci_users.setdefault(account.username, len(self._state.ci_users) + 1)
        token = self._sign_in(account.username, account.password)
        return _state(user_id, token, self._state.clock.now(), account.forge_user_id)

    async def tear_down_org(self, org: OrgId, state: CiState) -> None:
        self._state.record("tear_down_org", PLATFORM, org=org)
        self._state.check_up()
        username = service_account_name(org)
        self._state.ci_users.pop(username, None)
        for token in [token for token, owner in self._state.ci_tokens.items() if owner == username]:
            del self._state.ci_tokens[token]

    def needs_refresh(self, state: CiState, now: datetime) -> bool:
        signed_in_at = _signed_in_at(state)
        return signed_in_at is None or now - signed_in_at > self.login_lifetime * SIGN_IN_SHARE

    async def refresh(self, org: OrgId, state: CiState) -> CiState:
        """A fresh password at the forge for the account the state names by
        its id, and the sign-in made with it; refused for an account that is
        not the org's.
        """
        self._state.record("refresh", PLATFORM, org=org)
        self._state.check_up()
        found = json.loads(state)
        account = self._state.users.get(found.get("account_id") or 0)
        if account is None or account.username.lower() != service_account_name(org).lower():
            raise Rejected(f"the CI state of {org}'s account names no account of the org")
        password = secrets.token_urlsafe(16)
        self._state.passwords[account.id] = password
        token = self._sign_in(account.username, password)
        return _state(found["user_id"], token, self._state.clock.now(), account.id)

    def _sign_in(self, username: str, forge_password: str) -> str:
        user = self._state.user_named(username)
        if self._state.passwords.get(user.id) != forge_password:
            raise Forbidden(f"the forge did not accept the sign-in as {username}")
        if username not in self._state.ci_users:
            raise Forbidden(f"the CI admits no user named {username}")
        token = secrets.token_urlsafe(16)
        self._state.ci_tokens[token] = username
        return token

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
        self._state.record("run_state", PLATFORM, run=run)
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
        if not self.asks:
            raise NotFound("this CI is handed every run whole and never asks")
        ask = self._ask(request, now)
        run, spec = await lookup(ask.grading, ask.task)
        if dict(ask.variables) != run_variables(run):
            raise VariablesDiffer(
                "the run was not started with the variables its grading starts it with"
            )
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

    def _task_repo(self, task: TaskId) -> None:
        self._state.repo(*location(task))


def _sign(key: bytes, created: str, body: bytes) -> str:
    return hmac.new(key, created.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def _acting_for(state: State, as_: AsOrgAccount, task: TaskId) -> None:
    org = parse_task(task).org
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")
    if as_.ci_state and token_in(as_.ci_state) in state.revoked_ci_tokens:
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
