"""The grading area over Woodpecker: activating a task's repository once,
deactivating it again, and starting runs as the org account the caller
hands in, finding a run by its grading id, reading and cancelling runs as
the CI's administrator, the configuration extension's signed question and
the answer to it, and the org account's own user and token at the CI, made
and deleted by the administrator and signed in by the sign-in dance.

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
  clone image with `remote`, `sha`, `ref` and `path` set, `lfs` on for the
  task alone, and the machine's store of large files of the task's org
  mounted as `unicon-lfs-<org>:/lfs-cache`, one store per org, so no org's
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

The extension's request is signed by the CI's ed25519 key (RFC 9421,
`signatures.py`). The key is read from `GET /api/signature/public-key` with
the administrator's token and kept; a request that does not verify has the
key read again once, in case the CI's key changed. The key is asked for at
most once a minute, whether the last read worked or not, so requests that
do not verify never make the platform call the CI more often than that.
"""

import json
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.grading import (
    CiAnswer,
    CiRequest,
    ConfigAsk,
    GradingRun,
    RunPlaces,
)
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount, Identity
from forge.domain.ids import RunId, TaskId
from forge.forges.forgejo import signatures
from forge.forges.forgejo.ci_login import CiLogin
from forge.forges.forgejo.http import Http, json_of
from forge.forges.forgejo.repos import DEFAULT_BRANCH, Repos
from forge.forges.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    MalformedId,
    parse_publication,
    parse_submission,
    parse_task,
    task_of_repo,
)
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


class WoodpeckerGrading:
    def __init__(
        self, forge_http: Http, ci: Http, repos: Repos, *, ci_public_url: str, login: CiLogin
    ) -> None:
        self._forge = forge_http
        self._ci = ci
        self._repos = repos
        self._ci_public_url = ci_public_url.rstrip("/")
        self._login = login
        self._key: Ed25519PublicKey | None = None
        self._key_read_at = -KEY_REFETCH_SECONDS

    async def activate(self, as_: AsOrgAccount, task: TaskId) -> None:
        ref = parse_task(task)
        account = _acting_for(as_, ref.org)
        record = await self._repos.record(ref.org, ref.repo)
        try:
            activated = json_of(
                await self._ci.call(
                    account, "POST", "/api/repos", params={"forge_remote_id": int(record["id"])}
                )
            )
        except Conflict:
            activated = await self._lookup(account, ref.org, ref.repo)
        await self._ci.call(
            CI_ADMIN,
            "PATCH",
            f"/api/repos/{activated['id']}",
            json={"trusted": {"network": False, "volumes": True, "security": False}},
        )
        await self._delete_ci_webhooks(ref.org, ref.repo)

    async def deactivate(self, as_: AsOrgAccount, task: TaskId) -> None:
        """As the org account, which activated the repository: Woodpecker
        asks the forge to drop its webhook with the credential of whoever
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

    def run_variables(self, run: GradingRun) -> Mapping[str, str]:
        workspace, task, _ = parse_submission(run.submission)
        return {
            GRADING_VARIABLE: str(run.grading),
            ENVELOPE_VARIABLE: run.envelope_url,
            PUBLICATION_VARIABLE: str(run.publication_version),
            SUBMISSION_REPO_VARIABLE: f"{workspace.org}/{workspace.submission_repo(task)}",
            SUBMISSION_VARIABLE: str(run.submission_version),
            COMPUTE_VARIABLE: run.compute,
        }

    async def start_run(self, as_: AsOrgAccount, run: GradingRun) -> RunId:
        ref = parse_task(run.task)
        account = _acting_for(as_, ref.org)
        repo = await self._lookup(account, ref.org, ref.repo)
        started = await self._ci.call(
            account,
            "POST",
            f"/api/repos/{repo['id']}/pipelines",
            json={"branch": DEFAULT_BRANCH, "variables": dict(self.run_variables(run))},
        )
        body = started.json() if started.content else None
        if not isinstance(body, dict) or not isinstance(body.get("number"), int):
            raise Rejected(f"the CI answered {started.status_code} without a run")
        return RunId(f"{repo['id']}/{body['number']}")

    async def cancel_run(self, run: RunId) -> None:
        repo_id, number = _parse_run(run)
        await self._ci.call(CI_ADMIN, "POST", f"/api/repos/{repo_id}/pipelines/{number}/cancel")

    async def read_config_request(self, request: CiRequest, *, now: datetime) -> ConfigAsk:
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
        return ConfigAsk(
            task=task.id,
            grading=variables.get(GRADING_VARIABLE),
            variables=dict(variables),
            clone_url=clone_url,
        )

    def config_answer(
        self, run: GradingRun, ask: ConfigAsk, *, harness_image: str, clone_image: str
    ) -> CiAnswer:
        places = self.run_places(run)
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
                    "image": harness_image,
                    "environment": {"DOCKER_HOST": FILTER_SOCKET},
                    "volumes": [FILTER_VOLUME],
                }
            ],
        }
        data = yaml.safe_dump(workflow, sort_keys=False, default_flow_style=False)
        body = {"configs": [{"name": WORKFLOW_NAME, "data": data}]}
        return CiAnswer(body=json.dumps(body).encode(), content_type="application/json")

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

    async def create_ci_user(self, username: str) -> int:
        """Looked up before it is made: Woodpecker answers a duplicate with a
        server error, which the client would take for a CI that is down.
        """
        try:
            found = json_of(await self._ci.call(CI_ADMIN, "GET", f"/api/users/{username}"))
        except NotFound:
            found = json_of(
                await self._ci.call(CI_ADMIN, "POST", "/api/users", json={"login": username})
            )
        return int(found["id"])

    async def delete_ci_user(self, username: str) -> None:
        """Woodpecker deletes a user's own org along with it, and when that
        org is gone already it answers 404 and keeps the user (measured on
        3.18.1), so a 404 is believed only once the user reads as gone too.
        """
        path = f"/api/users/{username}"
        try:
            await self._ci.call(CI_ADMIN, "DELETE", path)
        except NotFound:
            try:
                await self._ci.call(CI_ADMIN, "GET", path)
            except NotFound:
                return
            raise Rejected(f"the CI answered 404 to deleting {username} and kept it") from None

    async def mint_ci_token(self, username: str, forge_password: str) -> str:
        return await self._login.mint_token(username, forge_password)

    async def _verify(self, request: CiRequest, now: datetime) -> None:
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
        response = await self._ci.call(CI_ADMIN, "GET", PUBLIC_KEY_PATH)
        try:
            key = load_pem_public_key(response.content)
        except ValueError as exc:
            raise Unavailable("the CI's signing key does not read") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise Unavailable("the CI's signing key is not an ed25519 key")
        self._key = key
        return key

    async def _lookup(self, as_: Identity, org: str, repo: str) -> dict[str, Any]:
        return json_of(await self._ci.call(as_, "GET", f"/api/repos/lookup/{org}/{repo}"))

    async def _delete_ci_webhooks(self, org: str, repo: str) -> None:
        for hook in await self._forge.get_all(PLATFORM, f"/api/v1/repos/{org}/{repo}/hooks"):
            url = str((hook.get("config") or {}).get("url", ""))
            if url.startswith(self._ci_public_url):
                await self._forge.call(
                    PLATFORM, "DELETE", f"/api/v1/repos/{org}/{repo}/hooks/{hook['id']}"
                )


def _verify_with(key: Ed25519PublicKey, request: CiRequest, now: datetime) -> None:
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
