"""One grading run from its queued row to its verdict on the real stack: the
CI, a grading machine with its socket filter, and the harness and primitive
images of the local registry. The test process stands in for the backend's
three machine routes, serving each through the package's own actions, and
points the task repository's configuration extension at itself, which is a
setting of that one repository the test makes and removes; the stack is
left as it is.

The CI asks the extension what the run is, signed with its own key; the
answer passes the CI's checks and the run is started with it, which the test
asserts in any case. Then the run checks the task out at its publication and
the submission at its commit, and the harness fetches the envelope, runs the
plan one sandboxed container per step, writes its log and posts the verdict.
The checkouts clone from the host of the repository's clone URL, the forge's
public URL, which is also the host the CI lends its credential for; on a
stack whose public URL a step container cannot reach, such as one under
`.localhost`, which inside a container is the container itself, the task
checkout fails, and the test says so and stops there.

It needs `UNICON_LIVE_MACHINE_HOST`, the name the CI and a step container
reach this machine by (`host.docker.internal` on Docker Desktop), and the
images the stack's `.env` names in `UNICON_LIVE_HARNESS_IMAGE` and
`UNICON_LIVE_CLONE_IMAGE`, besides what the other live tests need.
"""

import asyncio
import base64
import dataclasses
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.contracts import violation
from forge.domain.errors import UniconError
from forge.domain.grading import CiRequest, GradingStatus
from forge.domain.ids import ContestId, OrgName, TaskId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.forges.forgejo import ForgejoForge
from forge.forges.forgejo.objects import StorageConfig
from forge.runtime.setup import Setup
from forge.services import (
    access,
    contests,
    files,
    gradings,
    orgs,
    published,
    runs,
    sessions,
    tasks,
)
from forge.services.publications import Published
from forge.settings import Settings
from forge.testing import APP_URL, CALLBACK_PATH, tick
from tests.live.conftest import (
    FORGE_PUBLIC_URL,
    LIVE,
    as_person,
    credential_of,
    delete_org,
    delete_user,
    forge_config,
    make_user,
    needs_ci,
)

MACHINE_HOST = os.environ.get("UNICON_LIVE_MACHINE_HOST")
HARNESS_IMAGE = os.environ.get("UNICON_LIVE_HARNESS_IMAGE")
CLONE_IMAGE = os.environ.get("UNICON_LIVE_CLONE_IMAGE")
S3_ENDPOINT = os.environ.get("UNICON_LIVE_S3_ENDPOINT")
S3_ACCESS_KEY = os.environ.get("UNICON_LIVE_S3_ACCESS_KEY")
S3_SECRET_KEY = os.environ.get("UNICON_LIVE_S3_SECRET_KEY")
SOURCE = b"print(sum(map(int, input().split())))\n"
STATUSES = {
    "CiRequestRefused": 403,
    "NotFound": 404,
    "GradingClosed": 410,
    "InvalidToken": 401,
    "InvalidCallback": 400,
}

pytestmark = [
    *LIVE,
    needs_ci,
    pytest.mark.skipif(
        not (MACHINE_HOST and HARNESS_IMAGE and CLONE_IMAGE and S3_ENDPOINT),
        reason="no grading machine, images or object store configured",
    ),
]


class Platform:
    """The backend's three machine routes, served from a thread through the
    package's actions on the test's own event loop, and a store for the log.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.setup: Setup | None = None
        self.seen: list[tuple[str, str, int]] = []
        self.logs: dict[str, bytes] = {}
        self.answers: list[bytes] = []
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), self._handler())
        self.port = self.server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{MACHINE_HOST}:{self.port}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        platform = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def do_GET(self) -> None:
                self._answer("GET")

            def do_POST(self) -> None:
                self._answer("POST")

            def do_PUT(self) -> None:
                self._answer("PUT")

            def _answer(self, method: str) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                status, content, kind = platform.handle(
                    method, self.path, dict(self.headers.items()), body
                )
                platform.seen.append((method, urlsplit(self.path).path, status))
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        return Handler

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes, str]:
        assert self.setup is not None
        setup = self.setup
        path = urlsplit(target).path
        parts = path.strip("/").split("/")
        if method == "PUT" and parts[0] == "unicon-results":
            self.logs[path] = body
            return 200, b"", "text/plain"
        try:
            if method == "POST" and path == gradings.CI_CONFIG_PATH:
                answer = self._run(runs.config(setup, CiRequest(method, target, headers, body)))
                self.answers.append(answer.body)
                return 200, answer.body, answer.content_type
            grading = uuid.UUID(parts[3])
            if method == "GET" and parts[4] == "envelope":
                key = parse_qs(urlsplit(target).query).get("key", [""])[0]
                document = self._run(runs.envelope(setup, grading, key))
                return 200, json.dumps(document).encode(), "application/json"
            if method == "POST" and parts[4] == "callback":
                authorization = headers.get("Authorization")
                status = self._run(runs.callback(setup, grading, authorization, body))
                return 200, json.dumps({"status": status}).encode(), "application/json"
        except UniconError as exc:
            return STATUSES.get(type(exc).__name__, 500), exc.code.encode(), "text/plain"
        return 404, b"", "text/plain"

    def _run(self, work: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(work, self.loop).result(timeout=120)


@pytest.fixture
async def platform() -> AsyncIterator[Platform]:
    served = Platform(asyncio.get_running_loop())
    thread = threading.Thread(target=served.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield served
    finally:
        served.server.shutdown()


@pytest.fixture
async def grading_setup(
    migrated_database_url: str, admin: httpx.Client, platform: Platform
) -> AsyncIterator[Setup]:
    assert HARNESS_IMAGE and CLONE_IMAGE and S3_ENDPOINT and S3_ACCESS_KEY and S3_SECRET_KEY
    settings = Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        machine_url=platform.url,
        forge_public_url=FORGE_PUBLIC_URL,
        internal_url="http://backend:8000",
        harness_image=HARNESS_IMAGE,
        clone_image=CLONE_IMAGE,
    )
    config = forge_config(admin)
    storage = StorageConfig(
        endpoint=S3_ENDPOINT,
        region="garage",
        access_key=S3_ACCESS_KEY,
        secret_key=S3_SECRET_KEY,
        uploads_bucket="unicon-uploads",
        results_bucket="unicon-results",
        public_url=APP_URL,
        machine_url=platform.url,
    )
    built = Setup.build(
        settings,
        callback_path=CALLBACK_PATH,
        forge=ForgejoForge(dataclasses.replace(config, storage=storage)),
    )
    platform.setup = built
    try:
        yield built
    finally:
        await built.stop()


@pytest.fixture
def run_org(admin: httpx.Client, ci: httpx.Client, stamp: str) -> Iterator[str]:
    name = f"live-run-{stamp}"
    yield name
    found = ci.get(f"/api/repos/lookup/{name}/spring.sum.task")
    if found.status_code == 200:
        ci.delete(f"/api/repos/{found.json()['id']}", params={"remove": "true"})
    found = ci.get(f"/api/orgs/lookup/{name}")
    if found.status_code == 200 and found.json().get("id"):
        ci.delete(f"/api/orgs/{found.json()['id']}")
    ci.delete(f"/api/users/unicon-ci-{name}")
    delete_org(admin, name)
    delete_user(admin, f"unicon-ci-{name}")


@pytest.fixture
def people(admin: httpx.Client, stamp: str) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    organiser, contestant = (
        make_user(admin, f"live-org-{stamp}"),
        make_user(admin, f"live-c-{stamp}"),
    )
    yield organiser, contestant
    delete_user(admin, organiser["login"])
    delete_user(admin, contestant["login"])


async def test_a_run_goes_from_its_queued_row_to_its_verdict(
    grading_setup: Setup,
    platform: Platform,
    admin: httpx.Client,
    ci: httpx.Client,
    run_org: str,
    people: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    setup = grading_setup
    person, contestant = people
    async with setup.unit_of_work() as ctx:
        session = await sessions.create(
            ctx,
            user=await ctx.forge.identity.find_user(int(person["id"])),
            credential=credential_of(admin, str(person["login"])),
            ip=None,
            user_agent=None,
        )
    await orgs.create(setup, session, OrgName(run_org), description="Live grading run")
    await tick(setup, "provisioning")
    organiser = await access.organiser(setup, session, Scope(run_org), Role.MANAGER)
    await contests.create(setup, organiser, OrgName(run_org), "spring", title="Spring")
    await tick(setup, "provisioning")
    contest = ContestId(f"{run_org}/spring")
    await tasks.create(setup, organiser, contest, "sum", title="Sum")
    await tick(setup, "provisioning")
    task = TaskId(f"{run_org}/spring/sum")
    starter = await files.read(setup, organiser, task, "task.yaml")
    saved = await files.write(
        setup, organiser, task, "task.yaml", starter.content + b"\n", starter.token
    )
    assert isinstance(saved, Published) and saved.activation == "done", saved

    forge = setup.forge
    login = str(contestant["login"])
    workspace = await forge.workspaces.open_workspace(contest, UserOwner(login), [contestant["id"]])
    await forge.workspaces.open_submission_place(workspace, task, [contestant["id"]])
    document = {
        "schema_version": 3,
        "inputs": {"submission": {"files": ["files/submission/main.py"], "language": "python"}},
    }
    submitted = await forge.workspaces.record_submission(
        as_person(admin, contestant),
        workspace,
        task,
        {"submission.json": json.dumps(document).encode(), "files/submission/main.py": SOURCE},
        key="live-key-0001",
    )
    repo = ci.get(f"/api/repos/lookup/{run_org}/spring.sum.task").json()
    patched = ci.patch(
        f"/api/repos/{repo['id']}",
        json={
            "config_extension_endpoint": f"{platform.url}{gradings.CI_CONFIG_PATH}",
            "config_extension_exclusive": True,
        },
    )
    assert patched.status_code == 200, patched.text
    [publication] = await forge.workspaces.list_publications(task)
    async with setup.unit_of_work() as ctx:
        current = await published.task(ctx, task)
        assert current is not None
        [row] = gradings.queue_submission(
            ctx,
            task=task,
            workspace=workspace,
            submission=submitted,
            publication=current.publication,
            definition=current.definition,
            key="live-key-0001",
            at=ctx.now,
        )
        grading = row.id
    assert publication.id == current.publication.id

    await tick(setup, "gradings.dispatch")

    found = await _row(setup, grading)
    assert ("POST", gradings.CI_CONFIG_PATH, 200) in platform.seen, platform.seen
    assert found.status == GradingStatus.DISPATCHED, (found.status, found.wait_reason)
    for _ in range(100):
        state = _run(ci, found)
        if GradingStatus(found.status) in (GradingStatus.DONE, GradingStatus.SYSTEM_ERROR):
            break
        if state["status"] in ("failure", "error", "killed", "success"):
            await asyncio.sleep(3)
            found = await _row(setup, grading)
            break
        await asyncio.sleep(3)
        found = await _row(setup, grading)
    assert [error for error in state["errors"] or [] if not error["is_warning"]] == []
    checkout = dict(zip((step[0] for step in state["steps"]), state["steps"], strict=True))
    if checkout.get("task", ("task", "", 0))[1] == "failure":
        pytest.skip(f"the task checkout could not reach the forge: {state['logs']['task'][-2:]}")
    assert found.status == GradingStatus.DONE, (found.error, found.verdict, state)
    assert found.verdict is not None
    assert violation(found.verdict, "verdict") is None
    assert found.verdict["outcome"] == "accepted"
    assert found.log_key == f"logs/{grading}/1.log"
    assert any(path.endswith(f"/logs/{grading}/1.log") for path in platform.logs)


async def _row(setup: Setup, grading: uuid.UUID) -> Grading:
    async with setup.unit_of_work() as ctx:
        return (await ctx.db.execute(select(Grading).where(Grading.id == grading))).scalar_one()


def _run(ci: httpx.Client, row: Grading) -> dict[str, Any]:
    """What the CI says of the grading's run: its status, what its checks
    found, and each step's state and the end of its log.
    """
    assert row.run_id is not None
    repo, number = row.run_id.split("/")
    pipeline = ci.get(f"/api/repos/{repo}/pipelines/{number}").json()
    steps = [
        step
        for workflow in pipeline.get("workflows") or []
        for step in workflow.get("children") or []
    ]
    logs = {
        step["name"]: [
            base64.b64decode(line.get("data") or "").decode(errors="replace")
            for line in ci.get(f"/api/repos/{repo}/logs/{number}/{step['id']}").json() or []
        ][-8:]
        for step in steps
    }
    return {
        "status": pipeline.get("status"),
        "errors": pipeline.get("errors"),
        "steps": [(step["name"], step.get("state"), step.get("exit_code")) for step in steps],
        "logs": logs,
    }
