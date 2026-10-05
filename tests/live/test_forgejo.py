"""The Forgejo implementation against a real Forgejo. These run only when
`UNICON_LIVE_FORGE_URL` and `UNICON_LIVE_FORGE_ADMIN_TOKEN` name a running
instance whose platform account may create users and orgs; every name they
create carries a random suffix and is removed afterwards. The tests that
need the CI run only when `UNICON_LIVE_CI_URL`, `UNICON_LIVE_CI_PUBLIC_URL`,
`UNICON_LIVE_CI_ADMIN_TOKEN` and `UNICON_LIVE_FORGE_PUBLIC_URL` name a
running Woodpecker signed in through that Forgejo, and the forge as a
browser reaches it. The contest and task content is in `test_content.py`,
and the workflows people make in `test_workflows.py`.
"""

import contextlib
import secrets
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import OrgId, ThreadId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.threads import ThreadKind
from forge.domain.uploads import POINTER_MAX, pointer_text
from forge.domain.workflows import Visibility
from forge.forges.forgejo import ForgejoForge
from tests.live.conftest import (
    CI_ADMIN_TOKEN,
    CI_URL,
    HAS_CI,
    LIVE,
    as_person,
    delete_org,
    delete_user,
    make_user,
    needs_ci,
)

pytestmark = LIVE


@pytest.fixture(scope="module")
def user(admin: httpx.Client, stamp: str) -> Iterator[dict[str, Any]]:
    person = make_user(admin, f"live-{stamp}")
    yield person
    delete_user(admin, person["login"])


@pytest.fixture(scope="module")
def person(admin: httpx.Client, user: dict[str, Any]) -> AsUser:
    """The user acting as themself."""
    return as_person(admin, user)


@pytest.fixture(scope="module")
def org(admin: httpx.Client, stamp: str) -> Iterator[str]:
    name = f"live-org-{stamp}"
    yield name
    delete_org(admin, name)


@pytest.fixture(scope="module")
def account_name(admin: httpx.Client, org: str) -> Iterator[str]:
    """The org's service account's username; the tests make the account and
    this removes it, at the forge and at the CI, afterwards.
    """
    name = f"unicon-ci-{org}"
    yield name
    admin.delete(f"/api/v1/admin/users/{name}", params={"purge": "true"})
    if HAS_CI:
        assert CI_URL and CI_ADMIN_TOKEN
        httpx.delete(
            f"{CI_URL.rstrip('/')}/api/users/{name}",
            headers={"Authorization": f"Bearer {CI_ADMIN_TOKEN}"},
            timeout=30,
        )


async def _org_exists(forge: ForgejoForge, org: str) -> None:
    with contextlib.suppress(Conflict):
        await forge.orgs.create_org(OrgId(org), description="Live test org")
    await forge.orgs.create_roles(OrgId(org))


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
    forge: ForgejoForge, org: str, user: dict[str, Any], person: AsUser
) -> None:
    await forge.orgs.create_org(OrgId(org), description="Live test org")
    with pytest.raises(Conflict):
        await forge.orgs.create_org(OrgId(org), description="again")
    await forge.orgs.create_roles(OrgId(org))
    await forge.orgs.create_roles(OrgId(org))
    await forge.orgs.create_thread_labels(OrgId(org))
    await forge.orgs.create_thread_labels(OrgId(org))

    user_id = int(user["id"])
    await forge.orgs.grant_role(user_id, Scope(org), Role.ADMIN)
    assert await forge.orgs.roles_of(person) == (RoleGrant(Scope(org), Role.ADMIN),)
    assert [holder.id for holder in await forge.orgs.holders_of(Scope(org), Role.ADMIN)] == [
        user_id
    ]

    contest = await forge.content.create_contest(
        OrgId(org), "spring", {"contest.yaml": b"name: Spring\n"}
    )
    task = await forge.content.create_task(contest, "sum", {"task.yaml": b"name: Sum\n"})
    await forge.content.secure(contest)
    await forge.content.secure(task)

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
    assert RoleGrant(Scope(org, "spring"), Role.MANAGER) in await forge.orgs.roles_of(person)
    await forge.orgs.revoke_role(user_id, Scope(org, "spring"), Role.MANAGER)
    assert RoleGrant(Scope(org, "spring"), Role.MANAGER) not in await forge.orgs.roles_of(person)


async def test_anyones_roles_are_read_as_the_platform_and_a_contest_role_comes_and_goes(
    forge: ForgejoForge, org: str, user: dict[str, Any], person: AsUser, admin: httpx.Client
) -> None:
    await _org_exists(forge, org)
    user_id = int(user["id"])
    contest = Scope(org, "roles")
    team = f"{org}.roles-observer"

    await forge.orgs.grant_role(user_id, contest, Role.OBSERVER)
    try:
        read = await forge.orgs.roles_of_user(user_id)
        assert RoleGrant(contest, Role.OBSERVER) in read
        assert set(read) == set(await forge.orgs.roles_of(person))
        assert user_id in [
            holder.id for holder in await forge.orgs.holders_of(contest, Role.OBSERVER)
        ]
        teams = admin.get(f"/api/v1/orgs/{org}/teams", params={"limit": 50}).json()
        (made,) = [entry for entry in teams if entry["name"] == team]
        assert made["includes_all_repositories"] is False
        members = admin.get(f"/api/v1/teams/{made['id']}/members").json()
        assert [member["login"] for member in members] == [user["login"]]

        await forge.orgs.revoke_role(user_id, contest, Role.OBSERVER)
        assert RoleGrant(contest, Role.OBSERVER) not in await forge.orgs.roles_of_user(user_id)
        assert admin.get(f"/api/v1/teams/{made['id']}/members").json() == []
    finally:
        for entry in admin.get(f"/api/v1/orgs/{org}/teams", params={"limit": 50}).json():
            if entry["name"].startswith(f"{org}.roles-"):
                admin.delete(f"/api/v1/teams/{entry['id']}")
    with pytest.raises(NotFound):
        await forge.orgs.roles_of_user(2_000_000_000)


async def test_a_workspace_takes_submissions_as_the_contestant_at_their_own_commit(
    forge: ForgejoForge, org: str, admin: httpx.Client, stamp: str
) -> None:
    contestant = make_user(admin, f"contestant-{stamp}")
    person = as_person(admin, contestant)
    member = [int(contestant["id"])]
    try:
        contest = await forge.content.create_contest(OrgId(org), "autumn", {"contest.yaml": b"x\n"})
        task = await forge.content.create_task(contest, "sum", {"task.yaml": b"y\n"})
        for _ in range(2):
            workspace = await forge.workspaces.open_workspace(
                contest, UserOwner(int(contestant["id"])), member
            )
            await forge.workspaces.open_submission_place(workspace, task, member)

        first = await forge.workspaces.record_submission(
            person, workspace, task, {"main.py": b"print(1)\n"}, key="live-key-one"
        )
        second = await forge.workspaces.record_submission(
            person,
            workspace,
            task,
            {"files/submission/sum.py": b"print(2)\n", "submission.json": b"{}\n"},
            key="live-key-two",
        )
        assert await forge.workspaces.list_submissions(workspace, task) == (first, second)
        assert [(made.number, made.key) for made in (first, second)] == [
            (1, "live-key-one"),
            (2, "live-key-two"),
        ]
        assert await forge.workspaces.read_submission_file(
            person, first.id, "main.py", max_size=64
        ) == (b"print(1)\n")
        assert (
            await forge.workspaces.read_submission_file(
                person, second.id, "files/submission/sum.py", max_size=64
            )
            == b"print(2)\n"
        )
        with pytest.raises(NotFound):
            await forge.workspaces.read_submission_file(person, second.id, "main.py", max_size=64)
        # A pointer reads back as the pointer through the blob read, which is
        # how a recovered submission finds the uploads it used.
        pointer = pointer_text("a" * 64, 7)
        third = await forge.workspaces.record_submission(
            person, workspace, task, {"files/submission/big.bin": pointer}, key="live-key-three"
        )
        assert (
            await forge.workspaces.read_submission_blob(
                person, third.id, "files/submission/big.bin", max_size=POINTER_MAX
            )
            == pointer
        )

        repo = f"/api/v1/repos/{org}/autumn.sum.u{contestant['id']}.sub"
        tags = {tag["name"]: tag["commit"]["sha"] for tag in admin.get(f"{repo}/tags").json()}
        commits = admin.get(f"{repo}/commits", params={"sha": "main"}).json()
        by_sha = {commit["sha"]: commit for commit in commits}
        assert by_sha[tags["submission/1"]]["commit"]["message"].startswith("Submit")
        assert by_sha[tags["submission/1"]]["author"]["login"] == contestant["login"]
        assert tags["submission/1"] != tags["submission/2"]

        await forge.workspaces.close_workspace(workspace, member)
        assert await forge.workspaces.list_publications(task) == ()
        with pytest.raises((Forbidden, NotFound)):
            await forge.workspaces.record_submission(
                person, workspace, task, {"main.py": b"late"}, key="live-key-late"
            )
        assert await forge.workspaces.list_submissions(workspace, task) == (first, second, third)
    finally:
        delete_user(admin, contestant["login"])


async def test_the_same_person_has_a_workspace_in_each_contest_of_the_org(
    forge: ForgejoForge, org: str, user: dict[str, Any], person: AsUser
) -> None:
    first = await forge.content.create_contest(OrgId(org), "one", {"contest.yaml": b"x\n"})
    second = await forge.content.create_contest(OrgId(org), "two", {"contest.yaml": b"x\n"})
    owner = UserOwner(int(user["id"]))

    in_first = await forge.workspaces.open_workspace(first, owner, [int(user["id"])])
    in_second = await forge.workspaces.open_workspace(second, owner, [int(user["id"])])
    await forge.threads.post_thread(
        person, in_second, ThreadKind.CLARIFICATION, title="Q", body="?"
    )

    await forge.workspaces.close_workspace(in_first, [int(user["id"])])
    assert len(await forge.threads.list_threads(person, in_second, ThreadKind.CLARIFICATION)) == 1


async def test_threads_are_posted_edited_answered_and_closed(forge: ForgejoForge, org: str) -> None:
    contest = await forge.content.create_contest(OrgId(org), "winter", {"contest.yaml": b"x\n"})
    thread = await forge.threads.post_thread(
        PLATFORM, contest, ThreadKind.ANNOUNCEMENT, title="Welcome", body="Hello"
    )
    await forge.threads.edit_thread(PLATFORM, thread.id, title="Welcome all", body="Hello all")
    await forge.threads.comment(PLATFORM, thread.id, "Noted")
    await forge.threads.mark_answered(PLATFORM, thread.id)
    await forge.threads.mark_answered(PLATFORM, thread.id)

    (listed,) = await forge.threads.list_threads(PLATFORM, contest, ThreadKind.ANNOUNCEMENT)
    assert (listed.id, listed.place, listed.number) == (thread.id, contest, thread.number)
    assert listed.title == "Welcome all"
    assert (listed.answered, listed.closed) == (True, True)
    assert [comment.body for comment in listed.comments] == ["Noted"]
    assert await forge.threads.list_threads(PLATFORM, contest, ThreadKind.CLARIFICATION) == ()

    await forge.threads.unmark_answered(PLATFORM, thread.id)
    await forge.threads.unmark_answered(PLATFORM, thread.id)
    read = await forge.threads.read_thread(PLATFORM, thread.id)
    assert (read.answered, read.closed) == (False, False)
    await forge.threads.close_thread(PLATFORM, thread.id)
    assert (await forge.threads.read_thread(PLATFORM, thread.id)).closed is True


async def test_the_org_search_finds_the_open_questions_and_a_follow_up_reopens_one(
    forge: ForgejoForge, org: str, admin: httpx.Client, stamp: str
) -> None:
    asker = make_user(admin, f"asker-{stamp}")
    person = as_person(admin, asker)
    contest = await forge.content.create_contest(OrgId(org), "asking", {"contest.yaml": b"x\n"})
    workspace = await forge.workspaces.open_workspace(
        contest, UserOwner(int(asker["id"])), [int(asker["id"])]
    )
    thread = await forge.threads.post_thread(
        person, workspace, ThreadKind.CLARIFICATION, title="Input size?", body="How big?"
    )

    # The org is the module's, and other tests leave questions in their own
    # contests, so only what the search finds in this contest is counted.
    async def searched(*, open_only: bool = True) -> list[ThreadId]:
        found = await forge.threads.search_threads(
            PLATFORM, OrgId(org), ThreadKind.CLARIFICATION, open_only=open_only
        )
        return [entry.id for entry in found if entry.place == workspace]

    assert await searched() == [thread.id]

    await forge.threads.mark_answered(PLATFORM, thread.id)
    assert await searched() == []
    assert await searched(open_only=False) == [thread.id]

    # The asker reads their desk and comments there; the label is the
    # platform's to take off on a follow-up, as `clarifications` does.
    await forge.threads.comment(person, thread.id, "One more thing")
    with pytest.raises(Forbidden):
        await forge.threads.unmark_answered(person, thread.id)
    await forge.threads.unmark_answered(PLATFORM, thread.id)
    again = await forge.threads.read_thread(person, thread.id)
    assert (again.answered, again.closed) == (False, False)


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


async def test_a_deleted_user_is_gone_and_their_questions_still_read(
    forge: ForgejoForge, org: str, admin: httpx.Client, stamp: str
) -> None:
    leaver = make_user(admin, f"leaver-{stamp}")
    person = as_person(admin, leaver)
    contest = await forge.content.create_contest(OrgId(org), "leaving", {"contest.yaml": b"x\n"})
    workspace = await forge.workspaces.open_workspace(
        contest, UserOwner(int(leaver["id"])), [int(leaver["id"])]
    )
    thread = await forge.threads.post_thread(
        person, workspace, ThreadKind.CLARIFICATION, title="Before I go", body="?"
    )
    await forge.threads.comment(PLATFORM, thread.id, "An answer")
    await forge.threads.comment(person, thread.id, "Thanks")
    await forge.workflows.create_workflow(
        person, leaver["login"], "private", {"workflow.yaml": b"steps: []\n"}, Visibility.PRIVATE
    )

    await forge.identity.delete_user(int(leaver["id"]))

    with pytest.raises(NotFound):
        await forge.identity.find_user(int(leaver["id"]))
    assert admin.get(f"/api/v1/repos/{leaver['login']}/private.workflow").status_code == 404
    (listed,) = await forge.threads.list_threads(PLATFORM, workspace, ThreadKind.CLARIFICATION)
    assert listed.title == "Before I go"
    assert listed.author_id is None
    assert [comment.body for comment in listed.comments] == ["An answer", "Thanks"]
    assert listed.comments[1].author_id is None


async def test_an_org_gets_one_event_push_however_often_it_is_asked(
    forge: ForgejoForge, org: str, admin: httpx.Client
) -> None:
    await _org_exists(forge, org)
    url = f"http://backend/api/v1/events/forge/{org}"

    await forge.orgs.create_event_push(OrgId(org), url=url, secret="live-secret")
    await forge.orgs.create_event_push(OrgId(org), url=url, secret="live-secret")

    hooks = [
        hook
        for hook in admin.get(f"/api/v1/orgs/{org}/hooks").json()
        if hook["config"]["url"] == url
    ]
    assert len(hooks) == 1, hooks
    (hook,) = hooks
    assert hook["active"] is True
    assert hook["config"]["content_type"] == "json"
    assert set(hook["events"]) >= {"push", "create", "delete", "issues", "issue_comment"}
    assert admin.delete(f"/api/v1/orgs/{org}/hooks/{hook['id']}").status_code == 204


async def test_the_service_account_is_made_placed_and_given_a_token(
    forge: ForgejoForge, org: str, admin: httpx.Client, account_name: str
) -> None:
    await _org_exists(forge, org)
    password = "live-" + secrets.token_urlsafe(12)

    account = await forge.identity.create_user(
        account_name,
        f"{account_name}@unicon.invalid",
        password,
        must_change_password=False,
        visibility="private",
    )
    with pytest.raises(Conflict):
        await forge.identity.create_user(
            account_name, f"{account_name}@unicon.invalid", password, must_change_password=False
        )
    assert await forge.identity.find_user_by_username(account_name) == account
    assert await forge.orgs.ensure_account_membership(OrgId(org), account.id) is True
    assert await forge.orgs.ensure_account_membership(OrgId(org), account.id) is False
    token = await forge.identity.mint_token(
        account_name, password, name="unicon", scopes=["read:user", "read:organization"]
    )
    again = await forge.identity.mint_token(
        account_name, password, name="unicon", scopes=["read:user"]
    )

    assert account.username == account_name
    teams = admin.get(f"/api/v1/orgs/{org}/teams", params={"limit": 50}).json()
    ci_team = next(team for team in teams if team["name"] == f"{org}-ci")
    members = admin.get(f"/api/v1/teams/{ci_team['id']}/members").json()
    assert [member["login"] for member in members] == [account_name]
    assert ci_team["permission"] == "admin"
    assert token != again
    named = httpx.get(
        f"{admin.base_url}/api/v1/users/{account_name}/tokens", auth=(account_name, password)
    ).json()
    assert [entry["name"] for entry in named] == ["unicon"]
    me = httpx.get(f"{admin.base_url}/api/v1/user", headers={"Authorization": f"token {again}"})
    assert me.json()["login"] == account_name
    with pytest.raises(Forbidden):
        await forge.identity.mint_token(account_name, "wrong", name="unicon", scopes=["read:user"])

    await forge.orgs.grant_role(account.id, Scope(org), Role.OBSERVER)
    await forge.orgs.revoke_role(account.id, Scope(org), Role.OBSERVER)
    assert RoleGrant(Scope(org), Role.OBSERVER) not in await forge.orgs.roles_of_user(account.id)
    members = admin.get(f"/api/v1/teams/{ci_team['id']}/members").json()
    assert [member["login"] for member in members] == [account_name]

    fresh = "live-" + secrets.token_urlsafe(12)
    await forge.identity.set_password(account.id, fresh)
    with pytest.raises(Forbidden):
        await forge.identity.mint_token(account_name, password, name="unicon", scopes=["read:user"])
    await forge.identity.mint_token(account_name, fresh, name="unicon", scopes=["read:user"])


@needs_ci
async def test_the_ci_user_is_created_and_the_sign_in_dance_yields_a_token(
    forge: ForgejoForge, org: str, account_name: str
) -> None:
    assert CI_URL and CI_ADMIN_TOKEN
    account = await forge.identity.find_user_by_username(account_name)
    password = "live-" + secrets.token_urlsafe(12)
    await forge.identity.set_password(account.id, password)
    ci_admin = {"Authorization": f"Bearer {CI_ADMIN_TOKEN}"}

    ci_id = await forge.grading.create_ci_user(account_name)
    assert await forge.grading.create_ci_user(account_name) == ci_id
    listed = httpx.get(f"{CI_URL.rstrip('/')}/api/users/{account_name}", headers=ci_admin)
    assert listed.status_code == 200, listed.text
    assert listed.json()["id"] == ci_id

    token = await forge.grading.mint_ci_token(account_name, password)

    me = httpx.get(
        f"{CI_URL.rstrip('/')}/api/user", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    assert me.status_code == 200, me.text
    assert me.json()["login"] == account_name

    with pytest.raises(Forbidden):
        await forge.grading.mint_ci_token(account_name, "wrong")
    renewed = "live-" + secrets.token_urlsafe(12)
    await forge.identity.set_password(account.id, renewed)
    second = await forge.grading.mint_ci_token(account_name, renewed)
    again = httpx.get(
        f"{CI_URL.rstrip('/')}/api/user", headers={"Authorization": f"Bearer {second}"}, timeout=30
    )
    assert again.status_code == 200, again.text


async def test_a_role_team_made_a_repository_admin_is_given_write_back(
    forge: ForgejoForge, admin: httpx.Client, stamp: str
) -> None:
    org = f"live-perm-{stamp}"
    await forge.orgs.create_org(OrgId(org), description="Live permission check")
    try:
        await forge.orgs.create_roles(OrgId(org))
        teams = {team["name"]: team for team in admin.get(f"/api/v1/orgs/{org}/teams").json()}
        widened = teams[f"{org}-admin"]
        assert widened["permission"] == "write"
        patched = admin.patch(
            f"/api/v1/teams/{widened['id']}",
            json={"name": widened["name"], "permission": "admin", "units": ["repo.code"]},
        )
        assert patched.status_code == 200, patched.text
        assert admin.get(f"/api/v1/teams/{widened['id']}").json()["permission"] == "admin"

        await forge.orgs.create_roles(OrgId(org))

        after = {team["name"]: team for team in admin.get(f"/api/v1/orgs/{org}/teams").json()}
        assert after[f"{org}-admin"]["permission"] == "write"
        assert after[f"{org}-manager"]["permission"] == "write"
        assert after[f"{org}-observer"]["permission"] == "read"
        assert after[f"{org}-ci"]["permission"] == "admin"
    finally:
        delete_org(admin, org)


async def test_the_addresses_the_forge_confirmed_are_read_for_a_person(
    forge: ForgejoForge, admin: httpx.Client, stamp: str
) -> None:
    person = make_user(admin, f"emails-{stamp}")
    try:
        confirmed = await forge.identity.verified_emails(int(person["id"]))
    finally:
        delete_user(admin, person["login"])

    assert confirmed == (person["email"],)
