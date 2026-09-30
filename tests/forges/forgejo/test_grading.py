"""The grading area over Woodpecker, asserted without one: a task activated
once and its runs started as the org account with the run's variables, a
start answered without a run refused, and a run found by its grading id.
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from forge.domain.errors import Forbidden, Rejected
from forge.domain.grading import GradingRun, Run, RunStatus
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import PublicationId, RunId, SubmissionId, TaskId, VersionId
from forge.forges.forgejo import ForgejoForge
from tests.forges.forgejo.conftest import Recorder, ok

ACME = AsOrgAccount("acme", forge_token="forge-acme", ci_token="ci-acme")
GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TASK_COMMIT = "9c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d"
SUBMISSION_COMMIT = "6f1b0d0c4d2a9e8f7b6a5c4d3e2f1a0b9c8d7e6f"

RUN = GradingRun(
    grading=GRADING,
    task=TaskId("acme/spring/sum"),
    publication=PublicationId("acme/spring/sum#3"),
    publication_version=VersionId(TASK_COMMIT),
    submission=SubmissionId("acme/spring/@bob/sum#2"),
    submission_version=VersionId(SUBMISSION_COMMIT),
    envelope_url=f"http://proxy/api/v1/gradings/{GRADING}/envelope?key=k",
    compute="pool:platform",
)

VARIABLES = {
    "UNICON_GRADING_ID": str(GRADING),
    "UNICON_ENVELOPE_URL": RUN.envelope_url,
    "UNICON_PUBLICATION_COMMIT": TASK_COMMIT,
    "UNICON_SUBMISSION_REPO": "acme/spring.sum.bob.sub",
    "UNICON_SUBMISSION_COMMIT": SUBMISSION_COMMIT,
    "UNICON_COMPUTE": "pool:platform",
}


async def test_a_task_is_activated_once_as_the_org_account(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/repos/acme/spring.sum.task", ok({"id": 55}))
    recorder.on("POST", "/api/repos", ok({"id": 5}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.sum.task/hooks",
        ok(
            [
                {"id": 1, "config": {"url": "http://ci.test/api/hook"}},
                {"id": 2, "config": {"url": "http://backend/x"}},
            ]
        ),
    )

    await forgejo.grading.activate(ACME, TaskId("acme/spring/sum"))

    assert recorder.headers("POST", "/api/repos") == ["Bearer ci-acme"]
    assert recorder.sent("PATCH", "/api/repos/5") == [
        {"trusted": {"network": False, "volumes": True, "security": False}}
    ]
    assert "DELETE /api/v1/repos/acme/spring.sum.task/hooks/1" in recorder.calls()
    assert "DELETE /api/v1/repos/acme/spring.sum.task/hooks/2" not in recorder.calls()


async def test_a_run_is_started_on_main_as_the_org_account_with_its_variables(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/repos/lookup/acme/spring.sum.task", ok({"id": 5}))
    recorder.on("POST", "/api/repos/5/pipelines", ok({"number": 3, "status": "pending"}))

    run = await forgejo.grading.start_run(ACME, RUN)

    assert run == "5/3"
    assert recorder.headers("POST", "/api/repos/5/pipelines") == ["Bearer ci-acme"]
    assert recorder.sent("POST", "/api/repos/5/pipelines") == [
        {"branch": "main", "variables": VARIABLES}
    ]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(204), id="204-empty"),
        pytest.param(ok({}), id="200-without-a-number"),
    ],
)
async def test_a_start_answered_without_a_run_is_rejected(
    forgejo: ForgejoForge, recorder: Recorder, answer: httpx.Response
) -> None:
    recorder.on("GET", "/api/repos/lookup/acme/spring.sum.task", ok({"id": 5}))
    recorder.on("POST", "/api/repos/5/pipelines", answer)

    with pytest.raises(Rejected):
        await forgejo.grading.start_run(ACME, RUN)


async def test_a_start_for_another_org_is_forbidden(forgejo: ForgejoForge) -> None:
    other = AsOrgAccount("other", forge_token="f", ci_token="c")

    with pytest.raises(Forbidden):
        await forgejo.grading.start_run(other, RUN)


async def test_a_run_is_found_by_its_grading_id_among_the_runs_since(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/repos/lookup/acme/spring.sum.task", ok({"id": 5}))
    recorder.on(
        "GET",
        "/api/repos/5/pipelines",
        ok(
            [
                {"number": 9, "variables": {"UNICON_GRADING_ID": str(uuid.uuid4())}},
                {"number": 7, "status": "pending", "variables": VARIABLES},
                {"number": 6},
            ]
        ),
    )

    found = await forgejo.grading.find_run(ACME, RUN, since=NOW)

    assert found == Run(id=RunId("5/7"), status=RunStatus.PENDING)
    [listing] = [request for request in recorder.seen if request.url.path.endswith("/pipelines")]
    assert listing.url.params["after"] == "2026-09-30T12:00:00Z"
    assert listing.headers["Authorization"] == "Bearer ci-acme"


async def test_no_run_is_found_when_none_carries_the_grading_id(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/repos/lookup/acme/spring.sum.task", ok({"id": 5}))
    recorder.on("GET", "/api/repos/5/pipelines", ok([{"number": 1, "variables": {}}]))

    assert await forgejo.grading.find_run(ACME, RUN, since=NOW) is None
