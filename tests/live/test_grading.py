"""Grading against the real Woodpecker: the CI's signing key is read with the
administrator's token and a request it did not sign is refused, and a
queued grading of a published task is started as the org's own account on
`main` with the run's variables, found again by its grading id, and read
and read and cancelled as the CI's administrator. While the CI starts it,
the CI asks the configuration extension of the backend the deployment runs;
a backend that refuses it leaves the CI answering the start with an empty
204 and keeping a run that ended at once, carrying the same variables, and
the grading then waits in the queue with its reason. Everything made is
removed afterwards.
"""

import base64
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select

from forge.adapters.git.forgejo import ForgejoForge
from forge.adapters.git.forgejo.grading import run_variables
from forge.db.tables import Grading
from forge.domain.errors import Forbidden
from forge.domain.grading import GradingRun, GradingStatus, InboundRequest, RunSpec
from forge.domain.ids import (
    ContestId,
    OrgId,
    RunId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
)
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import (
    access,
    contests,
    files,
    gradings,
    orgs,
    sessions,
    tasks,
)
from forge.services.publications import Published
from tests.live.conftest import LIVE, credential_of, delete_org, delete_user, make_user, needs_ci

pytestmark = [*LIVE, needs_ci]


@pytest.fixture
def grading_org(admin: httpx.Client, ci: httpx.Client, stamp: str) -> Iterator[str]:
    """The org's name; everything made under it, at the forge and at the CI,
    is removed afterwards.
    """
    name = f"live-grade-{stamp}"
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
def grader(admin: httpx.Client, stamp: str) -> Iterator[dict[str, Any]]:
    made = make_user(admin, f"live-grader-{stamp}")
    yield made
    delete_user(admin, made["login"])


async def _signed_in(setup: Setup, admin: httpx.Client, person: dict[str, Any]) -> Session:
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx,
            user=await ctx.forge.identity.find_user(int(person["id"])),
            credential=credential_of(admin, str(person["login"])),
            ip=None,
            user_agent=None,
        )


async def _never_looked_up(grading: str | None, task: TaskId) -> tuple[GradingRun, RunSpec]:
    raise AssertionError("a request that does not verify is never looked up")


async def test_the_cis_key_is_read_and_a_request_it_did_not_sign_is_refused(
    forge: ForgejoForge,
) -> None:
    body = b'{"repo": {"owner": "acme", "name": "spring.sum.task"}, "pipeline": {}}'
    digest = "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    now = datetime.now(UTC)
    parameters = (
        f'("@request-target" "content-digest");created={int(now.timestamp())};alg="ed25519"'
    )
    base = (
        f'"@request-target": /api/v1/ci/config\n"content-digest": {digest}\n'
        f'"@signature-params": {parameters}'
    )
    signature = base64.b64encode(Ed25519PrivateKey.generate().sign(base.encode())).decode()
    request = InboundRequest(
        method="POST",
        target="/api/v1/ci/config",
        headers={
            "Content-Digest": digest,
            "Signature-Input": f"woodpecker-ci-extensions={parameters}",
            "Signature": f"woodpecker-ci-extensions=:{signature}:",
        },
        body=body,
    )

    with pytest.raises(Forbidden):
        await forge.grading.answer(request, _never_looked_up, now=now)

    assert isinstance(forge.grading._key, Ed25519PublicKey)


async def test_a_grading_is_started_as_the_org_account_once_its_row_commits(
    live_setup: Setup,
    admin: httpx.Client,
    ci: httpx.Client,
    grading_org: str,
    grader: dict[str, Any],
) -> None:
    session = await _signed_in(live_setup, admin, grader)
    await orgs.create(live_setup, session, OrgId(grading_org), description="Live grading")
    organiser = await access.organiser(live_setup, session, Scope(grading_org), Role.MANAGER)
    await contests.create(live_setup, organiser, OrgId(grading_org), "spring", title="Spring")
    contest = ContestId(f"{grading_org}/spring")
    await tasks.create(live_setup, organiser, contest, "sum", title="Sum")
    task = TaskId(f"{grading_org}/spring/sum")
    starter = await files.read(live_setup, organiser, task, "task.yaml")
    saved = await files.write(
        live_setup,
        organiser,
        task,
        "task.yaml",
        starter.content.replace(b"value: 2.0", b"value: 3.0"),
        starter.token,
    )
    assert isinstance(saved, Published), saved
    workspace = WorkspaceId(f"{grading_org}/spring/@u999999")
    async with live_setup.unit_of_work() as ctx:
        row = gradings.new_row(
            ctx,
            task=task,
            workspace=workspace,
            submission=SubmissionId(f"{workspace}/sum#1"),
            number=1,
            version=VersionId("0" * 40),
            submitted_at=ctx.now,
            publication=saved.publication,
            attempt=1,
            key=None,
        )
        grading = row.id

    async with live_setup.unit_of_work() as ctx:
        after = (await ctx.db.execute(select(Grading).where(Grading.id == grading))).scalar_one()
        run = await gradings.run_of(ctx, after)
        expected = run_variables(run)
    assert expected["UNICON_COMPUTE"] == "pool:platform"
    if after.status == GradingStatus.SYSTEM_ERROR:
        assert (after.error, after.run_id) == (gradings.NO_RUN, None)
        return
    assert after.status == GradingStatus.DISPATCHED, (after.status, after.error)
    assert after.run_id is not None
    repo_id, number = after.run_id.split("/")
    pipeline = ci.get(f"/api/repos/{repo_id}/pipelines/{number}").json()
    assert pipeline["variables"] == expected
    assert pipeline["event"] == "manual" and pipeline["branch"] == "main"
    await live_setup.forge.grading.cancel_run(RunId(after.run_id))
