"""The requests each area makes of Forgejo and Woodpecker: the shapes that a
live forge accepts, asserted without one.
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from forge.domain.content import ConflictToken
from forge.domain.errors import Conflict, Forbidden, Misconfigured, Rejected
from forge.domain.identity import PLATFORM, AsUser, Credential
from forge.domain.ids import ContestId, OrgName, TaskId, WorkspaceId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.workflows import Visibility
from forge.forges.forgejo import ForgejoForge
from tests.forges.forgejo.conftest import Recorder, ok

USER = {"id": 7, "login": "ada", "full_name": "Ada", "email": None, "avatar_url": None}


def _credential() -> Credential:
    return Credential("access", "refresh", datetime.now(UTC) + timedelta(hours=1))


async def test_an_org_agent_is_enrolled_under_its_org(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/orgs/lookup/acme", ok({"id": 42}))
    recorder.on("POST", "/api/orgs/42/agents", ok({"id": 9, "token": "t"}))
    recorder.on("POST", "/api/agents", ok({"id": 10, "token": "g"}))

    org_agent = await forgejo.computes.enrol_agent(OrgName("acme"), "box")
    global_agent = await forgejo.computes.enrol_agent(None, "pool")
    await forgejo.computes.revoke_agent(OrgName("acme"), org_agent.agent)

    assert (org_agent.agent, org_agent.token) == ("9", "t")
    assert global_agent.agent == "10"
    assert "POST /api/orgs/42/agents" in recorder.calls()
    assert "DELETE /api/orgs/42/agents/9" in recorder.calls()
    assert recorder.sent("POST", "/api/orgs/42/agents") == [{"name": "box"}]


async def test_a_workspace_is_attached_to_the_contest_roles(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on("GET", "/api/v1/repos/acme/spring.bob.desk/branches/main", ok({}, 404))
    recorder.on("GET", "/api/v1/repos/acme/spring.sum.bob.sub/branches/main", ok({}, 404))
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/teams/search",
        ok(
            {
                "data": [
                    {"id": 1, "name": "acme.spring-admin"},
                    {"id": 2, "name": "acme.spring-manager"},
                    {"id": 3, "name": "acme.spring-observer"},
                ]
            }
        ),
    )

    workspace = await forgejo.workspaces.open_workspace(
        ContestId("acme/spring"), UserOwner("Bob"), [8], [TaskId("acme/spring/sum")]
    )

    assert workspace == WorkspaceId("acme/spring/@bob")
    calls = recorder.calls()
    for repo in ("spring.bob.desk", "spring.sum.bob.sub"):
        assert f"PUT /api/v1/teams/1/repos/acme/{repo}" in calls
        assert f"PUT /api/v1/teams/2/repos/acme/{repo}" in calls
        assert f"PUT /api/v1/repos/acme/{repo}/collaborators/bob" in calls
    assert "POST /api/v1/repos/acme/spring.sum.bob.sub/tag_protections" in calls
    assert "POST /api/v1/repos/acme/spring.bob.desk/tag_protections" not in calls
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.sum.bob.sub/tag_protections") == [
        {"name_pattern": "submission/*", "whitelist_usernames": ["platform-account"]}
    ]
    assert recorder.sent("PUT", "/api/v1/repos/acme/spring.bob.desk/collaborators/bob") == [
        {"permission": "write"}
    ]
    assert recorder.calls().count("POST /api/v1/orgs/acme/repos") == 2
    assert set(recorder.headers("POST", "/api/v1/orgs/acme/repos")) == {"token admin"}


async def test_a_file_is_read_with_a_token_and_written_back_with_it(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    content = base64.b64encode(b"name: Spring\n").decode()
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.contest/contents/contest.yaml",
        ok({"type": "file", "sha": "blob-1", "content": content}),
    )
    recorder.on(
        "PUT",
        "/api/v1/repos/acme/spring.contest/contents/contest.yaml",
        ok({"commit": {"sha": "commit-2"}}),
        ok({"message": "sha does not match"}, 422),
    )
    ada = AsUser(7, _credential())

    read = await forgejo.content.read_file(ada, ContestId("acme/spring"), "contest.yaml")
    written = await forgejo.content.write_file(
        ada, ContestId("acme/spring"), "contest.yaml", b"x", message="Edit", expected=read.token
    )

    assert read.token == ConflictToken("blob-1")
    assert written == "commit-2"
    assert (
        recorder.sent("PUT", "/api/v1/repos/acme/spring.contest/contents/contest.yaml")[0]["sha"]
        == "blob-1"
    )
    assert recorder.headers("PUT", "/api/v1/repos/acme/spring.contest/contents/contest.yaml") == [
        "Bearer access"
    ]
    with pytest.raises(Rejected):
        await forgejo.content.write_file(
            ada, ContestId("acme/spring"), "contest.yaml", b"y", message="Edit", expected=read.token
        )


async def test_creating_a_file_that_exists_is_a_conflict(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.contest/contents/contest.yaml",
        ok({"type": "file", "sha": "blob-1", "content": ""}),
    )
    with pytest.raises(Conflict):
        await forgejo.content.write_file(
            PLATFORM, ContestId("acme/spring"), "contest.yaml", b"y", message="New", expected=None
        )


async def test_roles_are_read_in_one_listing_of_the_users_teams_as_the_user(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/user/teams",
        ok(
            [
                {"id": 1, "name": "acme-admin", "organization": {"username": "acme"}},
                {"id": 2, "name": "acme.spring.sum-observer", "organization": {"username": "acme"}},
                {"id": 3, "name": "acme-ci", "organization": {"username": "acme"}},
                {"id": 4, "name": "Owners", "organization": {"username": "other"}},
            ]
        ),
    )

    grants = await forgejo.orgs.roles_of(AsUser(7, _credential()))

    assert grants == (
        RoleGrant(Scope("acme"), Role.ADMIN),
        RoleGrant(Scope("acme", "spring", "sum"), Role.OBSERVER),
    )
    assert recorder.calls().count("GET /api/v1/user/teams") == 1
    assert recorder.headers("GET", "/api/v1/user/teams") == ["Bearer access"]
    assert "sudo" not in str(recorder.seen[0].url)


async def test_an_org_is_created_limited_with_its_four_teams_and_labels(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("POST", "/api/v1/orgs/acme/teams", ok({"id": 1}))
    recorder.on("GET", "/api/v1/orgs/acme/teams/search", ok({"data": []}))

    await forgejo.orgs.create_org(OrgName("acme"), description="Acme")
    await forgejo.orgs.create_roles(OrgName("acme"))
    await forgejo.orgs.create_thread_labels(OrgName("acme"))

    assert recorder.sent("POST", "/api/v1/orgs")[0]["visibility"] == "limited"
    teams = [body["name"] for body in recorder.sent("POST", "/api/v1/orgs/acme/teams")]
    assert teams == ["acme-admin", "acme-manager", "acme-observer", "acme-ci"]
    assert all(
        body["includes_all_repositories"]
        for body in recorder.sent("POST", "/api/v1/orgs/acme/teams")
    )
    labels = [body["name"] for body in recorder.sent("POST", "/api/v1/orgs/acme/labels")]
    assert labels == ["announcement", "clarification", "answered"]


async def test_roles_and_labels_that_exist_are_kept_on_a_rerun(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/teams/search",
        ok(
            {
                "data": [
                    {"id": 1, "name": "acme-admin"},
                    {"id": 2, "name": "acme-manager"},
                    {"id": 3, "name": "acme-observer"},
                    {"id": 4, "name": "acme-ci"},
                ]
            }
        ),
    )
    recorder.on("GET", "/api/v1/orgs/acme/labels", ok([{"id": 1, "name": "announcement"}]))

    await forgejo.orgs.create_roles(OrgName("acme"))
    await forgejo.orgs.create_thread_labels(OrgName("acme"))

    assert recorder.sent("POST", "/api/v1/orgs/acme/teams") == []
    labels = [body["name"] for body in recorder.sent("POST", "/api/v1/orgs/acme/labels")]
    assert labels == ["clarification", "answered"]


async def test_a_submission_is_written_as_the_contestant_and_named_at_that_commit(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.bob.sub"
    recorder.on("GET", f"{repo}/branches/main", ok({"commit": {"id": "head-0"}}))
    recorder.on("GET", f"{repo}/git/trees/main", ok({"tree": []}))
    recorder.on("GET", f"{repo}/tags", ok([{"name": "submission/1"}, {"name": "v-other"}]))
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-bob"}}))
    bob = AsUser(8, _credential())

    submission = await forgejo.workspaces.record_submission(
        bob, WorkspaceId("acme/spring/@bob"), TaskId("acme/spring/sum"), {"main.py": b"x"}
    )

    assert submission == "acme/spring/@bob/sum#2"
    assert recorder.headers("POST", f"{repo}/contents") == ["Bearer access"]
    assert recorder.sent("POST", f"{repo}/tags") == [
        {"tag_name": "submission/2", "target": "c-bob"}
    ]
    assert recorder.headers("POST", f"{repo}/tags") == ["token admin"]


async def test_a_publication_is_written_and_tagged_by_the_platform_and_nothing_else(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.task"
    recorder.on("GET", f"{repo}/branches/main", ok({"commit": {"id": "head-0"}}))
    recorder.on("GET", f"{repo}/git/trees/main", ok({"tree": []}))
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-plans"}}))
    recorder.on("GET", f"{repo}/tags", ok([{"name": "published/1"}]))

    publication = await forgejo.workspaces.publish(
        TaskId("acme/spring/sum"), {"plans/public.json": b"{}"}
    )

    assert publication == "acme/spring/sum#2"
    assert recorder.sent("POST", f"{repo}/tags") == [
        {"tag_name": "published/2", "target": "c-plans"}
    ]
    assert set(recorder.headers("POST", f"{repo}/contents")) == {"token admin"}
    assert [call for call in recorder.calls() if call.split(" ")[1].startswith("/api/repos")] == []


async def test_a_write_the_host_refuses_as_moved_is_read_again_and_repeated(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.bob.sub"
    recorder.on("GET", f"{repo}/branches/main", ok({"commit": {"id": "head-0"}}))
    recorder.on("GET", f"{repo}/git/trees/main", ok({"tree": []}))
    recorder.on(
        "POST",
        f"{repo}/contents",
        ok({"message": "the tree moved"}, 409),
        ok({"commit": {"sha": "c-2"}}),
    )

    submission = await forgejo.workspaces.record_submission(
        AsUser(8, _credential()),
        WorkspaceId("acme/spring/@bob"),
        TaskId("acme/spring/sum"),
        {"main.py": b"x"},
    )

    assert submission == "acme/spring/@bob/sum#1"
    assert recorder.calls().count(f"POST {repo}/contents") == 2
    assert recorder.calls().count(f"GET {repo}/git/trees/main") == 2
    assert recorder.sent("POST", f"{repo}/tags")[0]["target"] == "c-2"


async def test_a_workflow_under_a_person_is_made_by_the_platform_and_written_by_them(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/orgs/ada", ok({"message": "no such org"}, 404))
    recorder.on("GET", "/api/v1/repos/ada/classic.workflow/branches/main", ok({}, 404))
    recorder.on("POST", "/api/v1/repos/ada/classic.workflow/contents", ok({"commit": {"sha": "c"}}))
    ada = AsUser(7, _credential())

    workflow = await forgejo.workflows.create_workflow(
        ada, "ada", "classic", {"workflow.yaml": b"steps: []"}, Visibility.PRIVATE
    )

    assert workflow == "ada/classic"
    assert recorder.headers("POST", "/api/v1/admin/users/ada/repos") == ["token admin"]
    assert "POST /api/v1/user/repos" not in recorder.calls()
    assert recorder.headers("POST", "/api/v1/repos/ada/classic.workflow/contents") == [
        "Bearer access"
    ]
    assert recorder.headers("POST", "/api/v1/repos/ada/classic.workflow/branch_protections") == [
        "token admin"
    ]
    assert recorder.headers("PUT", "/api/v1/repos/ada/classic.workflow/topics/unicon-workflow") == [
        "Bearer access"
    ]


async def test_a_spent_refresh_is_forbidden_and_a_wrong_registration_is_misconfigured(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "POST",
        "/login/oauth/access_token",
        httpx.Response(400, json={"error": "unauthorized_client", "error_description": "used"}),
        httpx.Response(400, json={"error": "invalid_client"}),
    )
    with pytest.raises(Forbidden):
        await forgejo.identity.refresh_credential(_credential())
    with pytest.raises(Misconfigured):
        await forgejo.identity.refresh_credential(_credential())


async def test_a_sign_in_exchanges_the_code_and_reads_the_nonce(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    claims = base64.urlsafe_b64encode(b'{"nonce":"n-1"}').decode().rstrip("=")
    recorder.on(
        "POST",
        "/login/oauth/access_token",
        ok(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "id_token": f"x.{claims}.y",
            }
        ),
    )
    recorder.on("GET", "/login/oauth/userinfo", ok({"sub": "7", "preferred_username": "ada"}))

    signed = await forgejo.identity.complete_sign_in(code="c", verifier="v")

    assert signed.user.id == 7
    assert signed.nonce == "n-1"
    form = recorder.seen[0].content.decode()
    assert "code_verifier=v" in form and "client_secret=secret" in form
    assert forgejo.identity.sign_up_url() == "http://forge.test/user/sign_up"


async def test_a_run_is_registered_once_and_started_as_the_org_account(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/repos/acme/spring.sum.task", ok({"id": 55}))
    recorder.on("POST", "/api/repos", ok({"id": 5}))
    recorder.on("GET", "/api/repos/lookup/acme/spring.sum.task", ok({"id": 5}))
    recorder.on("POST", "/api/repos/5/pipelines", ok({"number": 3}))
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

    await forgejo.grading.register(TaskId("acme/spring/sum"))
    run = await forgejo.grading.start_run(
        TaskId("acme/spring/sum"), variables={"A": "1"}, compute_label="box"
    )

    assert run == "5/3"
    assert recorder.headers("POST", "/api/repos") == ["Bearer ci-acme"]
    assert recorder.sent("PATCH", "/api/repos/5") == [
        {"trusted": {"network": False, "volumes": True, "security": False}}
    ]
    assert "DELETE /api/v1/repos/acme/spring.sum.task/hooks/1" in recorder.calls()
    assert "DELETE /api/v1/repos/acme/spring.sum.task/hooks/2" not in recorder.calls()
    assert recorder.sent("POST", "/api/repos/5/pipelines") == [
        {"branch": "main", "variables": {"A": "1", "UNICON_COMPUTE": "box"}}
    ]


async def test_a_deleted_person_loses_what_they_own_and_keeps_what_others_read(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [USER]}))
    recorder.on("GET", "/api/v1/users/ada/repos", ok([{"name": "classic.workflow"}]))

    await forgejo.identity.delete_user(7)

    calls = recorder.calls()
    assert calls.index("DELETE /api/v1/repos/ada/classic.workflow") < calls.index(
        "DELETE /api/v1/admin/users/ada"
    )
    deletion = next(
        request for request in recorder.seen if request.url.path == "/api/v1/admin/users/ada"
    )
    assert "purge" not in str(deletion.url)
