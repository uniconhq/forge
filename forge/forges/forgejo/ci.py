"""The CI side of the port over Woodpecker: registering a task for grading,
starting, reading and cancelling runs as the org account, and enrolling the
agents that take work on a machine as the CI administrator.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from forge.domain.errors import Conflict, Forbidden, NotFound, Rejected, Unavailable
from forge.domain.grading import Enrolment, Run, RunStatus
from forge.domain.identity import CI_ADMIN, PLATFORM, AsOrgAccount, CiAdmin, Identity
from forge.domain.ids import AgentId, OrgName, RunId, TaskId
from forge.forges.forgejo.base import ForgejoBase, json_of
from forge.forges.forgejo.http import TokenSource, message_of
from forge.forges.forgejo.names import DEFAULT_BRANCH, parse_task

COMPUTE_VARIABLE = "UNICON_COMPUTE"
GLOBAL_AGENT_ORG = -1
RETRIES = 3
BACKOFF_SECONDS = 0.2
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

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


class Woodpecker:
    """The HTTP client for the CI, signing each call for the identity it is
    made under and failing in the port's five ways.
    """

    def __init__(self, client: httpx.AsyncClient, *, admin_token: str, tokens: TokenSource) -> None:
        self._client = client
        self._admin_token = admin_token
        self._tokens = tokens

    async def call(
        self,
        as_: Identity,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token(as_)}"}
        for attempt in range(RETRIES + 1):
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                if attempt == RETRIES:
                    raise Unavailable(f"the CI did not answer: {type(exc).__name__}") from exc
                await asyncio.sleep(BACKOFF_SECONDS * (2**attempt))
                continue
            if response.status_code >= 500:
                if attempt == RETRIES:
                    raise Unavailable(f"the CI answered {response.status_code}")
                await asyncio.sleep(BACKOFF_SECONDS * (2**attempt))
                continue
            if response.is_success:
                return response
            raise _refusal(response)
        raise Unavailable("the CI did not answer")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token(self, as_: Identity) -> str:
        match as_:
            case CiAdmin():
                return self._admin_token
            case AsOrgAccount(org=org):
                return await self._tokens.ci_token(org)
        raise Forbidden("only the CI administrator and org accounts reach the CI")


class CiOps(ForgejoBase):
    _ci: Woodpecker
    _ci_public_url: str

    async def register_for_grading(self, task: TaskId) -> None:
        ref = parse_task(task)
        account = AsOrgAccount(ref.org)
        forge_repo = json_of(
            await self._http.call(PLATFORM, "GET", f"/api/v1/repos/{ref.org}/{ref.repo}")
        )
        try:
            registered = json_of(
                await self._ci.call(
                    account, "POST", "/api/repos", params={"forge_remote_id": int(forge_repo["id"])}
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
        self, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        ref = parse_task(task)
        account = AsOrgAccount(ref.org)
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
        return Run(id=run, status=STATUS.get(str(state.get("status")), RunStatus.PENDING))

    async def cancel_run(self, run: RunId) -> None:
        repo_id, number = _parse_run(run)
        await self._ci.call(CI_ADMIN, "POST", f"/api/repos/{repo_id}/pipelines/{number}/cancel")

    async def enrol_agent(self, org: OrgName | None, label: str) -> Enrolment:
        org_id = GLOBAL_AGENT_ORG if org is None else await self._ci_org_id(org)
        created = json_of(
            await self._ci.call(
                CI_ADMIN, "POST", "/api/agents", json={"name": label, "org_id": org_id}
            )
        )
        return Enrolment(agent=AgentId(str(created["id"])), token=str(created["token"]))

    async def revoke_agent(self, agent: AgentId) -> None:
        await self._ci.call(CI_ADMIN, "DELETE", f"/api/agents/{agent}")

    async def _lookup(self, as_: Identity, org: str, repo: str) -> dict[str, Any]:
        return json_of(await self._ci.call(as_, "GET", f"/api/repos/lookup/{org}/{repo}"))

    async def _ci_org_id(self, org: str) -> int:
        found = json_of(await self._ci.call(CI_ADMIN, "GET", f"/api/orgs/lookup/{org}"))
        return int(found["id"])

    async def _delete_ci_webhooks(self, org: str, repo: str) -> None:
        hooks = await self._http.get_all(PLATFORM, f"/api/v1/repos/{org}/{repo}/hooks")
        for hook in hooks:
            url = str((hook.get("config") or {}).get("url", ""))
            if url.startswith(self._ci_public_url):
                await self._http.call(
                    PLATFORM, "DELETE", f"/api/v1/repos/{org}/{repo}/hooks/{hook['id']}"
                )


def _parse_run(run: RunId) -> tuple[int, int]:
    repo_id, number = run.split("/", 1)
    return int(repo_id), int(number)


def _refusal(response: httpx.Response) -> Exception:
    detail = message_of(response)
    if response.status_code == 404:
        return NotFound(detail or "not found at the CI")
    if response.status_code in (401, 403):
        return Forbidden(detail or "the CI refused this identity")
    if response.status_code == 409:
        return Conflict(detail or "the CI reports a conflict")
    return Rejected(detail or f"the CI answered {response.status_code}")


def new_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=TIMEOUT)
