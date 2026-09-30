"""The grading area over Woodpecker: activating a task's repository once and
starting runs as the org account the caller hands in, finding a run by its
grading id, reading and cancelling runs as the CI's administrator, and the
org account's own user and token at the CI, made by the administrator and
the sign-in dance.

A run is a manual pipeline on the task's repository, started on `main`,
since the CI starts a run only on a branch, with the run's variables:

    UNICON_GRADING_ID           the grading
    UNICON_ENVELOPE_URL         where the harness fetches its envelope
    UNICON_PUBLICATION_COMMIT   the commit the publication froze
    UNICON_SUBMISSION_REPO      the submission's repository, <org>/<repo>
    UNICON_SUBMISSION_COMMIT    the commit the submission's files went in with
    UNICON_COMPUTE              the label of the machines that may take it
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected
from forge.domain.grading import GradingRun, Run, RunStatus
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount, Identity
from forge.domain.ids import RunId, TaskId
from forge.forges.forgejo.ci_login import CiLogin
from forge.forges.forgejo.http import Http, json_of
from forge.forges.forgejo.repos import DEFAULT_BRANCH, Repos
from forge.forges.ids import parse_submission, parse_task
from forge.log import get_logger

log = get_logger(__name__)

GRADING_VARIABLE = "UNICON_GRADING_ID"
ENVELOPE_VARIABLE = "UNICON_ENVELOPE_URL"
PUBLICATION_VARIABLE = "UNICON_PUBLICATION_COMMIT"
SUBMISSION_REPO_VARIABLE = "UNICON_SUBMISSION_REPO"
SUBMISSION_VARIABLE = "UNICON_SUBMISSION_COMMIT"
COMPUTE_VARIABLE = "UNICON_COMPUTE"

FIND_PAGE_SIZE = 50
FIND_PAGES = 20

STATUS = {
    "pending": RunStatus.PENDING,
    "blocked": RunStatus.PENDING,
    "created": RunStatus.PENDING,
    "running": RunStatus.RUNNING,
    "started": RunStatus.RUNNING,
    "success": RunStatus.SUCCEEDED,
    "failure": RunStatus.FAILED,
    "error": RunStatus.FAILED,
    "killed": RunStatus.CANCELLED,
    "declined": RunStatus.CANCELLED,
    "skipped": RunStatus.CANCELLED,
}


class WoodpeckerGrading:
    def __init__(
        self, forge_http: Http, ci: Http, repos: Repos, *, ci_public_url: str, login: CiLogin
    ) -> None:
        self._forge = forge_http
        self._ci = ci
        self._repos = repos
        self._ci_public_url = ci_public_url.rstrip("/")
        self._login = login

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

    async def find_run(self, as_: AsOrgAccount, run: GradingRun, *, since: datetime) -> Run | None:
        """Woodpecker lists a repository's runs newest first, each with the
        variables it was started with and its status; `after` leaves out the
        ones made before `since`.
        """
        ref = parse_task(run.task)
        account = _acting_for(as_, ref.org)
        repo = await self._lookup(account, ref.org, ref.repo)
        after = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for page in range(1, FIND_PAGES + 1):
            listed = await self._ci.call(
                account,
                "GET",
                f"/api/repos/{repo['id']}/pipelines",
                params={"page": page, "perPage": FIND_PAGE_SIZE, "after": after},
            )
            pipelines = listed.json() if listed.content else []
            if not isinstance(pipelines, list):
                raise Rejected("the CI listed its runs in a shape this package does not read")
            for pipeline in pipelines:
                variables = pipeline.get("variables") or {}
                if variables.get(GRADING_VARIABLE) == str(run.grading):
                    found = RunId(f"{repo['id']}/{pipeline['number']}")
                    return Run(id=found, status=_status(found, pipeline))
            if len(pipelines) < FIND_PAGE_SIZE:
                return None
        return None

    async def read_run(self, run: RunId) -> Run:
        repo_id, number = _parse_run(run)
        state = json_of(
            await self._ci.call(CI_ADMIN, "GET", f"/api/repos/{repo_id}/pipelines/{number}")
        )
        reported = str(state.get("status"))
        if reported not in STATUS:
            log.warning("grading.unknown_status", run=run, status=reported)
            raise Rejected(f"the CI reports a run status this package does not know: {reported}")
        return Run(id=run, status=STATUS[reported])

    async def cancel_run(self, run: RunId) -> None:
        repo_id, number = _parse_run(run)
        await self._ci.call(CI_ADMIN, "POST", f"/api/repos/{repo_id}/pipelines/{number}/cancel")

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

    async def mint_ci_token(self, username: str, forge_password: str) -> str:
        return await self._login.mint_token(username, forge_password)

    async def ci_user_is_alive(self, as_: AsOrgAccount) -> bool:
        try:
            await self._ci.call(as_, "GET", "/api/user")
        except Forbidden:
            return False
        return True

    async def _lookup(self, as_: Identity, org: str, repo: str) -> dict[str, Any]:
        return json_of(await self._ci.call(as_, "GET", f"/api/repos/lookup/{org}/{repo}"))

    async def _delete_ci_webhooks(self, org: str, repo: str) -> None:
        for hook in await self._forge.get_all(PLATFORM, f"/api/v1/repos/{org}/{repo}/hooks"):
            url = str((hook.get("config") or {}).get("url", ""))
            if url.startswith(self._ci_public_url):
                await self._forge.call(
                    PLATFORM, "DELETE", f"/api/v1/repos/{org}/{repo}/hooks/{hook['id']}"
                )


def _status(run: RunId, pipeline: Mapping[str, Any]) -> RunStatus:
    """A listed run's status; one this package does not know is taken as
    still to begin, and read again by the overdue pass.
    """
    reported = str(pipeline.get("status"))
    if reported not in STATUS:
        log.warning("grading.unknown_status", run=run, status=reported)
    return STATUS.get(reported, RunStatus.PENDING)


def _acting_for(as_: AsOrgAccount, org: str) -> AsOrgAccount:
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")
    return as_


def _parse_run(run: RunId) -> tuple[int, int]:
    repo_id, number = run.split("/", 1)
    return int(repo_id), int(number)
