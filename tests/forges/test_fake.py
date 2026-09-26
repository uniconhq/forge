"""The in-memory forge behaves like a forge: a path from org to submission
completes against it, every call is recorded with its identity, and it refuses
what a real forge refuses.
"""

import pytest

from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.identity import PLATFORM, AsUser, Platform
from forge.domain.ids import OrgName
from forge.domain.names import UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.threads import ThreadKind
from forge.domain.workflows import Visibility
from forge.forges.fake import FakeForge


def _as(fake: FakeForge, user_id: int) -> AsUser:
    return AsUser(user_id=user_id, credential=fake.mint(user_id))


async def test_a_path_from_org_to_submission_completes(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {"contest.yaml": b"x"})
    task = await fake.content.create_task(contest, "sum", {"task.yaml": b"y"})
    publication = await fake.workspaces.publish(task, {"plans/public.json": b"{}"})
    workspace = await fake.workspaces.open_workspace(contest, UserOwner("bob"), [8], [task])
    submission = await fake.workspaces.record_submission(
        workspace, task, {"main.py": b"print(1)"}, submitter_id=8
    )

    assert await fake.workspaces.list_publications(task) == (publication,)
    assert await fake.workspaces.list_submissions(workspace, task) == (submission,)
    assert await fake.orgs.roles_of(7) == (RoleGrant(Scope("acme"), Role.ADMIN),)
    assert fake.calls_to("register")[0].identity.org == "acme"  # type: ignore[union-attr]


async def test_every_call_is_recorded_with_its_identity(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.MANAGER)
    ada = _as(fake, 7)
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {})
    await fake.content.write_file(ada, contest, "contest.yaml", b"x", message="Edit", expected=None)

    assert fake.calls_to("write_file")[-1].identity == ada
    assert fake.calls_to("create_org")[0].identity is PLATFORM
    fake.reset_calls()
    assert fake.calls == []


async def test_a_stale_conflict_check_is_refused(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.MANAGER)
    ada = _as(fake, 7)
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {"contest.yaml": b"one"})
    first = await fake.content.read_file(ada, contest, "contest.yaml")
    await fake.content.write_file(
        ada, contest, "contest.yaml", b"two", message="Edit", expected=first.token
    )

    with pytest.raises(Conflict):
        await fake.content.write_file(
            ada, contest, "contest.yaml", b"three", message="Edit", expected=first.token
        )
    with pytest.raises(Conflict):
        await fake.content.write_file(
            ada, contest, "contest.yaml", b"four", message="Create", expected=None
        )


async def test_a_protected_version_by_anyone_but_the_platform_is_refused(fake: FakeForge) -> None:
    ada = _as(fake, 7)
    workflow = await fake.workflows.create_workflow(
        ada, "ada", "classic", {"workflow.yaml": b""}, Visibility.PRIVATE
    )
    await fake.workflows.create_workflow_version(ada, workflow, "v1")

    with pytest.raises(Forbidden):
        await fake.workflows.create_workflow_version(ada, workflow, "published/1")
    assert isinstance(fake.calls_to("create_workflow_version")[-1].identity, AsUser)


async def test_a_read_without_access_is_refused(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {"contest.yaml": b"x"})

    with pytest.raises(Forbidden):
        await fake.content.read_file(_as(fake, 8), contest, "contest.yaml")
    assert (await fake.content.read_file(_as(fake, 7), contest, "contest.yaml")).content == b"x"
    assert (await fake.content.read_file(PLATFORM, contest, "contest.yaml")).content == b"x"


async def test_a_contest_manager_reads_a_clarification_and_a_stranger_does_not(
    fake: FakeForge,
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    fake.add_user(9, "eve")
    await fake.orgs.grant_role(9, Scope("acme", "spring"), Role.MANAGER)
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {})
    workspace = await fake.workspaces.open_workspace(contest, UserOwner("bob"), [8], [])
    bob = _as(fake, 8)
    await fake.threads.post_thread(bob, workspace, ThreadKind.CLARIFICATION, title="Q", body="?")

    assert len(await fake.threads.list_threads(bob, workspace, ThreadKind.CLARIFICATION)) == 1
    assert (
        len(await fake.threads.list_threads(_as(fake, 9), workspace, ThreadKind.CLARIFICATION)) == 1
    )
    with pytest.raises(Forbidden):
        await fake.threads.list_threads(_as(fake, 7), workspace, ThreadKind.CLARIFICATION)


async def test_a_copy_carries_the_mark_and_belongs_to_the_copier(fake: FakeForge) -> None:
    ada, bob = _as(fake, 7), _as(fake, 8)
    source = await fake.workflows.create_workflow(
        ada, "ada", "classic", {"workflow.yaml": b"v"}, Visibility.PUBLIC
    )
    await fake.workflows.create_workflow_version(ada, source, "v1")

    copied = await fake.workflows.copy_workflow(bob, source, "v1", "bob", "mine")

    owned = await fake.workflows.workflows_owned_by(8)
    assert [workflow.id for workflow in owned] == [copied]
    assert owned[0].visibility is Visibility.PRIVATE
    assert (await fake.workflows.search_public_workflows("classic"))[0].id == source


async def test_deleting_a_user_removes_them_and_what_they_own(fake: FakeForge) -> None:
    ada = _as(fake, 7)
    await fake.workflows.create_workflow(ada, "ada", "classic", {}, Visibility.PRIVATE)
    await fake.identity.delete_user(7)

    with pytest.raises(NotFound):
        await fake.identity.find_user(7)
    assert fake.state.repos == {}
    assert isinstance(fake.calls_to("delete_user")[0].identity, Platform)
