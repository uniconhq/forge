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
import dataclasses
import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.errors import Forbidden
from forge.domain.grading import ENDED, CiRequest, GradingStatus, RunStatus
from forge.domain.ids import (
    ContestId,
    OrgName,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
)
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.forges.forgejo import ForgejoForge
from forge.runtime.setup import Setup
from forge.services import (
    access,
    contests,
    dispatch,
    files,
    gradings,
    org_accounts,
    orgs,
    sessions,
    tasks,
)
from forge.services.publications import Published
from forge.testing import tick
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
    request = CiRequest(
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
        await forge.grading.read_config_request(request, now=now)

    assert isinstance(forge.grading._key, Ed25519PublicKey)


async def test_a_queued_grading_is_started_as_the_org_account_and_found_by_its_id(
    live_setup: Setup,
    admin: httpx.Client,
    ci: httpx.Client,
    grading_org: str,
    grader: dict[str, Any],
) -> None:
    session = await _signed_in(live_setup, admin, grader)
    await orgs.create(live_setup, session, OrgName(grading_org), description="Live grading")
    await tick(live_setup, "provisioning")
    organiser = await access.organiser(live_setup, session, Scope(grading_org), Role.MANAGER)
    await contests.create(live_setup, organiser, OrgName(grading_org), "spring", title="Spring")
    await tick(live_setup, "provisioning")
    contest = ContestId(f"{grading_org}/spring")
    await tasks.create(live_setup, organiser, contest, "sum", title="Sum")
    await tick(live_setup, "provisioning")
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
    assert isinstance(saved, Published) and saved.activation == "done", saved
    workspace = WorkspaceId(f"{grading_org}/spring/@nobody")
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
            stage="default",
            attempt=1,
            key=None,
        )
        grading = row.id

    await tick(live_setup, "gradings.dispatch")

    async with live_setup.unit_of_work() as ctx:
        after = (await ctx.db.execute(select(Grading).where(Grading.id == grading))).scalar_one()
        run = await gradings.run_of(ctx, after)
        account = await org_accounts.identity(ctx, OrgName(grading_org))
        expected = dict(ctx.forge.grading.run_variables(run))
    forge = live_setup.forge.grading
    since = after.queued_at - timedelta(minutes=2)
    assert expected["UNICON_COMPUTE"] == "pool:platform"
    assert expected["UNICON_SUBMISSION_REPO"] == f"{grading_org}/spring.sum.nobody.sub"
    assert (
        await forge.find_run(account, dataclasses.replace(run, grading=uuid.uuid4()), since=since)
        is None
    )
    found = await forge.find_run(account, run, since=since)
    assert found is not None
    if after.status == GradingStatus.QUEUED:
        assert (after.wait_reason, after.start_failures) == (dispatch.NO_RUN, 1)
        assert after.retry_at is not None and after.run_id is None
        assert found.status in ENDED
    else:
        assert after.status == GradingStatus.DISPATCHED, (after.status, after.wait_reason)
        assert found.id == after.run_id
    repo_id, number = found.id.split("/")
    pipeline = ci.get(f"/api/repos/{repo_id}/pipelines/{number}").json()
    assert pipeline["variables"] == expected
    assert pipeline["event"] == "manual" and pipeline["branch"] == "main"
    future = datetime.now(UTC) + timedelta(hours=1)
    assert await forge.find_run(account, run, since=future) is None
    state = await forge.read_run(found.id)
    assert state.status == found.status
    if state.status not in ENDED:
        await forge.cancel_run(found.id)
        assert (await forge.read_run(found.id)).status is RunStatus.CANCELLED
