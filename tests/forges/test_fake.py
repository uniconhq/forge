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


@pytest.fixture
def fake() -> FakeForge:
    forge = FakeForge()
    forge.add_user(7, "ada")
    forge.add_user(8, "bob")
    return forge


def _as(fake: FakeForge, user_id: int) -> AsUser:
    return AsUser(user_id=user_id, credential=fake.mint(user_id))


async def test_a_path_from_org_to_submission_completes(fake: FakeForge) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme"), Role.ADMIN)
    ada = _as(fake, 7)
    contest = await fake.create_contest(ada, "acme", "spring", {"contest.yaml": b"name: Spring"})
    task = await fake.create_task(ada, contest, "sum", {"task.yaml": b"name: Sum"})
    publication = await fake.publish(task, {"plans/public.json": b"{}"})
    workspace = await fake.open_workspace(contest, UserOwner("bob"), [8], [task])
    submission = await fake.record_submission(
        workspace, task, {"main.py": b"print(1)"}, submitter_id=8
    )

    assert await fake.list_publications(task) == (publication,)
    assert await fake.list_submissions(workspace, task) == (submission,)
    assert await fake.roles_of(7) == (RoleGrant(Scope("acme"), Role.ADMIN),)


async def test_every_call_is_recorded_with_its_identity(fake: FakeForge) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme"), Role.MANAGER)
    ada = _as(fake, 7)
    contest = await fake.create_contest(ada, "acme", "spring", {})
    await fake.write_file(ada, contest, "contest.yaml", b"x", message="Edit", expected_version=None)

    write = fake.calls_to("write_file")[-1]
    assert write.identity == ada
    assert fake.calls_to("create_org")[0].identity is PLATFORM
    fake.reset_calls()
    assert fake.calls == []


async def test_a_stale_conflict_check_is_refused(fake: FakeForge) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme"), Role.MANAGER)
    ada = _as(fake, 7)
    contest = await fake.create_contest(ada, "acme", "spring", {"contest.yaml": b"one"})
    current = await fake.read_file(ada, contest, "contest.yaml")
    await fake.write_file(
        ada, contest, "contest.yaml", b"two", message="Edit", expected_version=current.version
    )

    with pytest.raises(Conflict):
        await fake.write_file(
            ada, contest, "contest.yaml", b"three", message="Edit", expected_version=current.version
        )


async def test_a_protected_version_by_anyone_but_the_platform_is_refused(fake: FakeForge) -> None:
    ada = _as(fake, 7)
    workflow = await fake.create_workflow(
        ada, "ada", "classic", {"workflow.yaml": b""}, Visibility.PRIVATE
    )
    await fake.create_workflow_version(ada, workflow, "v1")

    with pytest.raises(Forbidden):
        await fake.create_workflow_version(ada, workflow, "published/1")
    assert isinstance(fake.calls_to("create_workflow_version")[-1].identity, AsUser)


async def test_a_read_without_access_is_refused(fake: FakeForge) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme"), Role.ADMIN)
    contest = await fake.create_contest(_as(fake, 7), "acme", "spring", {"contest.yaml": b"x"})

    with pytest.raises(Forbidden):
        await fake.read_file(_as(fake, 8), contest, "contest.yaml")
    assert (await fake.read_file(PLATFORM, contest, "contest.yaml")).content == b"x"


async def test_a_clarification_is_unreadable_by_another_contestant(fake: FakeForge) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    contest = await fake.create_contest(PLATFORM, "acme", "spring", {})
    workspace = await fake.open_workspace(contest, UserOwner("bob"), [8], [])
    bob = _as(fake, 8)
    await fake.post_thread(bob, workspace, ThreadKind.CLARIFICATION, title="Q", body="?")

    assert len(await fake.list_threads(bob, workspace, ThreadKind.CLARIFICATION)) == 1
    with pytest.raises(Forbidden):
        await fake.list_threads(_as(fake, 7), workspace, ThreadKind.CLARIFICATION)


async def test_a_copy_carries_the_mark_and_belongs_to_the_copier(fake: FakeForge) -> None:
    ada, bob = _as(fake, 7), _as(fake, 8)
    source = await fake.create_workflow(
        ada, "ada", "classic", {"workflow.yaml": b"v"}, Visibility.PUBLIC
    )
    await fake.create_workflow_version(ada, source, "v1")

    copied = await fake.copy_workflow(bob, source, "v1", "bob", "mine")

    owned = await fake.workflows_owned_by(8)
    assert [workflow.id for workflow in owned] == [copied]
    assert owned[0].visibility is Visibility.PRIVATE
    assert (await fake.search_public_workflows("classic"))[0].id == source


async def test_deleting_a_user_removes_them_and_what_they_own(fake: FakeForge) -> None:
    ada = _as(fake, 7)
    await fake.create_workflow(ada, "ada", "classic", {}, Visibility.PRIVATE)
    await fake.delete_user(7)

    with pytest.raises(NotFound):
        await fake.find_user(7)
    assert fake.repos == {}
    assert isinstance(fake.calls_to("delete_user")[0].identity, Platform)
