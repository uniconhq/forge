"""The grading area over Woodpecker: activating a task's repository once,
deactivating it again, and starting runs as the org account the caller
hands in, finding a run by its grading id, reading and cancelling runs as
the CI's administrator, the configuration extension's signed question and
the answer to it, and the org account's own user and token at the CI, made
and deleted by the administrator and signed in by the sign-in dance.

What the org account holds at Woodpecker is its user id, the token its
sign-in minted, when that was, and the account's id at the git host, by which
a refresh finds it (`ci_state.py`). Woodpecker keeps the
account's login at the git host fresh only while the account calls it, and
that login lasts as long as the git host's refresh token, which deploy sets to
the session's hard lifetime, `UNICON_SESSION_HARD_TTL`, 30 days unless the
operator says otherwise; an org that grades nothing for longer would lose
it. So a sign-in older than `SIGN_IN_SHARE` of that lifetime, 20 days by
default, needs refreshing: an org that grades every day signs in again
every 20 days, and one that was quiet for months on its first use. A
refresh gives the account a fresh password at the git host, signs it in with
that, and throws the password away.

A run is a manual pipeline on the task's repository, started on `main`,
since the CI starts a run only on a branch, with the run's variables:

    UNICON_GRADING_ID           the grading
    UNICON_ENVELOPE_URL         where the harness fetches its envelope
    UNICON_PUBLICATION_COMMIT   the commit the publication froze
    UNICON_SUBMISSION_REPO      the submission's repository, <org>/<repo>
    UNICON_SUBMISSION_COMMIT    the commit the submission's files went in with
    UNICON_COMPUTE              the label of the machines that may take it

The commit the CI records is whatever `main` points at, which may be a
draft, so the answer to the extension pins both checkouts itself. It is one
workflow, `grading`, the same every time for the same run:

- `when` the run is started by hand, which every grading run is;
- `labels` from `UNICON_COMPUTE`, `pool:platform` becoming `pool: platform`;
- under `clone:`, two full steps, `task` and `submission`, each running the
  clone image with `remote`, `sha`, `ref` and `path` set, `lfs` on, and the
  machine's store of large files of the task's org mounted as
  `unicon-lfs-<org>:/lfs-cache`, one store per org, so no org's
  task is served a large file another org's task brought to the machine by
  naming its object id. Neither carries `environment`: a clone step
  with one stops counting as a clone plugin and is lent no credential. The
  clone image is on the CI's trusted-clone list, so both are lent the org
  account's credential, and nothing else is;
- under `steps:`, one step, `grade`, running the harness image the plan
  names with the socket filter's socket mounted read-only as
  `unicon-filter:/run/unicon:ro`, so the harness can connect to the socket
  and cannot put another in its place, `DOCKER_HOST` naming it, and no
  credential, since the task repository is trusted for `volumes` alone.

The checkouts land at `/woodpecker/task` and `/woodpecker/submission`, inside
the run's workspace volume, which is what the envelope tells the harness.
The deployment sets Woodpecker's pipeline timeout,
`WOODPECKER_DEFAULT_PIPELINE_TIMEOUT`, to the platform's `RUN_TIMEOUT`.

The extension's request is signed by the CI's ed25519 key (RFC 9421,
`signatures.py`). The key is read from `GET /api/signature/public-key` with
the administrator's token and kept; a request that does not verify has the
key read again once, in case the CI's key changed. The key is asked for at
most once a minute, whether the last read worked or not, so requests that
do not verify never make the platform call the CI more often than that.
"""

import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from forge.adapters.ci.host import CiHost
from forge.adapters.ci.woodpecker import signatures
from forge.adapters.ci.woodpecker.ci_login import CiLogin
from forge.adapters.ci.woodpecker.ci_state import WoodpeckerState, read_state, written
from forge.adapters.ci.woodpecker.http import CI_ADMIN, Caller, Http, json_of, segment
from forge.adapters.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    MalformedId,
    parse_publication,
    parse_submission,
    parse_task,
    task_of_repo,
)
from forge.domain.errors import (
    Conflict,
    Forbidden,
    NotFound,
    Rejected,
    Unavailable,
    VariablesDiffer,
)
from forge.domain.grading import (
    GradingRun,
    InboundAnswer,
    InboundRequest,
    RunLookup,
    RunPlaces,
    RunSpec,
    RunState,
)
from forge.domain.identity import AsOrgAccount, CiState, OrgAccountRef
from forge.domain.ids import OrgId, RunId, TaskId
from forge.domain.names import service_account_name
from forge.log import get_logger

log = get_logger(__name__)

GRADING_VARIABLE = "UNICON_GRADING_ID"
ENVELOPE_VARIABLE = "UNICON_ENVELOPE_URL"
PUBLICATION_VARIABLE = "UNICON_PUBLICATION_COMMIT"
SUBMISSION_REPO_VARIABLE = "UNICON_SUBMISSION_REPO"
SUBMISSION_VARIABLE = "UNICON_SUBMISSION_COMMIT"
COMPUTE_VARIABLE = "UNICON_COMPUTE"

WORKFLOW_NAME = "grading"
WORKSPACE = "/woodpecker"
TASK_CHECKOUT = f"{WORKSPACE}/task"
SUBMISSION_CHECKOUT = f"{WORKSPACE}/submission"
LFS_CACHE_PREFIX = "unicon-lfs-"
LFS_CACHE_PATH = "/lfs-cache"
VOLUME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
"""What an org's name may be to name a volume, within Docker's rule for a
volume name."""
FILTER_VOLUME = "unicon-filter:/run/unicon:ro"
FILTER_SOCKET = "unix:///run/unicon/docker.sock"

PUBLIC_KEY_PATH = "/api/signature/public-key"
KEY_REFETCH_SECONDS = 60.0
QUEUE_PATH = "/api/queue/info"
QUEUE_KEPT_SECONDS = 10.0
WAITING_LISTS = ("pending", "waiting_on_deps")
TAKEN_LIST = "running"
FINISHED_STATUSES = frozenset(
    {"success", "failure", "killed", "canceled", "error", "blocked", "declined", "skipped"}
)
UNFINISHED_STATES = frozenset({"pending", "running"})
SIGN_IN_SHARE = 2 / 3
PASSWORD_BYTES = 24


@dataclass(frozen=True, slots=True)
class _Ask:
    """The extension's question once its signature is checked: the task
    whose run it is, the grading id the run was started with as it was
    given, every variable it was started with, and where the CI clones the
    task from.
    """

    task: TaskId
    grading: str | None
    variables: Mapping[str, str]
    clone_url: str


def run_variables(run: GradingRun) -> dict[str, str]:
    """The variables `run` is started with, the same every time for the same
    run, and the only ones the extension answers a run started with.
    """
    workspace, task, _ = parse_submission(run.submission)
    return {
        GRADING_VARIABLE: str(run.grading),
        ENVELOPE_VARIABLE: run.envelope_url,
        PUBLICATION_VARIABLE: str(run.publication_version),
        SUBMISSION_REPO_VARIABLE: f"{workspace.org}/{workspace.submission_repo(task)}",
        SUBMISSION_VARIABLE: str(run.submission_version),
        COMPUTE_VARIABLE: run.compute,
    }


class WoodpeckerGrading:
    def __init__(
        self,
        ci: Http,
        host: CiHost,
        *,
        ci_public_url: str,
        login: CiLogin,
        login_lifetime: timedelta,
    ) -> None:
        """`host` is the git host, reached only through what a CI needs of
        it. `login_lifetime` is how long the account's login at the git host
        lasts, the session's hard lifetime.
        """
        self._ci = ci
        self._host = host
        self._ci_public_url = ci_public_url.rstrip("/")
        self._login = login
        self._login_lifetime = login_lifetime
        self._key: Ed25519PublicKey | None = None
        self._key_read_at = -KEY_REFETCH_SECONDS
        self._queue: dict[tuple[int, int], RunState] = {}
        self._queue_read_at = -QUEUE_KEPT_SECONDS

    async def set_up_org(self, org: OrgId, account: OrgAccountRef) -> CiState:
        """The account's user at the CI, made by the administrator, then
        signed in with its password at the git host.
        """
        user_id = await self._ci_user(account.username)
        token = await self._login.mint_token(account.username, account.password)
        return written(WoodpeckerState(user_id, token, datetime.now(UTC), account.forge_user_id))

    async def tear_down_org(self, org: OrgId, state: CiState) -> None:
        """Woodpecker deletes a user's own org along with it, and when that
        org is gone already it answers 404 and keeps the user (measured on
        3.18.1), so a 404 is believed only once the user reads as gone too.
        """
        username = service_account_name(org)
        path = f"/api/users/{segment(username)}"
        try:
            await self._ci.call(CI_ADMIN, "DELETE", path)
        except NotFound:
            try:
                await self._ci.call(CI_ADMIN, "GET", path)
            except NotFound:
                return
            raise Rejected(f"the CI answered 404 to deleting {username} and kept it") from None

    def needs_refresh(self, state: CiState, now: datetime) -> bool:
        signed_in_at = read_state(state).signed_in_at
        return signed_in_at is None or now - signed_in_at > self._login_lifetime * SIGN_IN_SHARE

    async def refresh(self, org: OrgId, state: CiState) -> CiState:
        """The account found at the git host by the id it was made with, never
        by its name, and refused unless it is still the org's account, so a
        fresh password is never set on anyone else's.
        """
        current = read_state(state)
        if current.account_id is None:
            raise Rejected(f"the CI state of {org}'s account names no account at the forge")
        account = await self._host.account(current.account_id)
        if account.username.lower() != service_account_name(org).lower():
            raise Rejected(f"the account the CI state of {org} names is not the org's account")
        password = secrets.token_urlsafe(PASSWORD_BYTES)
        await self._host.set_password(account.id, password)
        token = await self._login.mint_token(account.username, password)
        return written(replace(current, token=token, signed_in_at=datetime.now(UTC)))

    async def activate(self, as_: AsOrgAccount, task: TaskId) -> bool:
        """The CI knew the task when it answers the activation with a
        conflict; its settings are applied again either way.
        """
        ref = parse_task(task)
        account = _acting_for(as_, ref.org)
        repo_id = await self._host.repo_id(ref.org, ref.repo)
        try:
            activated = json_of(
                await self._ci.call(
                    account, "POST", "/api/repos", params={"forge_remote_id": repo_id}
                )
            )
            new = True
        except Conflict:
            activated = await self._lookup(account, ref.org, ref.repo)
            new = False
        await self._ci.call(
            CI_ADMIN,
            "PATCH",
            f"/api/repos/{activated['id']}",
            json={"trusted": {"network": False, "volumes": True, "security": False}},
        )
        await self._host.remove_hooks(ref.org, ref.repo, self._ci_public_url)
        return new

    async def deactivate(self, as_: AsOrgAccount, task: TaskId) -> None:
        """As the org account, which activated the repository: Woodpecker
        asks the git host to drop its webhook with the credential of whoever
        deactivates, and the org account's is the one kept fresh. `remove`
        has the CI forget the repository rather than keep it switched off.
        """
        ref = parse_task(task)
        account = _acting_for(as_, ref.org)
        try:
            repo = await self._lookup(account, ref.org, ref.repo)
            await self._ci.call(
                account, "DELETE", f"/api/repos/{repo['id']}", params={"remove": "true"}
            )
        except NotFound:
            return

    async def start_run(self, as_: AsOrgAccount, run: GradingRun, spec: RunSpec) -> RunId:
        """`spec` is not sent: Woodpecker asks what the run is while it
        starts it, and is answered from the platform's records then.
        """
        ref = parse_task(run.task)
        account = _acting_for(as_, ref.org)
        repo = await self._lookup(account, ref.org, ref.repo)
        started = await self._ci.call(
            account,
            "POST",
            f"/api/repos/{repo['id']}/pipelines",
            json={"branch": self._host.default_branch, "variables": run_variables(run)},
        )
        body = started.json() if started.content else None
        if not isinstance(body, dict) or not isinstance(body.get("number"), int):
            raise Rejected(f"the CI answered {started.status_code} without a run")
        return RunId(f"{repo['id']}/{body['number']}")

    async def cancel_run(self, run: RunId) -> None:
        repo_id, number = _parse_run(run)
        await self._ci.call(CI_ADMIN, "POST", f"/api/repos/{repo_id}/pipelines/{number}/cancel")

    async def run_state(self, run: RunId) -> RunState:
        """Woodpecker 3.18.1 drops a run from its queue for good when the
        machine it handed the run to does not renew its claim within a
        minute, after a dropped network or a machine that died during the
        checkout, and leaves the pipeline saying `pending`
        (woodpecker-ci/woodpecker#7063), so the pipeline's status says
        nothing either way.

        So from the queue, which lists every task with its repository and
        pipeline number under `pending`, `waiting_on_deps` or `running`, read
        at most once every `QUEUE_KEPT_SECONDS` however many runs are asked
        about; only a run the queue does not hold costs a read of its
        pipeline. A run finishing marks its workflow finished before it
        leaves the queue, so one in no queue whose workflows are all still
        `pending` or `running` was dropped, and one whose pipeline the CI
        does not know is lost too.
        """
        key = _parse_run(run)
        queued = (await self._queue_view()).get(key)
        if queued is not None:
            return queued
        repo_id, number = key
        try:
            pipeline = json_of(
                await self._ci.call(CI_ADMIN, "GET", f"/api/repos/{repo_id}/pipelines/{number}")
            )
        except NotFound:
            return RunState.LOST
        if pipeline.get("status") in FINISHED_STATUSES:
            return RunState.FINISHED
        workflows = [one for one in pipeline.get("workflows") or [] if isinstance(one, dict)]
        if workflows and all(one.get("state") in UNFINISHED_STATES for one in workflows):
            return RunState.LOST
        return RunState.FINISHED

    async def _queue_view(self) -> dict[tuple[int, int], RunState]:
        if time.monotonic() - self._queue_read_at < QUEUE_KEPT_SECONDS:
            return self._queue
        queue = json_of(await self._ci.call(CI_ADMIN, "GET", QUEUE_PATH))
        view: dict[tuple[int, int], RunState] = {}
        for name, state in (
            *((waiting, RunState.QUEUED) for waiting in WAITING_LISTS),
            (TAKEN_LIST, RunState.TAKEN),
        ):
            for task in queue.get(name) or []:
                if isinstance(task, dict):
                    repo, number = task.get("repo_id"), task.get("pipeline_number")
                    if isinstance(repo, int) and isinstance(number, int):
                        view[(repo, number)] = state
        self._queue, self._queue_read_at = view, time.monotonic()
        return view

    async def answer(
        self, request: InboundRequest, lookup: RunLookup, *, now: datetime
    ) -> InboundAnswer:
        """The configuration extension. Of the two checks that decide what a
        run runs, `lookup` keeps the platform's, a grading of that task being
        started, and this the CI's: anyone who may start a manual pipeline
        on the task's repository may pass variables of their own, a harness
        image among them, so a run started with any variable but exactly
        those `run_variables` gives is answered with nothing.
        """
        ask = await self._ask(request, now)
        run, spec = await lookup(ask.grading, ask.task)
        if dict(ask.variables) != run_variables(run):
            raise VariablesDiffer(
                "the run was not started with the variables its grading starts it with"
            )
        return self._answer(run, ask, spec)

    async def _ask(self, request: InboundRequest, now: datetime) -> _Ask:
        await self._verify(request, now)
        try:
            document = json.loads(request.body)
        except UnicodeDecodeError, json.JSONDecodeError:
            document = None
        if not isinstance(document, dict):
            raise Rejected("the CI's request is not a JSON object")
        repo = document.get("repo") or {}
        pipeline = document.get("pipeline") or {}
        if not isinstance(repo, dict) or not isinstance(pipeline, dict):
            raise Rejected("the CI's request names no repository or run")
        owner, name = _owner_and_name(repo)
        try:
            task = task_of_repo(owner, name)
        except MalformedId as exc:
            raise Rejected(f"the CI asks about {owner}/{name}, which is no task") from exc
        variables = pipeline.get("variables") or {}
        if not isinstance(variables, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in variables.items()
        ):
            raise Rejected("the run's variables are not text")
        clone_url = repo.get("clone_url")
        if not isinstance(clone_url, str) or not clone_url:
            raise Rejected("the CI's request names no clone URL")
        return _Ask(
            task=task.id,
            grading=variables.get(GRADING_VARIABLE),
            variables=dict(variables),
            clone_url=clone_url,
        )

    def _answer(self, run: GradingRun, ask: _Ask, spec: RunSpec) -> InboundAnswer:
        places = self.run_places(run)
        clone_image = spec.clone_image
        task_remote = _remote(ask.clone_url, places.task["org"], places.task["repo"])
        submission_remote = _remote(
            ask.clone_url, places.submission["org"], places.submission["repo"]
        )
        cache = lfs_cache_volume(places.task["org"])
        workflow = {
            "when": [{"event": "manual"}],
            "labels": _labels(run.compute),
            "clone": [
                _clone(
                    "task",
                    clone_image,
                    remote=task_remote,
                    sha=places.publication["commit"],
                    ref=f"refs/tags/{places.publication['tag']}",
                    path=TASK_CHECKOUT,
                    lfs=True,
                    cache=cache,
                ),
                _clone(
                    "submission",
                    clone_image,
                    remote=submission_remote,
                    sha=places.submission["commit"],
                    ref=f"refs/tags/{places.submission['tag']}",
                    path=SUBMISSION_CHECKOUT,
                    # Every file a contestant uploads is an object in the
                    # forge's large-file store, and the commit holds a
                    # pointer to it, so this checkout pulls them as the
                    # task's does. Both share the org's store on the
                    # machine, so a file is downloaded once however many
                    # runs name it.
                    lfs=True,
                    cache=cache,
                ),
            ],
            "steps": [
                {
                    "name": "grade",
                    "image": spec.harness_image,
                    "environment": {"DOCKER_HOST": FILTER_SOCKET},
                    "volumes": [FILTER_VOLUME],
                }
            ],
        }
        data = yaml.safe_dump(workflow, sort_keys=False, default_flow_style=False)
        body = {"configs": [{"name": WORKFLOW_NAME, "data": data}]}
        return InboundAnswer(body=json.dumps(body).encode(), content_type="application/json")

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

    async def _ci_user(self, username: str) -> int:
        """Looked up before it is made: Woodpecker answers a duplicate with a
        server error, which the client would take for a CI that is down.
        """
        try:
            found = json_of(await self._ci.call(CI_ADMIN, "GET", f"/api/users/{segment(username)}"))
        except NotFound:
            found = json_of(
                await self._ci.call(CI_ADMIN, "POST", "/api/users", json={"login": username})
            )
        return int(found["id"])

    async def _verify(self, request: InboundRequest, now: datetime) -> None:
        """Check the request against the CI's key, reading the key again once
        when it does not verify and the key was not read in the last minute.
        """
        key = self._key
        if key is None:
            if self._read_recently():
                raise Unavailable("the CI's signing key could not be read a moment ago")
            key = await self._read_key()
        try:
            _verify_with(key, request, now)
            return
        except signatures.Unverified as first:
            if self._read_recently():
                log.info("grading.config_unverified", reason=str(first))
                raise Forbidden("the request is not signed by the CI") from None
        key = await self._read_key()
        try:
            _verify_with(key, request, now)
        except signatures.Unverified as exc:
            log.info("grading.config_unverified", reason=str(exc))
            raise Forbidden("the request is not signed by the CI") from None

    def _read_recently(self) -> bool:
        return time.monotonic() - self._key_read_at < KEY_REFETCH_SECONDS

    async def _read_key(self) -> Ed25519PublicKey:
        self._key_read_at = time.monotonic()
        try:
            response = await self._ci.call(CI_ADMIN, "GET", PUBLIC_KEY_PATH)
        except NotFound:
            # Kept apart from a CI that never asks, which is what `NotFound`
            # from `answer` says.
            raise Unavailable("the CI has no signing key to read") from None
        try:
            key = load_pem_public_key(response.content)
        except ValueError as exc:
            raise Unavailable("the CI's signing key does not read") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise Unavailable("the CI's signing key is not an ed25519 key")
        self._key = key
        return key

    async def _lookup(self, as_: Caller, org: str, repo: str) -> dict[str, Any]:
        return json_of(
            await self._ci.call(as_, "GET", f"/api/repos/lookup/{segment(org)}/{segment(repo)}")
        )


def _verify_with(key: Ed25519PublicKey, request: InboundRequest, now: datetime) -> None:
    signatures.verify(
        key,
        method=request.method,
        target=request.target,
        headers=request.headers,
        body=request.body,
        now=now,
    )


def _owner_and_name(repo: Mapping[str, Any]) -> tuple[str, str]:
    owner, name = repo.get("owner"), repo.get("name")
    if isinstance(owner, str) and isinstance(name, str) and owner and name:
        return owner, name
    full = repo.get("full_name")
    if isinstance(full, str) and full.count("/") == 1:
        owner, name = full.split("/")
        return owner, name
    raise Rejected("the CI's request names no repository")


def _remote(clone_url: str, owner: str, repo: str) -> str:
    """Where a machine clones `owner/repo` from: the task's clone URL the CI
    sent, at another repository of the same host.
    """
    parts = urlsplit(clone_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise Rejected("the CI's clone URL is not one this package reads")
    segments = parts.path.rstrip("/").split("/")
    if len(segments) < 3:
        raise Rejected("the CI's clone URL names no repository")
    path = "/".join([*segments[:-2], owner, f"{repo}.git"])
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _labels(compute: str) -> dict[str, str]:
    name, separator, value = compute.partition(":")
    if not separator or not name or not value:
        raise Rejected(f"the label {compute!r} is not a name and a value")
    return {name: value}


def lfs_cache_volume(org: str) -> str:
    """The mount of the machine's store of large files for the org's tasks,
    a volume of the org's own. `Rejected` for an org whose name is no
    volume name, which the org name rule never lets through.
    """
    if not VOLUME_PART.fullmatch(org):
        raise Rejected(f"the org {org!r} names no volume")
    return f"{LFS_CACHE_PREFIX}{org}:{LFS_CACHE_PATH}"


def _clone(
    name: str,
    image: str,
    *,
    remote: str,
    sha: str,
    ref: str,
    path: str,
    lfs: bool,
    cache: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "image": image,
        "settings": {"remote": remote, "sha": sha, "ref": ref, "path": path, "lfs": lfs},
        "volumes": [cache],
    }


def _acting_for(as_: AsOrgAccount, org: str) -> AsOrgAccount:
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")
    return as_


def _parse_run(run: RunId) -> tuple[int, int]:
    repo_id, number = run.split("/", 1)
    return int(repo_id), int(number)
