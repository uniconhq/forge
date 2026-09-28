"""The grading area over Woodpecker: registering a task's repository once
and starting runs as the org account the caller hands in, reading and
cancelling runs as the CI's administrator.
"""

from collections.abc import Mapping
from typing import Any

from forge.domain.errors import Conflict, Forbidden, Rejected, Unavailable
from forge.domain.grading import Run, RunStatus
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount, Identity
from forge.domain.ids import RunId, TaskId
from forge.forges.forgejo.http import Http, json_of
from forge.forges.forgejo.repos import DEFAULT_BRANCH, Repos
from forge.forges.ids import parse_task
from forge.log import get_logger

log = get_logger(__name__)

COMPUTE_VARIABLE = "UNICON_COMPUTE"
STATUS = {
    "pending": RunStatus.PENDING,
    "blocked": RunStatus.PENDING,
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
    def __init__(self, forge_http: Http, ci: Http, repos: Repos, *, ci_public_url: str) -> None:
        self._forge = forge_http
        self._ci = ci
        self._repos = repos
        self._ci_public_url = ci_public_url.rstrip("/")

    async def register(self, as_: AsOrgAccount, task: TaskId) -> None:
        ref = parse_task(task)
        account = _acting_for(as_, ref.org)
        record = await self._repos.record(ref.org, ref.repo)
        try:
            registered = json_of(
                await self._ci.call(
                    account, "POST", "/api/repos", params={"forge_remote_id": int(record["id"])}
                )
            )
        except Conflict:
            registered = await self._lookup(account, ref.org, ref.repo)
        await self._ci.call(
            CI_ADMIN,
            "PATCH",
            f"/api/repos/{registered['id']}",
            json={"trusted": {"network": False, "volumes": True, "security": False}},
        )
        await self._delete_ci_webhooks(ref.org, ref.repo)

    async def start_run(
        self, as_: AsOrgAccount, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        ref = parse_task(task)
        account = _acting_for(as_, ref.org)
        repo = await self._lookup(account, ref.org, ref.repo)
        started = await self._ci.call(
            account,
            "POST",
            f"/api/repos/{repo['id']}/pipelines",
            json={
                "branch": DEFAULT_BRANCH,
                "variables": {**variables, COMPUTE_VARIABLE: compute_label},
            },
        )
        body = started.json() if started.content else None
        if not isinstance(body, dict) or "number" not in body:
            raise Unavailable("the CI did not return a run")
        return RunId(f"{repo['id']}/{body['number']}")

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

    async def _lookup(self, as_: Identity, org: str, repo: str) -> dict[str, Any]:
        return json_of(await self._ci.call(as_, "GET", f"/api/repos/lookup/{org}/{repo}"))

    async def _delete_ci_webhooks(self, org: str, repo: str) -> None:
        for hook in await self._forge.get_all(PLATFORM, f"/api/v1/repos/{org}/{repo}/hooks"):
            url = str((hook.get("config") or {}).get("url", ""))
            if url.startswith(self._ci_public_url):
                await self._forge.call(
                    PLATFORM, "DELETE", f"/api/v1/repos/{org}/{repo}/hooks/{hook['id']}"
                )


def _acting_for(as_: AsOrgAccount, org: str) -> AsOrgAccount:
    if as_.org != org:
        raise Forbidden(f"the org account of {as_.org} does not act for {org}")
    return as_


def _parse_run(run: RunId) -> tuple[int, int]:
    repo_id, number = run.split("/", 1)
    return int(repo_id), int(number)
