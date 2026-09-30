"""The requests each area makes of Forgejo and Woodpecker: the shapes that a
live forge accepts, asserted without one.
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from forge.domain.content import ConflictToken
from forge.domain.errors import Conflict, Forbidden, Misconfigured, NotFound, Rejected
from forge.domain.identity import PLATFORM, AsOrgAccount, AsUser, Credential
from forge.domain.ids import ContestId, OrgName, TaskId, VersionId, WorkspaceId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.threads import ThreadKind
from forge.domain.workflows import Visibility
from forge.forges.forgejo import ForgejoForge
from tests.forges.forgejo.conftest import Recorder, ok

ACME = AsOrgAccount("acme", forge_token="forge-acme", ci_token="ci-acme")

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
    for repo in ("spring.bob.desk", "spring.sum.bob.sub"):
        recorder.on("GET", f"/api/v1/repos/acme/{repo}", ok({}, 404))
        recorder.on("GET", f"/api/v1/repos/acme/{repo}/branches/main", ok({}, 404))
        recorder.on("GET", f"/api/v1/repos/acme/{repo}/branch_protections/main", ok({}, 404))
        for team in (1, 2, 3):
            recorder.on("GET", f"/api/v1/teams/{team}/repos/acme/{repo}", ok({}, 404))
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
        ContestId("acme/spring"), UserOwner("Bob"), [8]
    )
    await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/spring/sum"), [8])

    assert workspace == WorkspaceId("acme/spring/@bob")
    calls = recorder.calls()
    for repo in ("spring.bob.desk", "spring.sum.bob.sub"):
        assert f"PUT /api/v1/teams/1/repos/acme/{repo}" in calls
        assert f"PUT /api/v1/teams/2/repos/acme/{repo}" in calls
        assert f"PUT /api/v1/repos/acme/{repo}/collaborators/bob" in calls
    assert "POST /api/v1/repos/acme/spring.sum.bob.sub/tag_protections" in calls
    assert "POST /api/v1/repos/acme/spring.bob.desk/tag_protections" not in calls
    assert calls.index("POST /api/v1/repos/acme/spring.sum.bob.sub/tag_protections") < calls.index(
        "PUT /api/v1/repos/acme/spring.sum.bob.sub/collaborators/bob"
    )
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.sum.bob.sub/tag_protections") == [
        {"name_pattern": "submission/*", "whitelist_usernames": ["platform-account"]}
    ]
    assert recorder.sent("PUT", "/api/v1/repos/acme/spring.bob.desk/collaborators/bob") == [
        {"permission": "write"}
    ]
    assert recorder.calls().count("POST /api/v1/orgs/acme/repos") == 2
    assert set(recorder.headers("POST", "/api/v1/orgs/acme/repos")) == {"token admin"}
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.bob.desk/branch_protections") == [
        {
            "branch_name": "main",
            "enable_push": True,
            "enable_force_push": False,
            "block_on_rejected_reviews": False,
        }
    ]


async def test_a_workspace_opened_again_keeps_what_is_there(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on("GET", "/api/v1/repos/acme/spring.bob.desk", ok({"id": 3}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.bob.desk/branch_protections/main",
        ok({"enable_force_push": False}),
    )
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
        ContestId("acme/spring"), UserOwner("bob"), [8]
    )

    calls = recorder.calls()
    assert "POST /api/v1/orgs/acme/repos" not in calls
    assert "POST /api/v1/repos/acme/spring.bob.desk/branch_protections" not in calls
    assert not [call for call in calls if call.startswith("PUT /api/v1/teams/")]
    assert "PUT /api/v1/repos/acme/spring.bob.desk/collaborators/bob" in calls
    with pytest.raises(NotFound):
        await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/autumn/sum"), [8])


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


async def test_a_thread_in_an_org_without_its_labels_is_refused_and_no_label_is_made(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/orgs/acme/labels", ok([{"id": 1, "name": "clarification"}]))

    with pytest.raises(NotFound, match="no label announcement"):
        await forgejo.threads.post_thread(
            PLATFORM, ContestId("acme/spring"), ThreadKind.ANNOUNCEMENT, title="t", body="b"
        )

    assert recorder.sent("POST", "/api/v1/orgs/acme/labels") == []
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.contest/issues") == []


async def test_a_thread_is_posted_with_the_label_of_its_kind(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/orgs/acme/labels", ok([{"id": 4, "name": "announcement"}]))
    recorder.on("POST", "/api/v1/repos/acme/spring.contest/issues", ok({"number": 12}))

    thread = await forgejo.threads.post_thread(
        PLATFORM, ContestId("acme/spring"), ThreadKind.ANNOUNCEMENT, title="t", body="b"
    )

    assert thread == "acme/spring.contest#12"
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.contest/issues") == [
        {"title": "t", "body": "b", "labels": [4]}
    ]


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
    created = "2026-09-30T10:00:00Z"
    first = {"name": "submission/1", "commit": {"sha": "c-1", "created": created}}
    second = {
        "name": "submission/2",
        "message": "idempotency_key: key-12345678\n",
        "commit": {"sha": "c-bob", "created": created},
    }
    recorder.on("GET", f"{repo}/branches/main", ok({"commit": {"id": "head-0"}}))
    tree = [
        {"path": "files/submission/old.py", "sha": "b-old", "type": "blob"},
        {"path": "main.py", "sha": "b-main", "type": "blob"},
    ]
    recorder.on("GET", f"{repo}/git/trees/main", ok({"tree": tree}))
    recorder.on(
        "GET", f"{repo}/tags", ok([first, {"name": "v-other"}]), ok([first, second]), ok([])
    )
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-bob"}}))
    bob = AsUser(8, _credential())

    submission = await forgejo.workspaces.record_submission(
        bob,
        WorkspaceId("acme/spring/@bob"),
        TaskId("acme/spring/sum"),
        {"main.py": b"x"},
        key="key-12345678",
    )

    assert (submission.id, submission.number, submission.version) == (
        "acme/spring/@bob/sum#2",
        2,
        "c-bob",
    )
    assert (submission.key, submission.at) == (
        "key-12345678",
        datetime(2026, 9, 30, 10, tzinfo=UTC),
    )
    assert recorder.headers("POST", f"{repo}/contents") == ["Bearer access"]
    [written] = recorder.sent("POST", f"{repo}/contents")
    assert [(entry["operation"], entry["path"]) for entry in written["files"]] == [
        ("update", "main.py"),
        ("delete", "files/submission/old.py"),
    ]
    assert recorder.sent("POST", f"{repo}/tags") == [
        {
            "tag_name": "submission/2",
            "target": "c-bob",
            "message": "idempotency_key: key-12345678\n",
        }
    ]
    assert recorder.headers("POST", f"{repo}/tags") == ["token admin"]


async def test_a_submission_number_another_took_first_is_taken_by_the_next(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.bob.sub"
    commit = {"sha": "c-bob", "created": "2026-09-30T10:00:00Z"}
    raced = {"name": "submission/1", "commit": {"sha": "c-other", "created": commit["created"]}}
    mine = {"name": "submission/2", "message": "idempotency_key: key-12345678\n", "commit": commit}
    recorder.on("GET", f"{repo}/branches/main", ok({}, 404))
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-bob"}}))
    recorder.on("GET", f"{repo}/tags", ok([]), ok([raced]), ok([raced, mine]))
    recorder.on("POST", f"{repo}/tags", ok({"message": "tag already exists"}, 409), ok({}))

    submission = await forgejo.workspaces.record_submission(
        AsUser(8, _credential()),
        WorkspaceId("acme/spring/@bob"),
        TaskId("acme/spring/sum"),
        {"main.py": b"x"},
        key="key-12345678",
    )

    assert submission.number == 2
    assert [body["tag_name"] for body in recorder.sent("POST", f"{repo}/tags")] == [
        "submission/1",
        "submission/2",
    ]
    [written] = recorder.sent("POST", f"{repo}/contents")
    assert written["new_branch"] == "main"


async def test_the_submissions_are_listed_with_their_keys_and_a_file_is_read_through_media(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.bob.sub"
    commit = {"sha": "c-2", "created": "2026-09-30T10:00:00Z"}
    recorder.on(
        "GET",
        f"{repo}/tags",
        ok(
            [
                {
                    "name": "submission/2",
                    "message": "idempotency_key: k-2222222\n",
                    "commit": commit,
                },
                {"name": "scratch", "commit": commit},
                {"name": "submission/1", "commit": {**commit, "sha": "c-1"}},
            ]
        ),
    )
    recorder.on("GET", f"{repo}/media/files/submission/main.py", httpx.Response(200, content=b"x"))
    workspace, task = WorkspaceId("acme/spring/@bob"), TaskId("acme/spring/sum")

    listed = await forgejo.workspaces.list_submissions(workspace, task)
    content = await forgejo.workspaces.read_submission_file(
        AsUser(8, _credential()), listed[1].id, "files/submission/main.py"
    )

    assert [(made.number, made.version, made.key) for made in listed] == [
        (1, "c-1", None),
        (2, "c-2", "k-2222222"),
    ]
    assert content == b"x"
    [read] = [request for request in recorder.seen if "/media/" in request.url.path]
    assert read.url.params["ref"] == "submission/2"
    assert read.headers["Authorization"] == "Bearer access"


async def test_a_publication_tags_the_saved_commit_with_its_note_and_writes_nothing(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.task"
    created = "2026-09-29T10:00:00Z"
    recorder.on(
        "GET",
        f"{repo}/tags",
        ok(
            [
                {
                    "name": "published/1",
                    "message": "grading_changed: false\nchanges: []\n",
                    "commit": {"sha": "c-1", "created": created},
                },
                {"name": "v1", "message": "", "commit": {"sha": "c-0", "created": created}},
                {
                    "name": "published/2",
                    "message": "grading_changed: true\nchanges:\n- limits.rate changed\n",
                    "commit": {"sha": "c-2", "created": created},
                },
            ]
        ),
    )

    publication = await forgejo.workspaces.publish(
        TaskId("acme/spring/sum"), VersionId("c-3"), "grading_changed: false\n"
    )
    listed = await forgejo.workspaces.list_publications(TaskId("acme/spring/sum"))

    assert publication == "acme/spring/sum#3"
    assert recorder.sent("POST", f"{repo}/tags") == [
        {"tag_name": "published/3", "target": "c-3", "message": "grading_changed: false\n"}
    ]
    assert recorder.headers("POST", f"{repo}/tags") == ["token admin"]
    assert f"POST {repo}/contents" not in recorder.calls()
    assert [call for call in recorder.calls() if call.split(" ")[1].startswith("/api/repos")] == []
    assert [(entry.number, entry.version, entry.grading_changed) for entry in listed] == [
        (1, "c-1", False),
        (2, "c-2", True),
    ]
    assert listed[1].changes == ("limits.rate changed",)
    assert listed[1].id == "acme/spring/sum#2"
    assert listed[0].at == datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


async def test_a_task_is_made_bare_and_secured_with_what_is_missing(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.task"
    recorder.on("GET", "/api/v1/orgs/acme", ok({"username": "acme"}))
    recorder.on("GET", f"{repo}/branches/main", ok({}, 404))
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-1"}}))
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/teams/search",
        ok(
            {
                "data": [
                    {"id": 1, "name": "acme.spring-admin"},
                    {"id": 2, "name": "acme.spring-manager"},
                    {"id": 3, "name": "acme.spring-observer"},
                    {"id": 4, "name": "acme.spring.sum-admin"},
                    {"id": 5, "name": "acme.spring.sum-manager"},
                    {"id": 6, "name": "acme.spring.sum-observer"},
                ]
            }
        ),
    )
    for team in (1, 2, 4, 5, 6):
        recorder.on("GET", f"/api/v1/teams/{team}/repos/acme/spring.sum.task", ok({}, 404))
    recorder.on("GET", "/api/v1/teams/3/repos/acme/spring.sum.task", ok({"id": 9}))
    recorder.on("GET", f"{repo}/branch_protections/main", ok({"enable_force_push": True}))
    recorder.on(
        "GET",
        f"{repo}/tag_protections",
        ok([{"id": 7, "name_pattern": "published/*", "whitelist_usernames": ["someone"]}]),
    )

    task = await forgejo.content.create_task(
        ContestId("acme/spring"), "sum", {"task.yaml": b"name: Sum\n"}
    )
    assert "PUT /api/v1/teams/1/repos/acme/spring.sum.task" not in recorder.calls()
    assert f"POST {repo}/branch_protections" not in recorder.calls()
    put_back = await forgejo.content.secure(task)

    assert task == "acme/spring/sum"
    attached = [call for call in recorder.calls() if call.startswith("PUT /api/v1/teams/")]
    assert attached == [
        f"PUT /api/v1/teams/{team}/repos/acme/spring.sum.task" for team in (1, 2, 4, 5, 6)
    ]
    assert recorder.sent("PATCH", f"{repo}/branch_protections/main") == [
        {"enable_force_push": False}
    ]
    assert recorder.sent("PATCH", f"{repo}/tag_protections/7") == [
        {"name_pattern": "published/*", "whitelist_usernames": ["platform-account"]}
    ]
    assert put_back == 7


async def test_a_save_is_one_commit_carrying_each_files_blob(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.task"
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-2"}}))
    ada = AsUser(7, _credential())

    version = await forgejo.content.save_files(
        ada,
        TaskId("acme/spring/sum"),
        {"task.yaml": b"a", "plans/default.json": b"{}", "plans/old.json": None},
        expected={"task.yaml": ConflictToken("blob-1"), "plans/old.json": ConflictToken("b-o")},
        message="Save",
    )

    assert version == "c-2"
    (sent,) = recorder.sent("POST", f"{repo}/contents")
    assert (sent["branch"], sent["message"]) == ("main", "Save")
    assert sent["files"] == [
        {
            "operation": "create",
            "path": "plans/default.json",
            "content": base64.b64encode(b"{}").decode(),
        },
        {"operation": "delete", "path": "plans/old.json", "sha": "b-o"},
        {
            "operation": "update",
            "path": "task.yaml",
            "content": base64.b64encode(b"a").decode(),
            "sha": "blob-1",
        },
    ]
    assert recorder.headers("POST", f"{repo}/contents") == ["Bearer access"]


async def test_the_files_at_a_version_are_read_page_by_page(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.task"
    recorder.on("GET", f"{repo}/branches/main", ok({"commit": {"id": "head-1"}}))
    recorder.on(
        "GET",
        f"{repo}/git/trees/head-1",
        ok({"tree": [{"path": "a", "type": "blob", "sha": "s-a"}], "truncated": True}),
        ok(
            {
                "tree": [
                    {"path": "d", "type": "tree", "sha": "s-d"},
                    {"path": "d/b", "type": "blob", "sha": "s-b"},
                ],
                "truncated": False,
            }
        ),
    )

    files = await forgejo.content.list_files(PLATFORM, TaskId("acme/spring/sum"))

    assert files.version == "head-1"
    assert files.tokens == {"a": "s-a", "d/b": "s-b"}
    pages = [
        request.url.params["page"]
        for request in recorder.seen
        if request.url.path == f"{repo}/git/trees/head-1"
    ]
    assert pages == ["1", "2"]


async def test_contests_and_tasks_are_listed_from_what_the_caller_sees(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/repos",
        ok(
            [
                {"name": "spring.contest"},
                {"name": "spring.sum.task"},
                {"name": "spring.ada.desk"},
                {"name": "spring.sum.ada.sub"},
                {"name": "autumn.contest"},
                {"name": "autumn.sum.task"},
                {"name": "classic.workflow"},
            ]
        ),
    )
    ada = AsUser(7, _credential())

    contests = await forgejo.content.list_contests(ada, OrgName("acme"))
    tasks = await forgejo.content.list_tasks(ada, ContestId("acme/spring"))

    assert contests == ("acme/autumn", "acme/spring")
    assert tasks == ("acme/spring/sum",)
    assert set(recorder.headers("GET", "/api/v1/orgs/acme/repos")) == {"Bearer access"}


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
    made = {"name": "submission/1", "commit": {"sha": "c-2", "created": "2026-09-30T10:00:00Z"}}
    recorder.on("GET", f"{repo}/tags", ok([]), ok([made]))

    submission = await forgejo.workspaces.record_submission(
        AsUser(8, _credential()),
        WorkspaceId("acme/spring/@bob"),
        TaskId("acme/spring/sum"),
        {"main.py": b"x"},
        key="key-12345678",
    )

    assert submission.id == "acme/spring/@bob/sum#1"
    assert recorder.calls().count(f"POST {repo}/contents") == 2
    assert recorder.calls().count(f"GET {repo}/git/trees/main") == 2
    assert recorder.sent("POST", f"{repo}/tags")[0]["target"] == "c-2"


async def test_a_workflow_under_a_person_is_made_by_the_platform_and_written_by_them(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/orgs/ada", ok({"message": "no such org"}, 404))
    recorder.on("GET", "/api/v1/repos/ada/classic.workflow/branches/main", ok({}, 404))
    recorder.on("GET", "/api/v1/repos/ada/classic.workflow/branch_protections/main", ok({}, 404))
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


SERVICE_ACCOUNT = {"id": 9, "login": "unicon-ci-acme", "full_name": "", "email": None}


async def test_an_event_push_is_made_once_for_the_org(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    url = "http://backend:8000/api/v1/events/forge/acme"
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/hooks",
        ok([]),
        ok([{"id": 3, "config": {"url": url}}]),
    )

    await forgejo.orgs.create_event_push(OrgName("acme"), url=url, secret="s3cret")
    await forgejo.orgs.create_event_push(OrgName("acme"), url=url, secret="s3cret")

    assert recorder.calls().count("POST /api/v1/orgs/acme/hooks") == 1
    (sent,) = recorder.sent("POST", "/api/v1/orgs/acme/hooks")
    assert sent["type"] == "forgejo"
    assert sent["active"] is True
    assert sent["config"] == {"url": url, "content_type": "json", "secret": "s3cret"}
    assert set(sent["events"]) >= {"push", "create", "delete", "issues", "issue_comment"}


async def test_the_service_account_is_put_in_its_place_once(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/teams/search",
        ok({"data": [{"id": 5, "name": "acme-ci"}, {"id": 1, "name": "acme-admin"}]}),
    )
    recorder.on("GET", "/api/v1/teams/5/members", ok([]), ok([SERVICE_ACCOUNT]))
    recorder.on("GET", "/api/v1/users/search", ok({"data": [SERVICE_ACCOUNT]}))

    assert await forgejo.orgs.ensure_account_membership(OrgName("acme"), 9) is True
    assert await forgejo.orgs.ensure_account_membership(OrgName("acme"), 9) is False

    assert recorder.calls().count("PUT /api/v1/teams/5/members/unicon-ci-acme") == 1


async def test_a_role_is_revoked_from_its_own_team_and_no_other(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/teams/search",
        ok({"data": [{"id": 5, "name": "acme-ci"}, {"id": 1, "name": "acme-admin"}]}),
    )
    recorder.on("GET", "/api/v1/teams/1/members", ok([USER, SERVICE_ACCOUNT]))
    recorder.on("GET", "/api/v1/users/search", ok({"data": [SERVICE_ACCOUNT]}))

    holders = await forgejo.orgs.holders_of(Scope("acme"), Role.ADMIN)
    await forgejo.orgs.revoke_role(9, Scope("acme"), Role.ADMIN)

    assert [holder.id for holder in holders] == [7, 9]
    assert "DELETE /api/v1/teams/1/members/unicon-ci-acme" in recorder.calls()
    assert "DELETE /api/v1/teams/5/members/unicon-ci-acme" not in recorder.calls()


async def test_anyones_roles_are_read_in_one_listing_the_platform_asks_for_them(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on(
        "GET",
        "/api/v1/user/teams",
        ok(
            [
                {"id": 1, "name": "acme.spring-manager", "organization": {"username": "acme"}},
                {"id": 3, "name": "acme-ci", "organization": {"username": "acme"}},
            ]
        ),
    )

    grants = await forgejo.orgs.roles_of_user(8)

    assert grants == (RoleGrant(Scope("acme", "spring"), Role.MANAGER),)
    assert recorder.calls().count("GET /api/v1/user/teams") == 1
    assert recorder.headers("GET", "/api/v1/user/teams") == ["token admin"]
    (listing,) = [request for request in recorder.seen if request.url.path == "/api/v1/user/teams"]
    assert listing.url.params["sudo"] == "bob"


async def test_an_account_is_created_given_a_password_and_a_token(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("POST", "/api/v1/admin/users", ok(SERVICE_ACCOUNT, 201))
    recorder.on("GET", "/api/v1/users/search", ok({"data": [SERVICE_ACCOUNT]}))
    recorder.on("GET", "/api/v1/users/unicon-ci-acme", ok(SERVICE_ACCOUNT))
    recorder.on("GET", "/api/v1/users/unicon-ci-acme/tokens", ok([{"id": 3, "name": "unicon"}]))
    recorder.on("POST", "/api/v1/users/unicon-ci-acme/tokens", ok({"sha1": "forge-token"}, 201))

    created = await forgejo.identity.create_user(
        "unicon-ci-acme",
        "a@unicon.invalid",
        "pw-1",
        must_change_password=False,
        visibility="private",
    )
    await forgejo.identity.set_password(9, "pw-2")
    found = await forgejo.identity.find_user_by_username("unicon-ci-acme")
    token = await forgejo.identity.mint_token(
        "unicon-ci-acme", "pw-2", name="unicon", scopes=["read:user"]
    )

    assert created.id == found.id == 9
    assert token == "forge-token"
    (sent,) = recorder.sent("POST", "/api/v1/admin/users")
    assert (sent["visibility"], sent["must_change_password"], sent["password"]) == (
        "private",
        False,
        "pw-1",
    )
    (patched,) = recorder.sent("PATCH", "/api/v1/admin/users/unicon-ci-acme")
    assert (patched["password"], patched["must_change_password"]) == ("pw-2", False)
    assert "DELETE /api/v1/users/unicon-ci-acme/tokens/3" in recorder.calls()
    assert recorder.sent("POST", "/api/v1/users/unicon-ci-acme/tokens") == [
        {"name": "unicon", "scopes": ["read:user"]}
    ]
    basic = base64.b64encode(b"unicon-ci-acme:pw-2").decode()
    assert recorder.headers("POST", "/api/v1/users/unicon-ci-acme/tokens") == [f"Basic {basic}"]


async def test_the_ci_user_is_found_or_made_and_asked_whether_it_is_alive(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/users/unicon-ci-acme", ok({}, 404), ok({"id": 4}))
    recorder.on("POST", "/api/users", ok({"id": 4}))
    recorder.on("GET", "/api/user", ok({"login": "unicon-ci-acme"}), ok({}, 401))

    assert await forgejo.grading.create_ci_user("unicon-ci-acme") == 4
    assert await forgejo.grading.create_ci_user("unicon-ci-acme") == 4
    assert await forgejo.grading.ci_user_is_alive(ACME) is True
    assert await forgejo.grading.ci_user_is_alive(ACME) is False

    assert recorder.sent("POST", "/api/users") == [{"login": "unicon-ci-acme"}]
    assert recorder.headers("POST", "/api/users") == ["Bearer ci-admin"]
    assert recorder.headers("GET", "/api/user") == ["Bearer ci-acme"] * 2
