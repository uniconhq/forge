"""The in-memory forge behaves like a forge: a path from org to submission
completes against it, every call is recorded with its identity, and it refuses
what a real forge refuses.
"""

from collections.abc import Awaitable, Callable

import pytest

from forge.domain.content import ConflictToken
from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.identity import PLATFORM, AsOrgAccount, AsUser, Platform
from forge.domain.ids import (
    ContestId,
    OrgName,
    TaskId,
    ThreadId,
    VersionId,
    WorkflowId,
    WorkspaceId,
)
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
    head = await fake.content.list_files(PLATFORM, task)
    publication = await fake.workspaces.publish(task, head.version, "grading_changed: false\n")
    workspace = await fake.workspaces.open_workspace(contest, UserOwner("bob"), [8])
    await fake.workspaces.open_submission_place(workspace, task, [8])
    bob = _as(fake, 8)
    submission = await fake.workspaces.record_submission(
        bob, workspace, task, {"main.py": b"print(1)"}, key="key-12345678"
    )

    assert [entry.id for entry in await fake.workspaces.list_publications(task)] == [publication]
    assert await fake.workspaces.list_submissions(workspace, task) == (submission,)
    assert (submission.number, submission.key) == (1, "key-12345678")
    assert await fake.orgs.roles_of(_as(fake, 7)) == (RoleGrant(Scope("acme"), Role.ADMIN),)
    assert fake.calls_to("record_submission")[0].identity == bob
    fake.add_user(9, "eve")
    with pytest.raises(Forbidden):
        await fake.workspaces.record_submission(
            _as(fake, 9), workspace, task, {"a": b"b"}, key="key-87654321"
        )
    assert fake.calls_to("activate") == []


@pytest.mark.parametrize(
    "call",
    [
        lambda fake: fake.content.create_task(ContestId("acme"), "sum", {}),
        lambda fake: fake.workspaces.list_publications(TaskId("acme/spring")),
        lambda fake: fake.workspaces.list_submissions(
            WorkspaceId("acme/spring/sum"), TaskId("acme/spring/sum")
        ),
        lambda fake: fake.workflows.star_workflow(PLATFORM, WorkflowId("ada")),
        lambda fake: fake.threads.close_thread(PLATFORM, ThreadId("acme/spring.contest")),
    ],
)
async def test_a_malformed_id_names_nothing(
    fake: FakeForge, call: Callable[[FakeForge], Awaitable[object]]
) -> None:
    with pytest.raises(NotFound):
        await call(fake)


async def test_grading_is_done_as_the_org_account_handed_in(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {})
    task = await fake.content.create_task(contest, "sum", {})
    acme = AsOrgAccount("acme", forge_token="f", ci_token="c")

    await fake.grading.activate(acme, task)
    await fake.grading.start_run(acme, task, variables={}, compute_label="box")

    assert [call.identity for call in fake.calls_to("activate")] == [acme]
    assert [call.identity for call in fake.calls_to("start_run")] == [acme]
    with pytest.raises(Forbidden):
        await fake.grading.activate(AsOrgAccount("other", forge_token="f", ci_token="c"), task)


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
    workspace = await fake.workspaces.open_workspace(contest, UserOwner("bob"), [8])
    bob = _as(fake, 8)
    await fake.threads.post_thread(bob, workspace, ThreadKind.CLARIFICATION, title="Q", body="?")
    autumn = await fake.content.create_contest(OrgName("acme"), "autumn", {})
    other = await fake.workspaces.open_workspace(autumn, UserOwner("bob"), [8])
    assert await fake.threads.list_threads(bob, other, ThreadKind.CLARIFICATION) == ()

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


async def test_a_deleted_users_questions_still_read_as_nobodys(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {})
    workspace = await fake.workspaces.open_workspace(contest, UserOwner("bob"), [8])
    bob = _as(fake, 8)
    thread = await fake.threads.post_thread(
        bob, workspace, ThreadKind.CLARIFICATION, title="Q", body="?"
    )
    await fake.threads.comment(PLATFORM, thread, "A")
    await fake.threads.comment(bob, thread, "Thanks")

    await fake.identity.delete_user(8)

    (listed,) = await fake.threads.list_threads(PLATFORM, workspace, ThreadKind.CLARIFICATION)
    assert listed.author_id is None
    assert [comment.author_id for comment in listed.comments] == [None, None]
    assert [comment.body for comment in listed.comments] == ["A", "Thanks"]


async def test_a_service_account_is_made_placed_and_signed_in_at_the_ci(
    fake: FakeForge,
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.create_roles(OrgName("acme"))

    account = await fake.identity.create_user(
        "unicon-ci-acme", "unicon-ci-acme@unicon.invalid", "pw-1", must_change_password=False
    )
    assert account.id == 9
    assert await fake.identity.find_user_by_username("Unicon-CI-Acme") == account
    with pytest.raises(Conflict):
        await fake.identity.create_user("unicon-ci-acme", "x@y", "pw", must_change_password=False)
    with pytest.raises(NotFound):
        await fake.identity.find_user_by_username("nobody")
    assert "pw-1" not in str(fake.calls_to("create_user")[0].arguments)

    assert await fake.orgs.ensure_account_membership(OrgName("acme"), account.id) is True
    assert await fake.orgs.ensure_account_membership(OrgName("acme"), account.id) is False
    assert fake.state.orgs["acme"].account_members == {account.id}

    token = await fake.identity.mint_token("unicon-ci-acme", "pw-1", name="unicon", scopes=["a"])
    assert fake.state.tokens[token] == account.id
    with pytest.raises(Forbidden):
        await fake.identity.mint_token("unicon-ci-acme", "wrong", name="unicon", scopes=["a"])
    await fake.identity.set_password(account.id, "pw-2")
    with pytest.raises(Forbidden):
        await fake.grading.mint_ci_token("unicon-ci-acme", "pw-1")
    with pytest.raises(Forbidden, match="admits no user"):
        await fake.grading.mint_ci_token("unicon-ci-acme", "pw-2")

    assert await fake.grading.create_ci_user("unicon-ci-acme") == 1
    assert await fake.grading.create_ci_user("unicon-ci-acme") == 1
    ci_token = await fake.grading.mint_ci_token("unicon-ci-acme", "pw-2")
    acme = AsOrgAccount("acme", forge_token=token, ci_token=ci_token)
    assert await fake.grading.ci_user_is_alive(acme) is True
    fake.ci_dead.add("unicon-ci-acme")
    assert await fake.grading.ci_user_is_alive(acme) is False
    renewed = await fake.grading.mint_ci_token("unicon-ci-acme", "pw-2")
    assert await fake.grading.ci_user_is_alive(
        AsOrgAccount("acme", forge_token=token, ci_token=renewed)
    )
    assert fake.calls_to("ci_user_is_alive")[0].identity == acme


async def test_anyones_roles_are_read_as_the_platform(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(8, Scope("acme"), Role.OBSERVER)
    await fake.orgs.grant_role(8, Scope("acme", "spring", "sum"), Role.MANAGER)

    assert set(await fake.orgs.roles_of_user(8)) == {
        RoleGrant(Scope("acme"), Role.OBSERVER),
        RoleGrant(Scope("acme", "spring", "sum"), Role.MANAGER),
    }
    assert await fake.orgs.roles_of_user(7) == ()
    assert fake.calls_to("roles_of_user")[0].identity == PLATFORM
    with pytest.raises(NotFound):
        await fake.orgs.roles_of_user(99)


async def test_holders_are_listed_whatever_their_name_and_every_one_can_be_revoked(
    fake: FakeForge,
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    lookalike = fake.add_user(9, "unicon-ci-acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    await fake.orgs.grant_role(lookalike.id, Scope("acme"), Role.ADMIN)

    holders = await fake.orgs.holders_of(Scope("acme"), Role.ADMIN)
    assert [holder.id for holder in holders] == [7, 9]
    await fake.orgs.revoke_role(lookalike.id, Scope("acme"), Role.ADMIN)
    await fake.orgs.revoke_role(7, Scope("acme"), Role.ADMIN)
    assert await fake.orgs.holders_of(Scope("acme"), Role.ADMIN) == ()


async def test_an_event_push_is_made_once_per_url(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")

    await fake.orgs.create_event_push(OrgName("acme"), url="http://backend/events", secret="s1")
    await fake.orgs.create_event_push(OrgName("acme"), url="http://backend/events", secret="s2")

    assert fake.state.orgs["acme"].event_push == ("http://backend/events", "s1")
    assert "s1" not in str(fake.calls_to("create_event_push")[0].arguments)
    await fake.orgs.update_org(OrgName("acme"), description="Acme Corp", display_name="ACME")
    assert (fake.state.orgs["acme"].description, fake.state.orgs["acme"].display_name) == (
        "Acme Corp",
        "ACME",
    )


async def _contest_and_task(fake: FakeForge) -> tuple[ContestId, TaskId]:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {"contest.yaml": b"c"})
    task = await fake.content.create_task(contest, "sum", {"task.yaml": b"t", "data/a.in": b"1"})
    return contest, task


async def test_a_contest_role_reaches_a_task_only_once_it_is_secured(fake: FakeForge) -> None:
    contest, task = await _contest_and_task(fake)
    await fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    bob = _as(fake, 8)

    with pytest.raises(Forbidden):
        await fake.content.read_file(bob, task, "task.yaml")
    assert await fake.content.secure(contest) == 4
    assert await fake.content.secure(task) == 8
    assert await fake.content.secure(task) == 0

    assert (await fake.content.read_file(bob, task, "task.yaml")).content == b"t"
    repo = fake.state.repos[("acme", "spring.sum.task")]
    assert repo.teams == {Scope("acme", "spring"), Scope("acme", "spring", "sum")}
    assert repo.rewrites_refused is True
    assert repo.reserved == {"published/"}
    assert fake.state.repos[("acme", "spring.contest")].reserved == set()
    repo.teams.discard(Scope("acme", "spring"))
    assert await fake.content.secure(task) == 3
    assert {call.identity for call in fake.calls_to("secure")} == {PLATFORM}


async def test_contests_and_tasks_are_there_and_listed_as_the_reader_sees_them(
    fake: FakeForge,
) -> None:
    contest, task = await _contest_and_task(fake)
    other = await fake.content.create_contest(OrgName("acme"), "autumn", {})
    await fake.content.secure(contest)
    await fake.content.secure(other)
    await fake.orgs.grant_role(8, Scope("acme", "autumn"), Role.OBSERVER)

    assert await fake.content.exists(task) is True
    assert await fake.content.exists(TaskId("acme/spring/nope")) is False
    assert await fake.content.list_contests(PLATFORM, OrgName("acme")) == (other, contest)
    assert await fake.content.list_contests(_as(fake, 8), OrgName("acme")) == (other,)
    assert await fake.content.list_tasks(PLATFORM, contest) == (task,)
    assert await fake.content.list_tasks(_as(fake, 8), contest) == ()


async def test_a_save_is_one_change_as_the_person_checked_file_by_file(fake: FakeForge) -> None:
    _, task = await _contest_and_task(fake)
    await fake.orgs.grant_role(7, Scope("acme"), Role.MANAGER)
    ada = _as(fake, 7)
    before = await fake.content.list_files(ada, task)
    history = len(fake.state.repos[("acme", "spring.sum.task")].history)

    with pytest.raises(Conflict):
        await fake.content.save_files(
            ada,
            task,
            {"task.yaml": b"t2", "data/b.in": b"2"},
            expected={"task.yaml": ConflictToken("stale")},
            message="Save",
        )
    with pytest.raises(Conflict):
        await fake.content.save_files(ada, task, {"data/a.in": b"9"}, expected={}, message="Save")
    assert len(fake.state.repos[("acme", "spring.sum.task")].history) == history

    version = await fake.content.save_files(
        ada,
        task,
        {"task.yaml": b"t2", "data/b.in": b"2", "data/a.in": None},
        expected={"task.yaml": before.tokens["task.yaml"], "data/a.in": before.tokens["data/a.in"]},
        message="Save",
    )

    after = await fake.content.list_files(ada, task)
    assert after.version == version
    assert sorted(after.tokens) == ["data/b.in", "task.yaml"]
    assert after.has("data/") and not after.has("checker/")
    (change, *_) = await fake.content.history(ada, task)
    assert (change.version, change.author_id, change.message) == (version, 7, "Save")
    assert [entry.version for entry in await fake.content.history(ada, task, "data/b.in")] == [
        version
    ]
    old = await fake.content.read_file(ada, task, "task.yaml", at=before.version)
    assert old.content == b"t"
    assert (await fake.content.list_files(ada, task, at=before.version)).tokens == before.tokens


async def test_a_publication_names_a_version_with_its_note_and_numbers_follow(
    fake: FakeForge,
) -> None:
    _, task = await _contest_and_task(fake)
    first = await fake.content.list_files(PLATFORM, task)
    await fake.content.save_files(
        PLATFORM, task, {"statement.md": b"s"}, expected={}, message="Save"
    )
    second = await fake.content.list_files(PLATFORM, task)

    one = await fake.workspaces.publish(task, first.version, "grading_changed: false\n")
    two = await fake.workspaces.publish(
        task, second.version, "grading_changed: true\nchanges:\n- limits.rate changed\n"
    )

    listed = await fake.workspaces.list_publications(task)
    assert [(entry.id, entry.number, entry.version) for entry in listed] == [
        (one, 1, first.version),
        (two, 2, second.version),
    ]
    assert (listed[0].grading_changed, listed[0].changes) == (False, ())
    assert (listed[1].grading_changed, listed[1].changes) == (True, ("limits.rate changed",))
    assert second.version == fake.state.repos[("acme", "spring.sum.task")].head
    with pytest.raises(NotFound):
        await fake.workspaces.publish(task, VersionId("nowhere"), "")
