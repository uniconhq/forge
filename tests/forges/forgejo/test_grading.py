"""The grading area over Woodpecker, asserted without one: a task activated
once and its runs started as the org account with the run's variables, a
start answered without a run refused, a run found by its grading id, the
extension's request checked against the CI's key as RFC 9421 lays it out,
and the answer with its three steps.
"""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forge.domain.errors import Forbidden, Rejected, Unavailable
from forge.domain.grading import CiRequest, GradingRun, Run, RunStatus
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import PublicationId, RunId, SubmissionId, TaskId, VersionId
from forge.forges.forgejo import ForgejoForge, grading
from tests.forges.forgejo.conftest import Recorder, ok

ACME = AsOrgAccount("acme", forge_token="forge-acme", ci_token="ci-acme")
GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TASK_COMMIT = "9c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d"
SUBMISSION_COMMIT = "6f1b0d0c4d2a9e8f7b6a5c4d3e2f1a0b9c8d7e6f"
HARNESS = "ghcr.io/uniconhq/harness@sha256:" + "1" * 64
CLONE = "ghcr.io/uniconhq/clone@sha256:" + "2" * 64
TARGET = "/api/v1/ci/config"

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


def _pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _signed(
    key: Ed25519PrivateKey,
    body: bytes,
    *,
    created: datetime = NOW,
    target: str = TARGET,
    sent_body: bytes | None = None,
    expires: datetime | None = None,
) -> CiRequest:
    """A request signed the way Woodpecker signs an extension call, with an
    expiry when one is given.
    """
    digest = "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    parameters = (
        f'("@request-target" "content-digest");created={int(created.timestamp())};alg="ed25519"'
    )
    if expires is not None:
        parameters += f";expires={int(expires.timestamp())}"
    base = (
        f'"@request-target": {target}\n'
        f'"content-digest": {digest}\n'
        f'"@signature-params": {parameters}'
    )
    signature = base64.b64encode(key.sign(base.encode())).decode()
    return CiRequest(
        method="POST",
        target=target,
        headers={
            "Content-Type": "application/json",
            "Content-Digest": digest,
            "Signature-Input": f"woodpecker-ci-extensions={parameters}",
            "Signature": f"woodpecker-ci-extensions=:{signature}:",
        },
        body=body if sent_body is None else sent_body,
    )


def _ask_body(variables: dict[str, str] | None = None) -> bytes:
    return json.dumps(
        {
            "repo": {
                "owner": "acme",
                "name": "spring.sum.task",
                "full_name": "acme/spring.sum.task",
                "clone_url": "http://forgejo:3000/acme/spring.sum.task.git",
                "trusted": {"network": False, "volumes": True, "security": False},
            },
            "pipeline": {"event": "manual", "number": 4, "variables": variables or VARIABLES},
            "netrc": None,
        }
    ).encode()


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


async def test_a_signed_request_reads_as_the_run_it_asks_about(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    ask = await forgejo.grading.read_config_request(_signed(key, _ask_body()), now=NOW)

    assert ask.task == "acme/spring/sum"
    assert ask.grading == str(GRADING)
    assert ask.variables == VARIABLES
    assert ask.clone_url == "http://forgejo:3000/acme/spring.sum.task.git"
    assert recorder.headers("GET", "/api/signature/public-key") == ["Bearer ci-admin"]


async def test_the_key_is_read_once_and_kept(forgejo: ForgejoForge, recorder: Recorder) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    for _ in range(3):
        await forgejo.grading.read_config_request(_signed(key, _ask_body()), now=NOW)

    assert recorder.calls().count("GET /api/signature/public-key") == 1


@pytest.mark.parametrize(
    "request_of",
    [
        pytest.param(lambda key, other: _signed(other, _ask_body()), id="another-key"),
        pytest.param(
            lambda key, other: _signed(key, _ask_body(), sent_body=_ask_body({"A": "1"})),
            id="body-changed",
        ),
        pytest.param(
            lambda key, other: _signed(key, _ask_body(), created=NOW - timedelta(minutes=6)),
            id="stale",
        ),
        pytest.param(
            lambda key, other: _signed(
                key, _ask_body(), created=NOW - timedelta(minutes=1), expires=NOW
            ),
            id="expired",
        ),
        pytest.param(
            lambda key, other: _signed(key, _ask_body(), target="/api/v1/other"),
            id="signed-for-another-target",
        ),
        pytest.param(lambda key, other: CiRequest("POST", TARGET, {}, _ask_body()), id="unsigned"),
    ],
)
async def test_a_request_that_does_not_verify_is_forbidden(
    forgejo: ForgejoForge, recorder: Recorder, request_of: object
) -> None:
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    made = request_of(key, other)  # type: ignore[operator]

    with pytest.raises(Forbidden):
        await forgejo.grading.read_config_request(
            CiRequest(made.method, TARGET, made.headers, made.body), now=NOW
        )


async def test_a_request_that_does_not_verify_has_the_key_read_again_once(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(old), _pem_answer(new))
    await forgejo.grading.read_config_request(_signed(old, _ask_body()), now=NOW)
    forgejo.grading._key_read_at -= 120

    ask = await forgejo.grading.read_config_request(_signed(new, _ask_body()), now=NOW)

    assert ask.grading == str(GRADING)
    assert recorder.calls().count("GET /api/signature/public-key") == 2
    with pytest.raises(Forbidden):
        await forgejo.grading.read_config_request(_signed(old, _ask_body()), now=NOW)
    assert recorder.calls().count("GET /api/signature/public-key") == 2


async def test_a_key_that_could_not_be_read_is_not_asked_for_again_within_a_minute(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    unreadable = httpx.Response(200, content=b"not a key")
    recorder.on("GET", "/api/signature/public-key", unreadable, _pem_answer(key))

    for _ in range(3):
        with pytest.raises(Unavailable):
            await forgejo.grading.read_config_request(_signed(key, _ask_body()), now=NOW)
    assert recorder.calls().count("GET /api/signature/public-key") == 1
    forgejo.grading._key_read_at -= 120
    ask = await forgejo.grading.read_config_request(_signed(key, _ask_body()), now=NOW)

    assert ask.grading == str(GRADING)
    assert recorder.calls().count("GET /api/signature/public-key") == 2


async def test_a_signed_request_about_no_task_is_rejected(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    body = json.dumps(
        {"repo": {"owner": "acme", "name": "spring.contest", "clone_url": "http://f/x.git"}}
    ).encode()

    with pytest.raises(Rejected):
        await forgejo.grading.read_config_request(_signed(key, body), now=NOW)


async def test_the_answer_is_two_full_clone_steps_and_the_harness(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    ask = await forgejo.grading.read_config_request(_signed(key, _ask_body()), now=NOW)

    answer = forgejo.grading.config_answer(RUN, ask, harness_image=HARNESS, clone_image=CLONE)
    again = forgejo.grading.config_answer(RUN, ask, harness_image=HARNESS, clone_image=CLONE)

    assert answer == again
    assert answer.content_type == "application/json"
    [config] = json.loads(answer.body)["configs"]
    assert config["name"] == "grading"
    workflow = yaml.safe_load(config["data"])
    assert workflow == {
        "when": [{"event": "manual"}],
        "labels": {"pool": "platform"},
        "clone": [
            {
                "name": "task",
                "image": CLONE,
                "settings": {
                    "remote": "http://forgejo:3000/acme/spring.sum.task.git",
                    "sha": TASK_COMMIT,
                    "ref": "refs/tags/published/3",
                    "path": "/woodpecker/task",
                    "lfs": True,
                },
                "volumes": ["unicon-lfs-acme:/lfs-cache"],
            },
            {
                "name": "submission",
                "image": CLONE,
                "settings": {
                    "remote": "http://forgejo:3000/acme/spring.sum.bob.sub.git",
                    "sha": SUBMISSION_COMMIT,
                    "ref": "refs/tags/submission/2",
                    "path": "/woodpecker/submission",
                    "lfs": False,
                },
                "volumes": ["unicon-lfs-acme:/lfs-cache"],
            },
        ],
        "steps": [
            {
                "name": "grade",
                "image": HARNESS,
                "environment": {"DOCKER_HOST": "unix:///run/unicon/docker.sock"},
                "volumes": ["unicon-filter:/run/unicon:ro"],
            }
        ],
    }
    assert all("environment" not in step for step in workflow["clone"])


def test_each_org_has_its_own_store_of_large_files() -> None:
    assert grading.lfs_cache_volume("acme") == "unicon-lfs-acme:/lfs-cache"
    assert grading.lfs_cache_volume("beta_2") == "unicon-lfs-beta_2:/lfs-cache"
    for unsafe in ("", "a b", "a/b", "a:b"):
        with pytest.raises(Rejected):
            grading.lfs_cache_volume(unsafe)


def test_the_envelope_places_name_the_task_the_publication_and_the_submission(
    forgejo: ForgejoForge,
) -> None:
    places = forgejo.grading.run_places(RUN)

    assert places.task == {"org": "acme", "repo": "spring.sum.task"}
    assert places.publication == {"tag": "published/3", "commit": TASK_COMMIT}
    assert places.submission == {
        "org": "acme",
        "repo": "spring.sum.bob.sub",
        "tag": "submission/2",
        "commit": SUBMISSION_COMMIT,
    }
    assert places.checkouts == {"task": "/woodpecker/task", "submission": "/woodpecker/submission"}


def _pem_answer(key: Ed25519PrivateKey) -> httpx.Response:
    return httpx.Response(200, content=_pem(key), headers={"Content-Type": "text/plain"})
