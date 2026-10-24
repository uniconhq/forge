"""The CI in memory: its grading and compute areas over its own records.
The git host is reached only through the `CiHost` it is paired with, and
the clock, the record of calls and the outage switch are the world's it
shares with the other fakes.

The fake CI keeps every pipeline it was asked to start with its variables,
on the org's dispatch repository, `<org>/unicon-dispatch`, which
`activate_dispatch` switches on, and signs the question it asks the
platform about one the way a real CI signs its questions, with a key of its
own: `config_request` is that question, which a test hands to `answer` as
the CI would. `X-Fake-Signature` is the hex HMAC-SHA256 of the key over the
creation time, a newline and the body, and `X-Fake-Created` that time; a
request whose signature is wrong, or made more than five minutes from now,
is refused like one the CI did not sign. `refuse_starts` makes that many
starts answer without a run, as the real CI does when the platform's
extension refuses to say what the run is, and `lose_start_answer` starts
the next run and then fails as if its answer were lost. A CI credential in
`revoked_ci_tokens` is refused, as the CI refuses one it no longer holds,
and a pipeline of a dispatch repository not in `activated` is not found,
as the CI knows no such repository.

With `asks` off the fake CI is handed every pipeline whole, as a CI that is
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

import httpx

from forge.adapters.browser import open_browser
from forge.adapters.ci.host import CiHost
from forge.adapters.fake_world import FakeWorld
from forge.adapters.ids import DISPATCH_REPO
from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable, VariablesDiffer
from forge.domain.grading import (
    Enrolment,
    InboundAnswer,
    InboundRequest,
    PipelineLookup,
    PipelineRun,
    PipelineSpec,
    RunState,
)
from forge.domain.identity import PLATFORM, AsOrgAccount, CiState, OrgAccountRef, User
from forge.domain.ids import AgentId, OrgId, RunId
from forge.domain.names import service_account_name

GRADING_VARIABLE = "UNICON_GRADING_ID"
SIGNATURE_HEADER = "X-Fake-Signature"
CREATED_HEADER = "X-Fake-Created"
FRESHNESS = timedelta(minutes=5)
CLONE_URL = "http://forge.test"
SIGN_IN_SHARE = 2 / 3

NOWHERE = httpx.MockTransport(lambda request: httpx.Response(404))
"""What the fake CI's browser reaches: nothing, since the fake git host's
sign-in takes no pages."""


@dataclass
class StartedRun:
    """A pipeline the fake CI was asked to start: the dispatch repository it
    is on, the variables it was started with, when, whether it was cancelled, and where the CI
    has it, queued until a test says otherwise.
    """

    task: str
    variables: dict[str, str]
    at: datetime
    cancelled: bool = False
    ci_state: RunState = RunState.QUEUED


class FakeCi:
    """The CI's records, which a test reads and turns as `fake.ci`, and its
    two areas over them.
    """

    def __init__(
        self,
        world: FakeWorld,
        host: CiHost,
        *,
        login_lifetime: timedelta = timedelta(days=30),
        asks: bool = True,
    ) -> None:
        self.runs: dict[RunId, StartedRun] = {}
        self.ci_key = secrets.token_bytes(32)
        self.refuse_starts = 0
        self.lose_start_answer = False
        self.agents: dict[AgentId, tuple[str | None, str, str]] = {}
        self.ci_users: dict[str, int] = {}
        self.activated: set[str] = set()
        self.ci_tokens: dict[str, str] = {}
        self.revoked_ci_tokens: set[str] = set()
        self.grading = FakeGrading(self, world, host, login_lifetime=login_lifetime, asks=asks)
        self.computes = FakeComputes(self, world)

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Ask:
    org: OrgId
    grading: str | None
    variables: Mapping[str, str]
    clone_url: str


def pipeline_variables(run: PipelineRun) -> dict[str, str]:
    """The variables the fake CI starts a run task's pipeline with."""
    variables = {
        GRADING_VARIABLE: str(run.grading),
        "UNICON_PIPELINE": run.pipeline,
        "UNICON_PUBLICATION_COMMIT": str(run.publication_version),
        "UNICON_COMPUTE": run.compute,
    }
    if run.submission is not None:
        variables["UNICON_SUBMISSION"] = str(run.submission)
        variables["UNICON_SUBMISSION_COMMIT"] = str(run.submission_version)
    return variables


def dispatch_of(org: str) -> str:
    """Where the fake CI keeps the org's dispatch repository's runs."""
    return f"{org}/{DISPATCH_REPO}"


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
        self,
        ci: FakeCi,
        world: FakeWorld,
        host: CiHost,
        *,
        login_lifetime: timedelta = timedelta(days=30),
        asks: bool = True,
    ) -> None:
        self._ci = ci
        self._world = world
        self._host = host
        self.login_lifetime = login_lifetime
        self.asks = asks

    async def set_up_org(self, org: OrgId, account: OrgAccountRef) -> CiState:
        """The account's user at the fake CI, then the sign-in dance in
        memory: the password must be the account's at the git host.
        """
        self._world.record("set_up_org", PLATFORM, org=org, username=account.username)
        self._world.check_up()
        user_id = self._ci.ci_users.setdefault(account.username, len(self._ci.ci_users) + 1)
        token = await self._sign_in(account.username, account.password)
        return _state(user_id, token, self._world.clock.now(), account.forge_user_id)

    async def tear_down_org(self, org: OrgId, state: CiState) -> None:
        self._world.record("tear_down_org", PLATFORM, org=org)
        self._world.check_up()
        username = service_account_name(org)
        self._ci.ci_users.pop(username, None)
        for token in [token for token, owner in self._ci.ci_tokens.items() if owner == username]:
            del self._ci.ci_tokens[token]

    def needs_refresh(self, state: CiState, now: datetime) -> bool:
        signed_in_at = _signed_in_at(state)
        return signed_in_at is None or now - signed_in_at > self.login_lifetime * SIGN_IN_SHARE

    async def refresh(self, org: OrgId, state: CiState) -> CiState:
        """A fresh password at the git host for the account the state names
        by its id, and the sign-in made with it; refused for an account that
        is not the org's.
        """
        self._world.record("refresh", PLATFORM, org=org)
        self._world.check_up()
        found = json.loads(state)
        try:
            account: User | None = await self._host.account(found.get("account_id") or 0)
        except NotFound:
            account = None
        if account is None or account.username.lower() != service_account_name(org).lower():
            raise Rejected(f"the CI state of {org}'s account names no account of the org")
        password = secrets.token_urlsafe(16)
        await self._host.set_password(account.id, password)
        token = await self._sign_in(account.username, password)
        return _state(found["user_id"], token, self._world.clock.now(), account.id)

    async def _sign_in(self, username: str, forge_password: str) -> str:
        async with open_browser([self._host.web_address], transport=NOWHERE) as browser:
            await self._host.sign_in(browser, username, forge_password)
        if username not in self._ci.ci_users:
            raise Forbidden(f"the CI admits no user named {username}")
        token = secrets.token_urlsafe(16)
        self._ci.ci_tokens[token] = username
        return token

    async def activate_dispatch(self, as_: AsOrgAccount, org: OrgId) -> bool:
        self._world.record("activate_dispatch", as_, org=org)
        _acting_for_org(self._ci, as_, org)
        await self._host.repo_id(org, DISPATCH_REPO)
        place = dispatch_of(org)
        known = place in self._ci.activated
        self._ci.activated.add(place)
        return not known

    async def start_pipeline(self, as_: AsOrgAccount, run: PipelineRun) -> RunId:
        variables = pipeline_variables(run)
        self._world.record("start_pipeline", as_, org=run.org, variables=variables)
        self._world.check_up()
        _acting_for_org(self._ci, as_, run.org)
        place = dispatch_of(run.org)
        if place not in self._ci.activated:
            raise NotFound(f"the CI does not know {place}")
        if self._ci.refuse_starts > 0:
            self._ci.refuse_starts -= 1
            raise Rejected("the CI answered 204 without a run")
        made = RunId(f"{place}/{len(self._ci.runs) + 1}")
        self._ci.runs[made] = StartedRun(place, variables, self._world.clock.now())
        if self._ci.lose_start_answer:
            self._ci.lose_start_answer = False
            raise Unavailable("the CI's answer to the start was lost")
        return made

    async def run_state(self, run: RunId) -> RunState:
        self._world.record("run_state", PLATFORM, run=run)
        found = self._ci.runs.get(run)
        if found is None:
            return RunState.LOST
        return RunState.FINISHED if found.cancelled else found.ci_state

    async def cancel_run(self, run: RunId) -> None:
        self._world.record("cancel_run", PLATFORM, run=run)
        self._world.check_up()
        if run not in self._ci.runs:
            raise NotFound(f"no run {run}")
        self._ci.runs[run].cancelled = True

    async def answer(
        self, request: InboundRequest, lookup: PipelineLookup, *, now: datetime
    ) -> InboundAnswer:
        self._world.record("answer", PLATFORM)
        if not self.asks:
            raise NotFound("this CI is handed every run whole and never asks")
        ask = self._ask(request, now)
        pipeline, steps = await lookup(ask.grading, ask.org)
        if dict(ask.variables) != pipeline_variables(pipeline):
            raise VariablesDiffer(
                "the pipeline was not started with the variables its grading starts it with"
            )
        return _pipeline_answer(pipeline, ask, steps)

    def _ask(self, request: InboundRequest, now: datetime) -> _Ask:
        headers = {name.lower(): value for name, value in request.headers.items()}
        created = headers.get(CREATED_HEADER.lower(), "")
        signature = headers.get(SIGNATURE_HEADER.lower(), "")
        expected = _sign(self._ci.ci_key, created, request.body)
        if not hmac.compare_digest(expected, signature):
            raise Forbidden("the request is not signed by the CI")
        if not created.isdigit() or abs(now.timestamp() - int(created)) > FRESHNESS.total_seconds():
            raise Forbidden("the request is stale")
        try:
            document = json.loads(request.body)
        except ValueError:
            raise Rejected("the CI's request is not JSON") from None
        if not isinstance(document, dict) or not isinstance(document.get("org"), str):
            raise Rejected("the CI's request names no repository")
        variables = document.get("variables") or {}
        return _Ask(
            org=OrgId(document["org"]),
            grading=variables.get(GRADING_VARIABLE),
            variables=variables,
            clone_url=document["clone_url"],
        )

    def config_request(
        self,
        org: OrgId,
        variables: Mapping[str, str],
        *,
        now: datetime,
        body: bytes | None = None,
        key: bytes | None = None,
    ) -> InboundRequest:
        """The question the fake CI asks the platform about a pipeline on
        the org's dispatch repository started with `variables`, signed at
        `now`, as the platform's extension receives it; the CI asks it while
        the start is under way. `body` replaces what is sent after it is
        signed, and `key` signs with another key, for a test of a request
        the CI did not make.
        """
        sent = json.dumps(
            {"org": str(org), "variables": dict(variables), "clone_url": CLONE_URL}
        ).encode()
        created = str(int(now.timestamp()))
        return InboundRequest(
            method="POST",
            target="/api/v1/ci/config",
            headers={
                CREATED_HEADER: created,
                SIGNATURE_HEADER: _sign(key or self._ci.ci_key, created, sent),
                "Content-Type": "application/json",
            },
            body=sent if body is None else body,
        )


def _sign(key: bytes, created: str, body: bytes) -> str:
    return hmac.new(key, created.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def _pipeline_answer(run: PipelineRun, ask: _Ask, spec: PipelineSpec) -> InboundAnswer:
    """What the fake CI is answered for a pipeline: its labels, its
    checkout and each step as the spec gives it."""
    document = {
        "labels": list(spec.labels),
        "clone": (
            None
            if run.submission is None or not spec.clones
            else {
                "image": spec.clone_image,
                "submission": str(run.submission),
                "commit": str(run.submission_version),
            }
        ),
        "steps": [
            {
                "name": step.name,
                "image": step.image,
                "settings": dict(step.settings),
                "depends_on": list(step.depends_on),
                "cache": step.cache,
                "user": step.user,
                "always": step.always,
            }
            for step in spec.steps
        ],
        "clone_url": ask.clone_url,
    }
    return InboundAnswer(
        body=json.dumps(document, sort_keys=True).encode(), content_type="application/json"
    )


def _acting_for_org(ci: FakeCi, as_: AsOrgAccount, org: str) -> None:
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")
    if as_.ci_state and token_in(as_.ci_state) in ci.revoked_ci_tokens:
        raise Forbidden("the CI no longer holds that credential")


class FakeComputes:
    def __init__(self, ci: FakeCi, world: FakeWorld) -> None:
        self._ci = ci
        self._world = world

    async def enrol_agent(self, org: OrgId | None, label: str) -> Enrolment:
        self._world.record("enrol_agent", PLATFORM, org=org, label=label)
        agent = AgentId(str(len(self._ci.agents) + 1))
        token = secrets.token_urlsafe(16)
        self._ci.agents[agent] = (org, label, token)
        return Enrolment(agent=agent, token=token)

    async def revoke_agent(self, org: OrgId | None, agent: AgentId) -> None:
        self._world.record("revoke_agent", PLATFORM, org=org, agent=agent)
        if agent not in self._ci.agents:
            raise NotFound(f"no agent {agent}")
        del self._ci.agents[agent]
