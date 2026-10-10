"""The requests each area makes of Forgejo and Woodpecker: the shapes that a
live forge accepts, asserted without one.
"""

import base64
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from forge.adapters.git.forgejo import ForgejoForge
from forge.adapters.git.forgejo.ci_state import WoodpeckerState, read_state, written
from forge.domain.content import ConflictToken
from forge.domain.errors import Conflict, Forbidden, Misconfigured, NotFound, Rejected
from forge.domain.identity import PLATFORM, AsOrgAccount, AsUser, Credential, OrgAccountRef
from forge.domain.ids import ContestId, OrgId, TaskId, VersionId, WorkflowId, WorkspaceId
from forge.domain.names import OrgProfile, UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.threads import ThreadKind
from forge.domain.workflows import Visibility
from tests.forges.forgejo.conftest import CONFIG, Recorder, ok

ACME = AsOrgAccount(
    "acme", forge_token="forge-acme", ci_state=written(WoodpeckerState(4, "ci-acme", None))
)

USER = {"id": 7, "login": "ada", "full_name": "Ada", "email": None, "avatar_url": None}


def _credential() -> Credential:
    return Credential("access", "refresh", datetime.now(UTC) + timedelta(hours=1))


async def test_an_org_agent_is_enrolled_under_its_org(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/orgs/lookup/acme", ok({"id": 42}))
    recorder.on("POST", "/api/orgs/42/agents", ok({"id": 9, "token": "t"}))
    recorder.on("POST", "/api/agents", ok({"id": 10, "token": "g"}))

    org_agent = await forgejo.computes.enrol_agent(OrgId("acme"), "box")
    global_agent = await forgejo.computes.enrol_agent(None, "pool")
    await forgejo.computes.revoke_agent(OrgId("acme"), org_agent.agent)

    assert (org_agent.agent, org_agent.token) == ("9", "t")
    assert global_agent.agent == "10"
    assert "POST /api/orgs/42/agents" in recorder.calls()
    assert "DELETE /api/orgs/42/agents/9" in recorder.calls()
    assert recorder.sent("POST", "/api/orgs/42/agents") == [{"name": "box"}]


async def test_a_workspace_is_attached_to_the_contest_roles(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    for repo in ("spring.u8.desk", "spring.sum.u8.sub"):
        recorder.on("GET", f"/api/v1/repos/acme/{repo}/collaborators/bob/permission", ok({}, 404))
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

    workspace = await forgejo.workspaces.open_workspace(ContestId("acme/spring"), UserOwner(8), [8])
    await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/spring/sum"), [8])

    assert workspace == WorkspaceId("acme/spring/@u8")
    calls = recorder.calls()
    for repo in ("spring.u8.desk", "spring.sum.u8.sub"):
        assert f"PUT /api/v1/teams/1/repos/acme/{repo}" in calls
        assert f"PUT /api/v1/teams/2/repos/acme/{repo}" in calls
        assert f"PUT /api/v1/repos/acme/{repo}/collaborators/bob" in calls
    assert "POST /api/v1/repos/acme/spring.sum.u8.sub/tag_protections" in calls
    assert "POST /api/v1/repos/acme/spring.u8.desk/tag_protections" not in calls
    assert calls.index("POST /api/v1/repos/acme/spring.sum.u8.sub/tag_protections") < calls.index(
        "PUT /api/v1/repos/acme/spring.sum.u8.sub/collaborators/bob"
    )
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.sum.u8.sub/tag_protections") == [
        {"name_pattern": "submission/*", "whitelist_usernames": ["platform-account"]}
    ]
    assert recorder.sent("PUT", "/api/v1/repos/acme/spring.u8.desk/collaborators/bob") == [
        {"permission": "read"}
    ]
    assert recorder.calls().count("POST /api/v1/orgs/acme/repos") == 2
    assert set(recorder.headers("POST", "/api/v1/orgs/acme/repos")) == {"token admin"}
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.u8.desk/branch_protections") == [
        {"branch_name": "main", "enable_push": True, "block_on_rejected_reviews": False}
    ]


async def test_a_workspace_opened_again_keeps_what_is_there(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.u8.desk/collaborators/bob/permission",
        ok({"permission": "none"}),
    )
    recorder.on("GET", "/api/v1/repos/acme/spring.u8.desk", ok({"id": 3}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.u8.desk/branch_protections/main",
        ok({"branch_name": "main"}),
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

    workspace = await forgejo.workspaces.open_workspace(ContestId("acme/spring"), UserOwner(8), [8])

    calls = recorder.calls()
    assert "POST /api/v1/orgs/acme/repos" not in calls
    assert "POST /api/v1/repos/acme/spring.u8.desk/branch_protections" not in calls
    assert not [call for call in calls if call.startswith("PUT /api/v1/teams/")]
    assert "PUT /api/v1/repos/acme/spring.u8.desk/collaborators/bob" in calls
    with pytest.raises(NotFound):
        await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/autumn/sum"), [8])


async def test_a_workspace_repo_someone_else_is_in_is_refused_and_not_shared(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.u8.desk/collaborators/bob/permission",
        ok({"permission": "none"}),
    )
    recorder.on("GET", "/api/v1/repos/acme/spring.u8.desk", ok({"id": 3}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.u8.desk/collaborators",
        ok([{**USER, "id": 7, "login": "ada"}, {**USER, "id": 8, "login": "Bob"}]),
    )

    with pytest.raises(Conflict, match="ada"):
        await forgejo.workspaces.open_workspace(ContestId("acme/spring"), UserOwner(8), [8])

    assert not [call for call in recorder.calls() if call.startswith("PUT ")]


async def test_a_place_its_members_can_write_already_is_finished_and_left_alone(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.sum.u8.sub/collaborators/bob/permission",
        ok({"permission": "write"}),
    )
    workspace = forgejo.workspaces.workspace_of(ContestId("acme/spring"), UserOwner(8))

    await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/spring/sum"), [8])

    assert recorder.calls() == [
        "GET /api/v1/users/search",
        "GET /api/v1/repos/acme/spring.sum.u8.sub/collaborators/bob/permission",
    ]


async def test_a_branch_protected_by_another_maker_meanwhile_counts_as_protected(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    repo = "/api/v1/repos/acme/spring.sum.u8.sub"
    recorder.on("GET", f"{repo}/collaborators/bob/permission", ok({}, 404))
    recorder.on("GET", repo, ok({}, 404))
    recorder.on(
        "GET",
        f"{repo}/branch_protections/main",
        ok({}, 404),
        ok({"branch_name": "main"}),
    )
    recorder.on(
        "POST",
        f"{repo}/branch_protections",
        ok({"message": "Branch protection already exist"}, 403),
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
    workspace = forgejo.workspaces.workspace_of(ContestId("acme/spring"), UserOwner(8))

    await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/spring/sum"), [8])

    assert f"PUT {repo}/collaborators/bob" in recorder.calls()


async def test_a_place_a_member_only_reads_is_made_up_to_writing(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 8, "login": "bob"}]}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.sum.u8.sub/collaborators/bob/permission",
        ok({"permission": "read"}),
    )
    recorder.on("GET", "/api/v1/repos/acme/spring.sum.u8.sub", ok({"id": 3}))
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.sum.u8.sub/collaborators",
        ok([{**USER, "id": 8, "login": "bob"}]),
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
    workspace = forgejo.workspaces.workspace_of(ContestId("acme/spring"), UserOwner(8))

    await forgejo.workspaces.open_submission_place(workspace, TaskId("acme/spring/sum"), [8])

    assert recorder.sent("PUT", "/api/v1/repos/acme/spring.sum.u8.sub/collaborators/bob") == [
        {"permission": "write"}
    ]


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

    await forgejo.orgs.create_org(OrgId("acme"), description="Acme")
    await forgejo.orgs.create_roles(OrgId("acme"))
    await forgejo.orgs.create_thread_labels(OrgId("acme"))

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
    recorder.on("POST", "/api/v1/repos/acme/spring.contest/issues", ok(_issue(12)))

    thread = await forgejo.threads.post_thread(
        PLATFORM, ContestId("acme/spring"), ThreadKind.ANNOUNCEMENT, title="t", body="b"
    )

    assert (thread.id, thread.place, thread.number) == (
        "acme/spring.contest#12",
        "acme/spring",
        12,
    )
    assert thread.kind is ThreadKind.ANNOUNCEMENT
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.contest/issues") == [
        {"title": "t", "body": "b", "labels": [4]}
    ]


async def test_a_thread_whose_label_did_not_hold_is_labelled_by_the_platform(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/labels",
        ok([{"id": 4, "name": "clarification"}]),
        ok([{"id": 9, "name": "clarification"}]),
    )
    recorder.on("POST", "/api/v1/repos/acme/spring.u8.desk/issues", ok(_issue(3, labels=[])))
    recorder.on(
        "POST",
        "/api/v1/repos/acme/spring.u8.desk/issues/3/labels",
        ok([{"id": 9, "name": "clarification"}]),
    )
    workspace = forgejo.workspaces.workspace_of(ContestId("acme/spring"), UserOwner(8))

    thread = await forgejo.threads.post_thread(
        PLATFORM, workspace, ThreadKind.CLARIFICATION, title="t", body="b"
    )

    assert thread.kind is ThreadKind.CLARIFICATION
    assert recorder.sent("POST", "/api/v1/repos/acme/spring.u8.desk/issues/3/labels") == [
        {"labels": [9]}
    ]


def _issue(number: int, *labels: str, state: str = "open", **extra: object) -> dict[str, object]:
    return {
        "number": number,
        "title": "t",
        "body": "b",
        "state": state,
        "created_at": "2026-09-26T10:00:00+00:00",
        "labels": [{"name": label} for label in labels or ("announcement",)],
        "user": {"id": 7},
        **extra,
    }


async def test_marking_labels_and_closes_and_unmarking_takes_both_back(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    """The label's id is read once for both, and a label already gone when
    the mark is taken off is no error, so either can be repeated.
    """
    recorder.on("GET", "/api/v1/orgs/acme/labels", ok([{"id": 6, "name": "answered"}]))
    recorder.on("DELETE", "/api/v1/repos/acme/spring.u8.desk/issues/3/labels/6", ok({}, 404))
    thread = forgejo.threads.thread_of(WorkspaceId("acme/spring/@u8"), 3)

    await forgejo.threads.mark_answered(PLATFORM, thread)
    await forgejo.threads.unmark_answered(PLATFORM, thread)

    assert recorder.sent("POST", "/api/v1/repos/acme/spring.u8.desk/issues/3/labels") == [
        {"labels": [6]}
    ]
    assert recorder.sent("PATCH", "/api/v1/repos/acme/spring.u8.desk/issues/3") == [
        {"state": "closed"},
        {"state": "open"},
    ]
    assert recorder.calls().count("GET /api/v1/orgs/acme/labels") == 1


async def test_the_org_search_keeps_its_own_orgs_open_clarifications_with_their_comments(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/repos/issues/search",
        ok(
            [
                _issue(3, "clarification", repository={"owner": "acme", "name": "spring.u8.desk"}),
                _issue(4, "clarification", repository={"owner": "other", "name": "x.u8.desk"}),
            ]
        ),
    )
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.u8.desk/issues/3/comments",
        ok([{"id": 1, "body": "A", "created_at": "2026-09-26T10:05:00+00:00", "user": {"id": 9}}]),
    )

    (found,) = await forgejo.threads.search_threads(
        PLATFORM, OrgId("acme"), ThreadKind.CLARIFICATION
    )

    assert (found.place, found.number, found.kind) == (
        "acme/spring/@u8",
        3,
        ThreadKind.CLARIFICATION,
    )
    assert [comment.body for comment in found.comments] == ["A"]
    [search] = [
        dict(request.url.params)
        for request in recorder.seen
        if request.url.path == "/api/v1/repos/issues/search"
    ]
    assert (search["state"], search["labels"], search["owner"]) == (
        "open",
        "clarification",
        "acme",
    )


@pytest.mark.parametrize(
    ("kind", "repository", "expected"),
    [
        ("issues", "spring.contest", ("announcement", "acme/spring", None, None)),
        (
            "issue_comment",
            "spring.sum.task",
            ("announcement", "acme/spring", "acme/spring/sum", None),
        ),
        ("issue_comment", "spring.u8.desk", ("clarification", "acme/spring", None, 8)),
    ],
)
def test_an_event_about_a_thread_reads_as_the_change(
    forgejo: ForgejoForge, kind: str, repository: str, expected: tuple[object, ...]
) -> None:
    body = json.dumps(
        {
            "repository": {"name": repository, "owner": {"login": "acme"}},
            "issue": {"number": 5, "labels": [{"name": expected[0]}]},
        }
    ).encode()

    change = forgejo.threads.read_event(kind, body)

    assert change is not None
    asker = change.asker.user_id if isinstance(change.asker, UserOwner) else None
    assert (change.kind.value, change.contest, change.task, asker) == expected
    assert change.thread.endswith("#5")


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("push", b'{"repository": {"name": "spring.contest", "owner": {"login": "acme"}}}'),
        ("issues", b"not json"),
        (
            "issues",
            b'{"repository": {"name": "x.workflow", "owner": {"login": "acme"}},'
            b' "issue": {"number": 1, "labels": [{"name": "announcement"}]}}',
        ),
        (
            "issues",
            b'{"repository": {"name": "spring.contest", "owner": {"login": "acme"}},'
            b' "issue": {"number": 1, "labels": []}}',
        ),
        (
            "issue_comment",
            b'{"repository": {"name": "spring.contest", "owner": {"login": "acme"}},'
            b' "issue": {"number": 1, "labels": [{"name": "announcement"}],'
            b' "pull_request": {"merged": false}}}',
        ),
        (
            "issues",
            b'{"repository": {"name": "spring.u8.desk", "owner": {"login": "acme"}},'
            b' "issue": {"number": 1, "labels": [{"name": "announcement"}]}}',
        ),
    ],
)
def test_an_event_about_anything_else_reads_as_nothing(
    forgejo: ForgejoForge, kind: str, body: bytes
) -> None:
    assert forgejo.threads.read_event(kind, body) is None


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

    await forgejo.orgs.create_roles(OrgId("acme"))
    await forgejo.orgs.create_thread_labels(OrgId("acme"))

    assert recorder.sent("POST", "/api/v1/orgs/acme/teams") == []
    labels = [body["name"] for body in recorder.sent("POST", "/api/v1/orgs/acme/labels")]
    assert labels == ["clarification", "answered"]


async def test_a_submission_is_written_as_the_contestant_and_named_at_that_commit(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.u8.sub"
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
        WorkspaceId("acme/spring/@u8"),
        TaskId("acme/spring/sum"),
        {"main.py": b"x"},
        key="key-12345678",
    )

    assert (submission.id, submission.number, submission.version) == (
        "acme/spring/@u8/sum#2",
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
    repo = "/api/v1/repos/acme/spring.sum.u8.sub"
    commit = {"sha": "c-bob", "created": "2026-09-30T10:00:00Z"}
    raced = {"name": "submission/1", "commit": {"sha": "c-other", "created": commit["created"]}}
    mine = {"name": "submission/2", "message": "idempotency_key: key-12345678\n", "commit": commit}
    recorder.on("GET", f"{repo}/branches/main", ok({}, 404))
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c-bob"}}))
    recorder.on("GET", f"{repo}/tags", ok([]), ok([raced]), ok([raced, mine]))
    recorder.on("POST", f"{repo}/tags", ok({"message": "tag already exists"}, 409), ok({}))

    submission = await forgejo.workspaces.record_submission(
        AsUser(8, _credential()),
        WorkspaceId("acme/spring/@u8"),
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
    repo = "/api/v1/repos/acme/spring.sum.u8.sub"
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
    recorder.on("GET", f"{repo}/raw/files/submission/main.py", httpx.Response(200, content=b"p"))
    workspace, task = WorkspaceId("acme/spring/@u8"), TaskId("acme/spring/sum")

    listed = await forgejo.workspaces.list_submissions(workspace, task)
    content = await forgejo.workspaces.read_submission_file(
        AsUser(8, _credential()), listed[1].id, "files/submission/main.py", max_size=1
    )
    door = await forgejo.workspaces.download(
        AsUser(8, _credential()), listed[1].id, "files/submission/my main.py"
    )
    assert door.path == (f"{repo}/media/files/submission/my%20main.py?ref=submission%2F2")
    assert door.authorization == "Bearer access"
    with pytest.raises(Rejected, match="larger than 0 bytes"):
        await forgejo.workspaces.read_submission_file(
            AsUser(8, _credential()), listed[1].id, "files/submission/main.py", max_size=0
        )

    assert [(made.number, made.version, made.key) for made in listed] == [
        (1, "c-1", None),
        (2, "c-2", "k-2222222"),
    ]
    assert content == b"x"
    read = next(request for request in recorder.seen if "/media/" in request.url.path)
    assert read.url.params["ref"] == "submission/2"
    blob = await forgejo.workspaces.read_submission_blob(
        AsUser(8, _credential()), listed[1].id, "files/submission/main.py", max_size=1
    )
    assert blob == b"p"
    raw = next(request for request in recorder.seen if "/raw/" in request.url.path)
    assert raw.url.params["ref"] == "submission/2"
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
    recorder.on("GET", f"{repo}/branch_protections/main", ok({"branch_name": "main"}))
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
    assert f"POST {repo}/branch_protections" not in recorder.calls()
    assert recorder.sent("PATCH", f"{repo}/tag_protections/7") == [
        {"name_pattern": "published/*", "whitelist_usernames": ["platform-account"]}
    ]
    assert put_back == 6


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


async def test_contests_and_tasks_are_listed_by_a_search_of_what_the_caller_sees(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    """A search matches the part anywhere in a name, so the answer is checked
    name by name; the org's number is read once.
    """
    recorder.on("GET", "/api/v1/orgs/acme", ok({"id": 42}))
    recorder.on(
        "GET",
        "/api/v1/repos/search",
        ok(
            {
                "ok": True,
                "data": [
                    {"name": "spring.contest"},
                    {"name": "autumn.contest"},
                    {"name": "spring.contest.task"},
                ],
            }
        ),
        ok(
            {
                "ok": True,
                "data": [
                    {"name": "spring.sum.task"},
                    {"name": "autumn.sum.task"},
                    {"name": "spring.task.u7.sub"},
                ],
            }
        ),
    )
    ada = AsUser(7, _credential())

    contests = await forgejo.content.list_contests(ada, OrgId("acme"))
    tasks = await forgejo.content.list_tasks(ada, ContestId("acme/spring"))

    assert contests == ("acme/autumn", "acme/spring")
    assert tasks == ("acme/spring/sum",)
    searches = [
        dict(request.url.params)
        for request in recorder.seen
        if request.url.path == "/api/v1/repos/search"
    ]
    assert [(search["q"], search["uid"], search["exclusive"]) for search in searches] == [
        (".contest", "42", "true"),
        (".task", "42", "true"),
    ]
    assert set(recorder.headers("GET", "/api/v1/repos/search")) == {"Bearer access"}
    assert recorder.calls().count("GET /api/v1/orgs/acme") == 1
    assert "GET /api/v1/orgs/acme/repos" not in recorder.calls()


async def test_a_write_the_host_refuses_as_moved_is_read_again_and_repeated(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/spring.sum.u8.sub"
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
        WorkspaceId("acme/spring/@u8"),
        TaskId("acme/spring/sum"),
        {"main.py": b"x"},
        key="key-12345678",
    )

    assert submission.id == "acme/spring/@u8/sum#1"
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
        "token admin"
    ]


async def test_a_create_whose_repository_another_maker_filled_first_writes_nothing(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/grading.workflow"
    recorder.on("GET", "/api/v1/orgs/acme", ok({"username": "acme"}))
    recorder.on("GET", f"{repo}/branches/main", ok({"name": "main"}))
    recorder.on("GET", repo, ok({"name": "grading.workflow", "topics": ["unicon-workflow"]}))
    recorder.on(
        "GET",
        f"{repo}/git/trees/main",
        ok({"tree": [{"path": "workflow.yaml", "type": "blob", "sha": "b"}]}),
    )

    with pytest.raises(Conflict):
        await forgejo.workflows.create_workflow(
            AsUser(7, _credential()),
            "acme",
            "grading",
            {"workflow.yaml": b"steps: []"},
            Visibility.PRIVATE,
        )

    assert f"POST {repo}/contents" not in recorder.calls()


async def test_an_org_workflow_is_made_and_marked_by_the_platform_and_written_by_its_manager(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/grading.workflow"
    recorder.on("GET", "/api/v1/orgs/acme", ok({"username": "acme"}))
    recorder.on("GET", f"{repo}/branches/main", ok({}, 404))
    recorder.on("GET", f"{repo}/branch_protections/main", ok({}, 404))
    recorder.on("POST", f"{repo}/contents", ok({"commit": {"sha": "c"}}))

    workflow = await forgejo.workflows.create_workflow(
        AsUser(7, _credential()),
        "acme",
        "grading",
        {"workflow.yaml": b"steps: []"},
        Visibility.PRIVATE,
    )

    assert workflow == "acme/grading"
    assert recorder.headers("POST", "/api/v1/orgs/acme/repos") == ["token admin"]
    assert recorder.sent("POST", "/api/v1/orgs/acme/repos")[0]["private"] is True
    assert recorder.headers("POST", f"{repo}/contents") == ["Bearer access"]
    assert recorder.headers("POST", f"{repo}/branch_protections") == ["token admin"]
    assert recorder.headers("PUT", f"{repo}/topics/unicon-workflow") == ["token admin"]


async def test_who_reads_a_workflow_changes_as_the_platform_for_someone_who_may_write_it(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/grading.workflow"
    recorder.on(
        "GET",
        repo,
        ok(
            {
                "name": "grading.workflow",
                "topics": ["unicon-workflow"],
                "permissions": {"admin": False, "push": True, "pull": True},
            }
        ),
    )
    recorder.on("GET", "/api/v1/users/search", ok({"data": [{**USER, "id": 9, "login": "eve"}]}))
    manager = AsUser(7, _credential())
    workflow = WorkflowId("acme/grading")

    await forgejo.workflows.set_workflow_visibility(manager, workflow, Visibility.PUBLIC)
    await forgejo.workflows.share_workflow(manager, workflow, 9)
    await forgejo.workflows.unshare_workflow(manager, workflow, 9)

    assert recorder.headers("GET", repo) == ["Bearer access"] * 3
    assert recorder.headers("PATCH", repo) == ["token admin"]
    assert recorder.sent("PATCH", repo) == [{"private": False}]
    assert recorder.headers("PUT", f"{repo}/collaborators/eve") == ["token admin"]
    assert recorder.sent("PUT", f"{repo}/collaborators/eve") == [{"permission": "read"}]
    assert recorder.headers("DELETE", f"{repo}/collaborators/eve") == ["token admin"]


async def test_someone_who_only_reads_a_workflow_changes_nobodys_access_to_it(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/grading.workflow"
    recorder.on(
        "GET",
        repo,
        ok(
            {
                "name": "grading.workflow",
                "topics": ["unicon-workflow"],
                "permissions": {"admin": False, "push": False, "pull": True},
            }
        ),
    )
    observer = AsUser(8, _credential())
    workflow = WorkflowId("acme/grading")

    with pytest.raises(Forbidden):
        await forgejo.workflows.set_workflow_visibility(observer, workflow, Visibility.PUBLIC)
    with pytest.raises(Forbidden):
        await forgejo.workflows.share_workflow(observer, workflow, 9)

    assert [call for call in recorder.calls() if not call.startswith("GET ")] == []


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
    assert forgejo.identity.public_url() == "http://forge.test"


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

    await forgejo.orgs.create_event_push(OrgId("acme"), url=url, secret="s3cret")
    await forgejo.orgs.create_event_push(OrgId("acme"), url=url, secret="s3cret")

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

    assert await forgejo.orgs.ensure_account_membership(OrgId("acme"), 9) is True
    assert await forgejo.orgs.ensure_account_membership(OrgId("acme"), 9) is False

    assert recorder.calls().count("PUT /api/v1/teams/5/members/unicon-ci-acme") == 1


async def test_the_service_account_leaves_its_place_found_by_its_id(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme/teams/search",
        ok({"data": [{"id": 5, "name": "acme-ci"}, {"id": 1, "name": "acme-admin"}]}),
        ok({"data": []}),
    )
    recorder.on("GET", "/api/v1/teams/5/members", ok([USER, SERVICE_ACCOUNT]))

    await forgejo.orgs.remove_account_membership(OrgId("acme"), 9)
    await forgejo.orgs.remove_account_membership(OrgId("acme"), 9)

    deletions = [call for call in recorder.calls() if call.startswith("DELETE")]
    assert deletions == ["DELETE /api/v1/teams/5/members/unicon-ci-acme"]


async def test_an_orgs_own_fields_are_read_as_the_platform(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        "/api/v1/orgs/acme",
        ok({"username": "acme", "full_name": "ACME", "description": "Acme Corp"}),
        ok({"username": "acme", "full_name": "", "description": ""}),
        ok({}, 404),
    )

    named = await forgejo.orgs.read_org(OrgId("acme"))
    bare = await forgejo.orgs.read_org(OrgId("acme"))

    assert named == OrgProfile(display_name="ACME", description="Acme Corp")
    assert bare == OrgProfile(display_name=None, description="")
    with pytest.raises(NotFound):
        await forgejo.orgs.read_org(OrgId("acme"))
    assert recorder.headers("GET", "/api/v1/orgs/acme") == ["token admin"] * 3


async def test_an_org_is_deleted_and_one_not_there_is_no_error(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/orgs/acme/repos", ok([]), ok([]), ok({}, 404))
    recorder.on("DELETE", "/api/v1/orgs/acme", httpx.Response(204), ok({}, 404))

    await forgejo.orgs.delete_org(OrgId("acme"))
    await forgejo.orgs.delete_org(OrgId("acme"))
    await forgejo.orgs.delete_org(OrgId("acme"))

    assert recorder.headers("DELETE", "/api/v1/orgs/acme") == ["token admin"] * 2


async def test_an_org_that_still_holds_a_place_is_refused(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/orgs/acme/repos", ok([{"name": "spring.contest"}]))

    with pytest.raises(Rejected):
        await forgejo.orgs.delete_org(OrgId("acme"))

    assert "DELETE /api/v1/orgs/acme" not in recorder.calls()


async def test_a_place_is_deleted_with_its_own_teams_and_no_others(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
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
    recorder.on("DELETE", "/api/v1/repos/acme/spring.sum.task", httpx.Response(204))

    await forgejo.content.delete_place(TaskId("acme/spring/sum"))

    deletions = [call for call in recorder.calls() if call.startswith("DELETE")]
    assert deletions == [
        "DELETE /api/v1/teams/4",
        "DELETE /api/v1/teams/5",
        "DELETE /api/v1/teams/6",
        "DELETE /api/v1/repos/acme/spring.sum.task",
    ]


async def test_a_place_not_there_is_no_error(forgejo: ForgejoForge, recorder: Recorder) -> None:
    recorder.on("GET", "/api/v1/orgs/acme/teams/search", ok({"data": []}))
    recorder.on("DELETE", "/api/v1/repos/acme/spring.contest", ok({}, 404))

    await forgejo.content.delete_place(ContestId("acme/spring"))

    assert recorder.calls().count("DELETE /api/v1/repos/acme/spring.contest") == 1


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
    await forgejo.ci_host.set_password(9, "pw-2")
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


async def test_an_org_is_set_up_with_its_user_found_or_made_then_signed_in(
    forgejo: ForgejoForge, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder.on("GET", "/api/users/unicon-ci-acme", ok({}, 404), ok({"id": 4}))
    recorder.on("POST", "/api/users", ok({"id": 4}))
    signed_in: list[tuple[str, str]] = []

    async def mint_token(username: str, forge_password: str) -> str:
        signed_in.append((username, forge_password))
        return f"token-{len(signed_in)}"

    monkeypatch.setattr(forgejo.grading._login, "mint_token", mint_token)
    account = OrgAccountRef("unicon-ci-acme", 9, "pw-1")

    first = await forgejo.grading.set_up_org(OrgId("acme"), account)
    again = await forgejo.grading.set_up_org(OrgId("acme"), account)

    assert recorder.sent("POST", "/api/users") == [{"login": "unicon-ci-acme"}]
    assert recorder.headers("POST", "/api/users") == ["Bearer ci-admin"]
    assert signed_in == [("unicon-ci-acme", "pw-1")] * 2
    assert (read_state(first).user_id, read_state(first).token) == (4, "token-1")
    assert (read_state(again).user_id, read_state(again).token) == (4, "token-2")


async def test_a_name_goes_into_a_path_quoted_whole_and_a_dot_segment_reaches_nothing(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    recorder.on("GET", "/api/v1/users/bob/repos?x#y", ok({**USER, "login": "bob/repos?x#y"}))
    content = base64.b64encode(b"x").decode()
    recorder.on(
        "GET",
        "/api/v1/repos/acme/spring.contest/contents/notes/a b?.md",
        ok({"type": "file", "sha": "blob-1", "content": content}),
    )

    found = await forgejo.identity.find_user_by_username("bob/repos?x#y")
    await forgejo.content.read_file(PLATFORM, ContestId("acme/spring"), "notes/a b?.md")

    assert found.username == "bob/repos?x#y"
    assert [request.url.raw_path for request in recorder.seen] == [
        b"/api/v1/users/bob%2Frepos%3Fx%23y",
        b"/api/v1/repos/acme/spring.contest/contents/notes/a%20b%3F.md",
    ]
    for dots in ("..", ".", ""):
        with pytest.raises(NotFound):
            await forgejo.identity.find_user_by_username(dots)
    with pytest.raises(NotFound):
        await forgejo.content.read_file(PLATFORM, ContestId("acme/spring"), "../secrets")
    assert len(recorder.seen) == 2


async def test_a_version_is_read_at_its_tags_commit_whatever_its_name_spells(
    forgejo: ForgejoForge, recorder: Recorder
) -> None:
    repo = "/api/v1/repos/acme/grading.workflow"
    named, tagged = "a" * 40, "b" * 40
    recorder.on("GET", repo, ok({"name": "grading.workflow", "topics": ["unicon-workflow"]}))
    recorder.on("GET", f"{repo}/tags", ok([{"name": named, "commit": {"sha": tagged}}]))
    recorder.on("GET", f"{repo}/contents/workflow.yaml", ok({"type": "file", "sha": "s"}))

    await forgejo.workflows.read_workflow_file(
        AsUser(7, _credential()), WorkflowId("acme/grading"), named, "workflow.yaml"
    )

    [read] = [seen for seen in recorder.seen if seen.url.path == f"{repo}/contents/workflow.yaml"]
    assert read.url.params["ref"] == tagged


def test_a_ci_the_forgejo_forge_does_not_grade_with_is_misconfigured() -> None:
    with pytest.raises(Misconfigured, match="UNICON_CI=jenkins"):
        ForgejoForge(dataclasses.replace(CONFIG, ci="jenkins"))
