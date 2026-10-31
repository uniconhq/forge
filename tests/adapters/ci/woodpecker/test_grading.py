"""The grading area over Woodpecker, asserted without one: the org's dispatch
repository activated once as the org account, trusted for volumes alone,
the org account's CI user deleted, a pipeline started on the dispatch
repository with its variables and a start answered without a run refused,
where the CI has a run told by its queue and, for one in no queue, its
pipeline, the extension's request checked against the CI's key as RFC 9421
lays it out, and a pipeline answered with its checkout and each step a
plugin, a save's with no checkout, and nothing for a pipeline started with
other variables.
"""

import base64
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forge.adapters.ci.woodpecker import WoodpeckerCi, grading
from forge.adapters.ci.woodpecker.ci_state import WoodpeckerState, read_state, written
from forge.domain.errors import (
    CiRequestRefused,
    Forbidden,
    Rejected,
    Unavailable,
    VariablesDiffer,
)
from forge.domain.grading import (
    InboundRequest,
    PipelineLookup,
    PipelineRun,
    PipelineSpec,
    PipelineStepSpec,
    RunState,
)
from forge.domain.identity import AsOrgAccount, CiState, OrgAccountRef
from forge.domain.ids import OrgId, RunId, SubmissionId, VersionId
from forge.domain.names import EntrantOwner
from tests.adapters.conftest import Recorder, ok

U8 = EntrantOwner(uuid.UUID(int=8)).segment
"""The workspace segment of the entrant these tests name."""

ACME = AsOrgAccount(
    "acme",
    forge_token="forge-acme",
    ci_state=written(WoodpeckerState(4, "ci-acme", None, account_id=9)),
)
GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TASK_COMMIT = "9c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d"
SUBMISSION_COMMIT = "6f1b0d0c4d2a9e8f7b6a5c4d3e2f1a0b9c8d7e6f"
CLONE = "docker.io/woodpeckerci/plugin-git@sha256:" + "2" * 64
TARGET = "/api/v1/ci/config"


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
) -> InboundRequest:
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
    return InboundRequest(
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


async def test_the_dispatch_repository_is_activated_once_as_the_org_account(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/repos/acme/unicon-dispatch", ok({"id": 56}))
    recorder.on("POST", "/api/repos", ok({"id": 6}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/unicon-dispatch/hooks",
        ok(
            [
                {"id": 1, "config": {"url": "http://ci.test/api/hook"}},
                {"id": 2, "config": {"url": "http://backend/x"}},
            ]
        ),
    )

    assert await woodpecker.grading.activate_dispatch(ACME, OrgId("acme"))

    assert recorder.headers("POST", "/api/repos") == ["Bearer ci-acme"]
    (activated,) = [seen for seen in recorder.seen if seen.url.path == "/api/repos"]
    assert activated.url.params["forge_remote_id"] == "56"
    assert recorder.sent("PATCH", "/api/repos/6") == [
        {"trusted": {"network": False, "volumes": True, "security": False}}
    ]
    assert "DELETE /api/v1/repos/acme/unicon-dispatch/hooks/1" in recorder.calls()
    assert "DELETE /api/v1/repos/acme/unicon-dispatch/hooks/2" not in recorder.calls()
    # Everything asked of the git host went to Forgejo as the platform, and
    # everything else to Woodpecker.
    for seen in recorder.seen:
        at_forge = seen.url.path.startswith("/api/v1/")
        assert seen.url.host == ("forge.internal" if at_forge else "ci.internal")
        if at_forge:
            assert seen.headers["Authorization"] == "token admin"
    with pytest.raises(Forbidden):
        await woodpecker.grading.activate_dispatch(ACME, OrgId("beta"))


async def test_a_dispatch_repository_the_ci_knows_is_activated_again_and_says_so(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/repos/acme/unicon-dispatch", ok({"id": 56}))
    recorder.on("POST", "/api/repos", ok({"message": "exists"}, 409))
    recorder.on("GET", "/api/repos/lookup/acme/unicon-dispatch", ok({"id": 6}))
    recorder.on("GET", "/api/v1/repos/acme/unicon-dispatch/hooks", ok([]))

    assert not await woodpecker.grading.activate_dispatch(ACME, OrgId("acme"))

    assert recorder.sent("PATCH", "/api/repos/6") == [
        {"trusted": {"network": False, "volumes": True, "security": False}}
    ]


async def test_the_ci_user_is_deleted_as_the_administrator(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("DELETE", "/api/users/unicon-ci-acme", httpx.Response(204), ok({}, 404))
    recorder.on("GET", "/api/users/unicon-ci-acme", ok({}, 404))

    await woodpecker.grading.tear_down_org(OrgId("acme"), CiState(""))
    await woodpecker.grading.tear_down_org(OrgId("acme"), ACME.ci_state)

    assert recorder.headers("DELETE", "/api/users/unicon-ci-acme") == ["Bearer ci-admin"] * 2


async def test_a_ci_user_the_ci_keeps_after_a_404_is_refused(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("DELETE", "/api/users/unicon-ci-acme", ok({}, 404))
    recorder.on("GET", "/api/users/unicon-ci-acme", ok({"id": 4}))

    with pytest.raises(Rejected):
        await woodpecker.grading.tear_down_org(OrgId("acme"), CiState(""))


def test_a_sign_in_older_than_two_thirds_of_the_login_lifetime_needs_refreshing(
    woodpecker: WoodpeckerCi,
) -> None:
    lifetime = woodpecker.grading._login_lifetime
    fresh = written(WoodpeckerState(4, "t", NOW - lifetime * 2 / 3))
    stale = written(WoodpeckerState(4, "t", NOW - lifetime * 2 / 3 - timedelta(seconds=1)))

    assert woodpecker.grading.needs_refresh(fresh, NOW) is False
    assert woodpecker.grading.needs_refresh(stale, NOW) is True
    assert woodpecker.grading.needs_refresh(written(WoodpeckerState(4, "t", None)), NOW) is True


async def test_a_refresh_signs_the_account_in_with_a_fresh_password_and_keeps_its_user(
    woodpecker: WoodpeckerCi, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account is found by the id it was made with, never by its name,
    which anyone could have taken.
    """
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{"id": 9, "login": "unicon-ci-acme"}]}))
    recorder.on("PATCH", "/api/v1/admin/users/unicon-ci-acme", ok({}))
    signed_in: list[tuple[str, str]] = []

    async def mint_token(username: str, forge_password: str) -> str:
        signed_in.append((username, forge_password))
        return "fresh"

    monkeypatch.setattr(woodpecker.grading._login, "mint_token", mint_token)

    refreshed = read_state(await woodpecker.grading.refresh(OrgId("acme"), ACME.ci_state))

    [patched] = recorder.sent("PATCH", "/api/v1/admin/users/unicon-ci-acme")
    assert signed_in == [("unicon-ci-acme", patched["password"])]
    assert (refreshed.user_id, refreshed.account_id, refreshed.token) == (4, 9, "fresh")
    assert refreshed.signed_in_at is not None
    searches = [seen for seen in recorder.seen if seen.url.path == "/api/v1/users/search"]
    assert searches and all(seen.url.params["uid"] == "9" for seen in searches)
    assert "GET /api/v1/users/unicon-ci-acme" not in recorder.calls()
    assert {(seen.url.host, seen.headers["Authorization"]) for seen in recorder.seen} == {
        ("forge.internal", "token admin")
    }


async def test_a_refresh_sets_no_password_on_an_account_that_is_not_the_orgs(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{"id": 9, "login": "someone-else"}]}))

    with pytest.raises(Rejected):
        await woodpecker.grading.refresh(OrgId("acme"), ACME.ci_state)
    assert recorder.sent("PATCH", "/api/v1/admin/users/someone-else") == []


async def test_a_refresh_of_a_state_that_names_no_account_is_refused(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    with pytest.raises(Rejected):
        await woodpecker.grading.refresh(OrgId("acme"), written(WoodpeckerState(4, "t", None)))
    assert recorder.calls() == []


PIPELINE = PipelineRun(
    grading=GRADING,
    pipeline="early",
    org=OrgId("acme"),
    publication_version=VersionId(TASK_COMMIT),
    submission=SubmissionId(f"acme/spring/@{U8}/sum#2"),
    submission_version=VersionId(SUBMISSION_COMMIT),
    compute="pool:platform",
)
PIPELINE_VARIABLES = {
    "UNICON_GRADING_ID": str(GRADING),
    "UNICON_PIPELINE": "early",
    "UNICON_PUBLICATION_COMMIT": TASK_COMMIT,
    "UNICON_COMPUTE": "pool:platform",
    "UNICON_SUBMISSION_REPO": f"acme/spring.sum.{U8}.sub",
    "UNICON_SUBMISSION_COMMIT": SUBMISSION_COMMIT,
}
LINK = "ghcr.io/acme/link@sha256:" + "3" * 64
EXEC = "ghcr.io/uniconhq/primitive-exec@sha256:" + "4" * 64
BASE = "ghcr.io/uniconhq/primitive-base@sha256:" + "5" * 64
PIPELINE_SPEC = PipelineSpec(
    clone_image=CLONE,
    steps=(
        PipelineStepSpec("compile", LINK, {"data_url": "u1", "data_token": "t1"}, user=10007),
        PipelineStepSpec(
            "solution", EXEC, {"data_url": "u2", "data_token": "t2"}, ("compile",), cache=True
        ),
        PipelineStepSpec(
            "report",
            BASE,
            {"mode": "report", "data_url": "u3", "data_token": "t3"},
            ("compile", "solution"),
            user=10007,
            always=True,
        ),
    ),
    labels=("pool:platform", "shape:rootless"),
    clones=True,
)


def _dispatch_body(variables: dict[str, str]) -> bytes:
    return json.dumps(
        {
            "repo": {
                "owner": "acme",
                "name": "unicon-dispatch",
                "full_name": "acme/unicon-dispatch",
                "clone_url": "http://forgejo:3000/acme/unicon-dispatch.git",
            },
            "pipeline": {"event": "manual", "number": 7, "variables": variables},
        }
    ).encode()


def _pipelines(
    run: PipelineRun = PIPELINE,
    seen: list[tuple[str | None, OrgId]] | None = None,
    spec: PipelineSpec = PIPELINE_SPEC,
) -> PipelineLookup:
    async def lookup(grading: str | None, org: OrgId) -> tuple[PipelineRun, PipelineSpec]:
        if seen is not None:
            seen.append((grading, org))
        return run, spec

    return lookup


def _asked(variables: dict[str, str] = PIPELINE_VARIABLES) -> bytes:
    return _dispatch_body(variables)


async def test_a_signed_request_is_answered_for_the_pipeline_it_asks_about(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    seen: list[tuple[str | None, OrgId]] = []
    await woodpecker.grading.answer(_signed(key, _asked()), _pipelines(seen=seen), now=NOW)

    assert seen == [(str(GRADING), "acme")]
    assert recorder.headers("GET", "/api/signature/public-key") == ["Bearer ci-admin"]


async def test_the_key_is_read_once_and_kept(woodpecker: WoodpeckerCi, recorder: Recorder) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    for _ in range(3):
        await woodpecker.grading.answer(_signed(key, _asked()), _pipelines(), now=NOW)

    assert recorder.calls().count("GET /api/signature/public-key") == 1


@pytest.mark.parametrize(
    "request_of",
    [
        pytest.param(lambda key, other: _signed(other, _asked()), id="another-key"),
        pytest.param(
            lambda key, other: _signed(key, _asked(), sent_body=_asked({"A": "1"})),
            id="body-changed",
        ),
        pytest.param(
            lambda key, other: _signed(key, _asked(), created=NOW - timedelta(minutes=6)),
            id="stale",
        ),
        pytest.param(
            lambda key, other: _signed(
                key, _asked(), created=NOW - timedelta(minutes=1), expires=NOW
            ),
            id="expired",
        ),
        pytest.param(
            lambda key, other: _signed(key, _asked(), target="/api/v1/other"),
            id="signed-for-another-target",
        ),
        pytest.param(
            lambda key, other: InboundRequest("POST", TARGET, {}, _asked()), id="unsigned"
        ),
    ],
)
async def test_a_request_that_does_not_verify_is_forbidden(
    woodpecker: WoodpeckerCi, recorder: Recorder, request_of: object
) -> None:
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    made = request_of(key, other)  # type: ignore[operator]

    with pytest.raises(Forbidden):
        await woodpecker.grading.answer(
            InboundRequest(made.method, TARGET, made.headers, made.body), _pipelines(), now=NOW
        )


async def test_a_request_that_does_not_verify_has_the_key_read_again_once(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(old), _pem_answer(new))
    await woodpecker.grading.answer(_signed(old, _asked()), _pipelines(), now=NOW)
    woodpecker.grading._key_read_at -= 120

    await woodpecker.grading.answer(_signed(new, _asked()), _pipelines(), now=NOW)

    assert recorder.calls().count("GET /api/signature/public-key") == 2
    with pytest.raises(Forbidden):
        await woodpecker.grading.answer(_signed(old, _asked()), _pipelines(), now=NOW)
    assert recorder.calls().count("GET /api/signature/public-key") == 2


async def test_a_key_that_could_not_be_read_is_not_asked_for_again_within_a_minute(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    unreadable = httpx.Response(200, content=b"not a key")
    recorder.on("GET", "/api/signature/public-key", unreadable, _pem_answer(key))

    for _ in range(3):
        with pytest.raises(Unavailable):
            await woodpecker.grading.answer(_signed(key, _asked()), _pipelines(), now=NOW)
    assert recorder.calls().count("GET /api/signature/public-key") == 1
    woodpecker.grading._key_read_at -= 120
    await woodpecker.grading.answer(_signed(key, _asked()), _pipelines(), now=NOW)

    assert recorder.calls().count("GET /api/signature/public-key") == 2


@pytest.mark.parametrize("name", ["spring.sum.task", "spring.contest"])
async def test_a_signed_request_about_no_dispatch_repository_is_rejected(
    woodpecker: WoodpeckerCi, recorder: Recorder, name: str
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    body = json.dumps(
        {
            "repo": {"owner": "acme", "name": name, "clone_url": "http://f/x.git"},
            "pipeline": {"event": "manual", "variables": PIPELINE_VARIABLES},
        }
    ).encode()

    with pytest.raises(Rejected):
        await woodpecker.grading.answer(_signed(key, body), _pipelines(), now=NOW)


@pytest.mark.parametrize("variable", list(PIPELINE_VARIABLES))
async def test_a_pipeline_started_with_any_other_variable_is_answered_with_nothing(
    woodpecker: WoodpeckerCi, recorder: Recorder, variable: str
) -> None:
    """Rule 1, the CI's half: anyone who may start a manual pipeline on the
    dispatch repository may pass variables of their own. The platform's half
    says the grading is being started; this one refuses the pipeline unless
    every variable is the one it was started with.
    """
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    variables = {**PIPELINE_VARIABLES, variable: PIPELINE_VARIABLES[variable] + "x"}

    with pytest.raises(VariablesDiffer):
        await woodpecker.grading.answer(_signed(key, _asked(variables)), _pipelines(), now=NOW)


async def test_what_the_platform_refuses_is_refused_and_nothing_is_answered(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    async def refused(grading: str | None, org: OrgId) -> tuple[PipelineRun, PipelineSpec]:
        raise CiRequestRefused("not this one")

    with pytest.raises(CiRequestRefused):
        await woodpecker.grading.answer(_signed(key, _asked()), refused, now=NOW)


async def test_a_request_that_does_not_verify_is_never_looked_up(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    seen: list[tuple[str | None, OrgId]] = []

    with pytest.raises(Forbidden):
        await woodpecker.grading.answer(_signed(other, _asked()), _pipelines(seen=seen), now=NOW)
    assert seen == []


async def test_a_pipeline_starts_on_the_dispatch_repository_with_its_variables(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/repos/lookup/acme/unicon-dispatch", ok({"id": 6}))
    recorder.on("POST", "/api/repos/6/pipelines", ok({"number": 3, "status": "pending"}))

    assert await woodpecker.grading.start_pipeline(ACME, PIPELINE) == "6/3"
    assert grading.pipeline_variables(PIPELINE) == PIPELINE_VARIABLES
    assert recorder.headers("POST", "/api/repos/6/pipelines") == ["Bearer ci-acme"]
    assert recorder.sent("POST", "/api/repos/6/pipelines") == [
        {"branch": "main", "variables": PIPELINE_VARIABLES}
    ]
    with pytest.raises(Forbidden):
        await woodpecker.grading.start_pipeline(ACME, replace(PIPELINE, org=OrgId("beta")))


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(204), id="204-empty"),
        pytest.param(ok({}), id="200-without-a-number"),
    ],
)
async def test_a_start_answered_without_a_run_is_rejected(
    woodpecker: WoodpeckerCi, recorder: Recorder, answer: httpx.Response
) -> None:
    recorder.on("GET", "/api/repos/lookup/acme/unicon-dispatch", ok({"id": 6}))
    recorder.on("POST", "/api/repos/6/pipelines", answer)

    with pytest.raises(Rejected):
        await woodpecker.grading.start_pipeline(ACME, PIPELINE)


async def test_the_dispatch_repository_is_activated_trusted_for_volumes_alone(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/repos/acme/unicon-dispatch", ok({"id": 56}))
    recorder.on("POST", "/api/repos", ok({"id": 6}))
    recorder.on("GET", "/api/v1/repos/acme/unicon-dispatch/hooks", ok([]))

    assert await woodpecker.grading.activate_dispatch(ACME, OrgId("acme"))
    assert recorder.sent("PATCH", "/api/repos/6") == [
        {"trusted": {"network": False, "volumes": True, "security": False}}
    ]


async def test_a_pipeline_is_answered_with_its_checkout_and_each_step_a_plugin(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    seen: list[tuple[str | None, OrgId]] = []

    answer = await woodpecker.grading.answer(
        _signed(key, _dispatch_body(PIPELINE_VARIABLES)),
        _pipelines(seen=seen),
        now=NOW,
    )
    again = await woodpecker.grading.answer(
        _signed(key, _dispatch_body(PIPELINE_VARIABLES)), _pipelines(), now=NOW
    )

    assert answer == again and answer.content_type == "application/json"
    assert seen == [(str(GRADING), "acme")]
    [config] = json.loads(answer.body)["configs"]
    assert yaml.safe_load(config["data"]) == {
        "when": [{"event": "manual"}],
        "labels": {"pool": "platform", "shape": "rootless"},
        "clone": [
            {
                "name": "clone",
                "image": CLONE,
                "settings": {
                    "remote": f"http://forgejo:3000/acme/spring.sum.{U8}.sub.git",
                    "sha": SUBMISSION_COMMIT,
                    "ref": "refs/tags/submission/2",
                    "lfs": False,
                },
            }
        ],
        "steps": [
            {
                "name": "compile",
                "image": LINK,
                "settings": {"data_url": "u1", "data_token": "t1"},
                "backend_options": {"docker": {"user": "10007:10007"}},
                "depends_on": [],
            },
            {
                "name": "solution",
                "image": EXEC,
                "settings": {"data_url": "u2", "data_token": "t2"},
                "volumes": ["unicon-cache-acme:/cache"],
                "depends_on": ["compile"],
            },
            {
                "name": "report",
                "image": BASE,
                "settings": {"mode": "report", "data_url": "u3", "data_token": "t3"},
                "backend_options": {"docker": {"user": "10007:10007"}},
                "depends_on": ["compile", "solution"],
                "when": [{"status": ["success", "failure"]}],
            },
        ],
    }


async def test_a_save_pipeline_checks_nothing_out(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))
    save = replace(PIPELINE, pipeline="save", submission=None, submission_version=None)

    answer = await woodpecker.grading.answer(
        _signed(key, _dispatch_body(grading.pipeline_variables(save))),
        _pipelines(save),
        now=NOW,
    )

    workflow = yaml.safe_load(json.loads(answer.body)["configs"][0]["data"])
    assert workflow["skip_clone"] is True and "clone" not in workflow


async def test_a_pipeline_whose_steps_read_no_entrant_input_checks_nothing_out(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    answer = await woodpecker.grading.answer(
        _signed(key, _dispatch_body(grading.pipeline_variables(PIPELINE))),
        _pipelines(spec=replace(PIPELINE_SPEC, clones=False)),
        now=NOW,
    )

    workflow = yaml.safe_load(json.loads(answer.body)["configs"][0]["data"])
    assert workflow["skip_clone"] is True and "clone" not in workflow


@pytest.mark.parametrize(
    "variables",
    [
        pytest.param({**PIPELINE_VARIABLES, "UNICON_PIPELINE": "late"}, id="another-pipeline"),
        pytest.param({**PIPELINE_VARIABLES, "CI_DEBUG": "1"}, id="one-added"),
        pytest.param(
            {k: v for k, v in PIPELINE_VARIABLES.items() if k != "UNICON_COMPUTE"},
            id="one-dropped",
        ),
    ],
)
async def test_a_pipeline_started_with_other_variables_is_answered_with_nothing(
    woodpecker: WoodpeckerCi, recorder: Recorder, variables: dict[str, str]
) -> None:
    key = Ed25519PrivateKey.generate()
    recorder.on("GET", "/api/signature/public-key", _pem_answer(key))

    with pytest.raises(VariablesDiffer):
        await woodpecker.grading.answer(
            _signed(key, _dispatch_body(variables)), _pipelines(), now=NOW
        )


def _pem_answer(key: Ed25519PrivateKey) -> httpx.Response:
    return httpx.Response(200, content=_pem(key), headers={"Content-Type": "text/plain"})


def _queue(**lists: list[tuple[int, int]]) -> httpx.Response:
    return ok(
        {
            name: [
                {"id": str(index), "repo_id": repo, "pipeline_number": number}
                for index, (repo, number) in enumerate(lists.get(name, []))
            ]
            for name in ("pending", "waiting_on_deps", "running")
        }
    )


async def test_a_run_in_the_queue_is_queued_or_taken_without_reading_its_pipeline(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/queue/info", _queue(pending=[(5, 3)], running=[(5, 4)]))

    assert await woodpecker.grading.run_state(RunId("5/3")) is RunState.QUEUED
    assert await woodpecker.grading.run_state(RunId("5/4")) is RunState.TAKEN
    assert recorder.calls() == ["GET /api/queue/info"]
    assert recorder.headers("GET", "/api/queue/info") == ["Bearer ci-admin"]


async def test_a_run_in_no_queue_is_finished_or_lost_by_its_pipeline(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/queue/info", _queue())
    recorder.on(
        "GET",
        "/api/repos/5/pipelines/3",
        ok({"status": "pending", "workflows": [{"id": 9, "state": "pending"}]}),
    )
    recorder.on(
        "GET",
        "/api/repos/5/pipelines/4",
        ok({"status": "running", "workflows": [{"id": 10, "state": "success"}]}),
    )
    recorder.on("GET", "/api/repos/5/pipelines/5", ok({"status": "killed", "workflows": []}))
    recorder.on("GET", "/api/repos/5/pipelines/6", httpx.Response(404))

    assert await woodpecker.grading.run_state(RunId("5/3")) is RunState.LOST
    assert await woodpecker.grading.run_state(RunId("5/4")) is RunState.FINISHED
    assert await woodpecker.grading.run_state(RunId("5/5")) is RunState.FINISHED
    assert await woodpecker.grading.run_state(RunId("5/6")) is RunState.LOST


async def test_the_queue_is_read_at_most_once_in_ten_seconds(
    woodpecker: WoodpeckerCi, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/queue/info", _queue(pending=[(5, 3)]), _queue(running=[(5, 3)]))

    first = await woodpecker.grading.run_state(RunId("5/3"))
    kept = await woodpecker.grading.run_state(RunId("5/3"))
    woodpecker.grading._queue_read_at -= grading.QUEUE_KEPT_SECONDS
    fresh = await woodpecker.grading.run_state(RunId("5/3"))

    assert (first, kept, fresh) == (RunState.QUEUED, RunState.QUEUED, RunState.TAKEN)
    assert recorder.calls() == ["GET /api/queue/info", "GET /api/queue/info"]


@pytest.mark.parametrize(
    "state",
    [
        "",
        "[]",
        '{"user_id": 4, "token": "t"}',
        '{"user_id": "4", "token": "t", "signed_in_at": null}',
        '{"user_id": 4, "token": 7, "signed_in_at": null}',
        '{"user_id": 4, "token": "t", "signed_in_at": "yesterday"}',
        '{"user_id": 4, "token": "t", "signed_in_at": 7}',
        '{"user_id": 4, "token": "t", "signed_in_at": null, "account_id": "9"}',
    ],
)
def test_a_state_this_implementation_did_not_write_is_rejected(state: str) -> None:
    with pytest.raises(Rejected, match="not Woodpecker's"):
        read_state(CiState(state))


async def test_an_org_is_set_up_with_its_user_found_or_made_then_signed_in(
    woodpecker: WoodpeckerCi, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder.on("GET", "/api/users/unicon-ci-acme", ok({}, 404), ok({"id": 4}))
    recorder.on("POST", "/api/users", ok({"id": 4}))
    signed_in: list[tuple[str, str]] = []

    async def mint_token(username: str, forge_password: str) -> str:
        signed_in.append((username, forge_password))
        return f"token-{len(signed_in)}"

    monkeypatch.setattr(woodpecker.grading._login, "mint_token", mint_token)
    account = OrgAccountRef("unicon-ci-acme", 9, "pw-1")

    first = await woodpecker.grading.set_up_org(OrgId("acme"), account)
    again = await woodpecker.grading.set_up_org(OrgId("acme"), account)

    assert recorder.sent("POST", "/api/users") == [{"login": "unicon-ci-acme"}]
    assert recorder.headers("POST", "/api/users") == ["Bearer ci-admin"]
    assert signed_in == [("unicon-ci-acme", "pw-1")] * 2
    assert (read_state(first).user_id, read_state(first).token) == (4, "token-1")
    assert (read_state(again).user_id, read_state(again).token) == (4, "token-2")
