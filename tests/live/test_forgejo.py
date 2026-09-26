"""The Forgejo implementation against a real Forgejo. These run only when
`UNICON_LIVE_FORGE_URL` and `UNICON_LIVE_FORGE_ADMIN_TOKEN` name a running
instance whose platform account may create users and orgs; every name they
create carries a random suffix and is removed afterwards.
"""

import os
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from forge.domain.errors import Conflict, NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import OrgName
from forge.domain.names import UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.threads import ThreadKind
from forge.domain.workflows import Visibility
from forge.forges.forgejo import ForgejoConfig, ForgejoForge

URL = os.environ.get("UNICON_LIVE_FORGE_URL")
ADMIN_TOKEN = os.environ.get("UNICON_LIVE_FORGE_ADMIN_TOKEN")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not URL or not ADMIN_TOKEN, reason="no live forge configured"),
]


class NoTokens:
    async def forge_token(self, org: str) -> str:
        raise NotFound(f"org {org} has no org account")

    async def ci_token(self, org: str) -> str:
        raise NotFound(f"org {org} has no org account")


@pytest.fixture(scope="module")
def stamp() -> str:
    return secrets.token_hex(3)


@pytest.fixture(scope="module")
def admin() -> httpx.Client:
    assert URL and ADMIN_TOKEN
    return httpx.Client(
        base_url=URL.rstrip("/"), headers={"Authorization": f"token {ADMIN_TOKEN}"}, timeout=30
    )


@pytest.fixture
async def forge() -> AsyncIterator[ForgejoForge]:
    assert URL and ADMIN_TOKEN
    built = ForgejoForge(
        ForgejoConfig(
            public_url=URL,
            internal_url=URL,
            admin_token=ADMIN_TOKEN,
            oauth_client_id="unused",
            oauth_client_secret="unused",
            sign_in_redirect_uri="http://unused/callback",
            sign_ups_open=True,
            ci_url="http://unused",
            ci_public_url="http://unused",
            ci_admin_token="unused",
        ),
        NoTokens(),
    )
    try:
        yield built
    finally:
        await built.aclose()


@pytest.fixture(scope="module")
def user(admin: httpx.Client, stamp: str) -> Iterator[dict[str, Any]]:
    created = admin.post(
        "/api/v1/admin/users",
        json={
            "username": f"live-{stamp}",
            "email": f"live-{stamp}@unicon.invalid",
            "password": "live-password-123",
            "must_change_password": False,
        },
    )
    assert created.status_code == 201, created.text
    person: dict[str, Any] = created.json()
    yield person
    admin.delete(f"/api/v1/admin/users/{person['login']}", params={"purge": "true"})


@pytest.fixture(scope="module")
def org(admin: httpx.Client, stamp: str) -> Iterator[str]:
    name = f"live-org-{stamp}"
    yield name
    for repo in admin.get(f"/api/v1/orgs/{name}/repos", params={"limit": 50}).json() or []:
        admin.delete(f"/api/v1/repos/{name}/{repo['name']}")
    admin.delete(f"/api/v1/orgs/{name}")


async def test_a_user_is_found_deactivated_and_reactivated(
    forge: ForgejoForge, user: dict[str, Any], admin: httpx.Client
) -> None:
    found = await forge.identity.find_user(int(user["id"]))
    assert found.username == user["login"]

    await forge.identity.deactivate_user(found.id)
    assert (await forge.identity.find_user(found.id)).active is False
    admin.patch(
        f"/api/v1/admin/users/{found.username}",
        json={"active": True, "login_name": found.username, "source_id": 0},
    )
    assert (await forge.identity.find_user(found.id)).active is True


async def test_an_org_with_its_roles_and_a_contest_with_its_files(
    forge: ForgejoForge, org: str, user: dict[str, Any]
) -> None:
    await forge.orgs.create_org(OrgName(org), description="Live test org")
    with pytest.raises(Conflict):
        await forge.orgs.create_org(OrgName(org), description="again")

    user_id = int(user["id"])
    await forge.orgs.grant_role(user_id, Scope(org), Role.ADMIN)
    assert await forge.orgs.roles_of(user_id) == (RoleGrant(Scope(org), Role.ADMIN),)
    assert [holder.id for holder in await forge.orgs.holders_of(Scope(org), Role.ADMIN)] == [
        user_id
    ]

    contest = await forge.content.create_contest(
        OrgName(org), "spring", {"contest.yaml": b"name: Spring\n"}
    )
    task = await forge.content.create_task(contest, "sum", {"task.yaml": b"name: Sum\n"})

    first = await forge.content.read_file(PLATFORM, contest, "contest.yaml")
    assert first.content == b"name: Spring\n"
    await forge.content.write_file(
        PLATFORM,
        contest,
        "contest.yaml",
        b"name: Spring 2026\n",
        message="Rename",
        expected=first.token,
    )
    with pytest.raises(Conflict):
        await forge.content.write_file(
            PLATFORM, contest, "contest.yaml", b"stale\n", message="Stale", expected=first.token
        )
    with pytest.raises(Conflict):
        await forge.content.write_file(
            PLATFORM, contest, "contest.yaml", b"new\n", message="New", expected=None
        )
    second = await forge.content.read_file(PLATFORM, contest, "contest.yaml")
    assert second.content == b"name: Spring 2026\n"
    history = await forge.content.history(PLATFORM, contest)
    assert [change.message for change in history] == ["Rename", "Create"]
    older = await forge.content.read_file(PLATFORM, contest, "contest.yaml", at=history[1].version)
    assert older.content == b"name: Spring\n"
    assert [entry.path for entry in await forge.content.list_tree(PLATFORM, task)] == ["task.yaml"]

    await forge.orgs.grant_role(user_id, Scope(org, "spring"), Role.MANAGER)
    assert RoleGrant(Scope(org, "spring"), Role.MANAGER) in await forge.orgs.roles_of(user_id)
    await forge.orgs.revoke_role(user_id, Scope(org, "spring"), Role.MANAGER)
    assert RoleGrant(Scope(org, "spring"), Role.MANAGER) not in await forge.orgs.roles_of(user_id)


async def test_a_workspace_takes_submissions_and_a_task_lists_none_published(
    forge: ForgejoForge, org: str, user: dict[str, Any]
) -> None:
    contest = await forge.content.create_contest(OrgName(org), "autumn", {"contest.yaml": b"x\n"})
    task = await forge.content.create_task(contest, "sum", {"task.yaml": b"y\n"})
    workspace = await forge.workspaces.open_workspace(
        contest, UserOwner(user["login"]), [int(user["id"])], [task]
    )

    first = await forge.workspaces.record_submission(
        workspace, task, {"main.py": b"print(1)\n"}, submitter_id=int(user["id"])
    )
    second = await forge.workspaces.record_submission(
        workspace, task, {"main.py": b"print(2)\n"}, submitter_id=int(user["id"])
    )
    assert await forge.workspaces.list_submissions(workspace, task) == (first, second)

    await forge.workspaces.close_workspace(workspace, [int(user["id"])])
    assert await forge.workspaces.list_publications(task) == ()


async def test_threads_are_posted_answered_and_closed(forge: ForgejoForge, org: str) -> None:
    contest = await forge.content.create_contest(OrgName(org), "winter", {"contest.yaml": b"x\n"})
    thread = await forge.threads.post_thread(
        PLATFORM, contest, ThreadKind.ANNOUNCEMENT, title="Welcome", body="Hello"
    )
    await forge.threads.edit_thread(PLATFORM, thread, title="Welcome all", body="Hello all")
    await forge.threads.comment(PLATFORM, thread, "Noted", answered=True)

    (listed,) = await forge.threads.list_threads(PLATFORM, contest, ThreadKind.ANNOUNCEMENT)
    assert listed.id == thread
    assert listed.title == "Welcome all"
    assert listed.answered is True
    assert listed.closed is True
    assert [comment.body for comment in listed.comments] == ["Noted"]
    assert await forge.threads.list_threads(PLATFORM, contest, ThreadKind.CLARIFICATION) == ()


async def test_workflows_are_created_versioned_searched_and_copied(
    forge: ForgejoForge, org: str
) -> None:
    workflow = await forge.workflows.create_workflow(
        PLATFORM, org, "classic", {"workflow.yaml": b"steps: []\n"}, Visibility.PUBLIC
    )
    await forge.workflows.create_workflow_version(PLATFORM, workflow, "v1")
    read = await forge.workflows.read_workflow_file(PLATFORM, workflow, "v1", "workflow.yaml")
    assert read.content == b"steps: []\n"

    found = await forge.workflows.search_public_workflows("classic")
    assert workflow in [entry.id for entry in found]
    assert next(entry for entry in found if entry.id == workflow).versions == ("v1",)

    copied = await forge.workflows.copy_workflow(PLATFORM, workflow, "v1", org, "mine")
    copy = await forge.workflows.read_workflow_file(PLATFORM, copied, "main", "workflow.yaml")
    assert copy.content == b"steps: []\n"

    await forge.workflows.set_workflow_visibility(PLATFORM, workflow, Visibility.PRIVATE)
    public = await forge.workflows.search_public_workflows("classic")
    assert workflow not in [entry.id for entry in public]
