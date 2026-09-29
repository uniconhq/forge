"""A contest's or a task's files, read and written as the organiser. Reading
needs observer and writing manager, both made as the organiser's own
identity; a stale token is a conflict; `contest.yaml` is refused whole when
it is not valid and a manager's change to an admin-only key is refused
naming it, with nothing written either way; a task's write is its save; and
a rollback writes the old content as a new change.
"""

import pytest

from forge.domain.errors import AdminOnly, Conflict, Forbidden, InvalidPath, NotFound
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Role, Scope
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import files
from forge.services.access import Organiser
from forge.services.publications import Published
from tests.services.conftest import Acme, organiser


@pytest.fixture
async def manager(setup: Setup, acme: Acme, spring: ContestId) -> Organiser:
    """bob, a manager of the contest acme/spring."""
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    found = await organiser(setup, acme.fake, 8, Scope("acme", "spring"), Role.MANAGER)
    acme.fake.reset_calls()
    return found


async def test_an_observer_reads_the_file_the_tree_and_the_history_as_themself(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring", "sum"), Role.OBSERVER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring", "sum"))

    found = await files.read(setup, bob, sum_task, "task.yaml")
    listed = await files.tree(setup, bob, sum_task)
    folder = await files.tree(setup, bob, sum_task, "data")
    changes = await files.history(setup, bob, sum_task, "statement.md")

    assert found.content.startswith(b"# The task's settings")
    assert [entry.path for entry in listed] == ["checker", "data", "statement.md", "task.yaml"]
    assert [entry.path for entry in folder] == ["data/testcases"]
    assert [change.message for change in changes] == ["Create"]
    assert {
        call.identity
        for call in acme.fake.calls
        if call.operation in {"read_file", "list_tree", "history"}
    } == {bob.identity}
    with pytest.raises(Forbidden, match="manager role at acme/spring/sum"):
        await files.write(setup, bob, sum_task, "notes.md", b"x", None)


async def test_someone_outside_the_place_reads_nothing(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "autumn"), Role.ADMIN)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "autumn"))

    with pytest.raises(Forbidden, match="observer role at acme/spring"):
        await files.read(setup, bob, spring, "contest.yaml")
    with pytest.raises(NotFound):
        await files.read(setup, bob, ContestId("acme"), "contest.yaml")
    assert acme.fake.calls_to("read_file") == []


async def test_a_manager_writes_a_manager_key_and_a_stale_token_is_a_conflict(
    setup: Setup, acme: Acme, spring: ContestId, manager: Organiser
) -> None:
    before = await files.read(setup, manager, spring, "contest.yaml")
    changed = before.content.replace(b"max_size: 3", b"max_size: 4")

    version = await files.write(setup, manager, spring, "contest.yaml", changed, before.token)

    assert isinstance(version, str)
    (call,) = acme.fake.calls_to("write_file")
    assert call.identity == manager.identity
    assert (await files.history(setup, manager, spring, "contest.yaml"))[0].author_id == 8
    with pytest.raises(Conflict):
        await files.write(setup, manager, spring, "contest.yaml", changed, before.token)


@pytest.mark.parametrize(
    ("old", "new", "keys"),
    [
        (b'name: "Spring 2026"', b'name: "Mine"', ["name"]),
        (b"state: draft", b"state: published", ["state"]),
        (b"  mode: open", b"  mode: invite-only", ["registration"]),
        (b"visibility: signed-in", b"visibility: public", ["visibility"]),
    ],
)
async def test_a_managers_change_to_an_admin_only_key_is_refused_and_an_admins_is_not(
    setup: Setup,
    acme: Acme,
    spring: ContestId,
    manager: Organiser,
    old: bytes,
    new: bytes,
    keys: list[str],
) -> None:
    before = await files.read(setup, manager, spring, "contest.yaml")
    changed = before.content.replace(old, new)
    assert changed != before.content

    with pytest.raises(AdminOnly) as refused:
        await files.write(setup, manager, spring, "contest.yaml", changed, before.token)

    assert refused.value.code == "admin_only"
    assert refused.value.extra["keys"] == keys
    assert acme.fake.calls_to("write_file") == []
    await files.write(setup, acme.ada, spring, "contest.yaml", changed, before.token)
    assert len(acme.fake.calls_to("write_file")) == 1


async def test_an_invalid_contest_yaml_is_refused_whole_naming_each_path(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    before = await files.read(setup, acme.ada, spring, "contest.yaml")
    broken = before.content.replace(b"state: draft", b"state: open").replace(
        b"max_size: 3", b"max_size: 0"
    )

    with pytest.raises(InvalidDefinition) as refused:
        await files.write(setup, acme.ada, spring, "contest.yaml", broken, before.token)

    assert {problem["path"] for problem in refused.value.errors} == {"state", "teams.max_size"}
    assert acme.fake.calls_to("write_file") == []


async def test_a_rollback_writes_the_old_content_as_a_new_change(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    first = await files.read(setup, acme.ada, spring, "contest.yaml")
    (created,) = await files.history(setup, acme.ada, spring, "contest.yaml")
    edited = first.content.replace(b"max_size: 3", b"max_size: 5")
    await files.write(setup, acme.ada, spring, "contest.yaml", edited, first.token)
    current = await files.read(setup, acme.ada, spring, "contest.yaml")

    version = await files.rollback(
        setup, acme.ada, spring, "contest.yaml", created.version, current.token
    )

    assert (await files.read(setup, acme.ada, spring, "contest.yaml")).content == first.content
    history = await files.history(setup, acme.ada, spring, "contest.yaml")
    assert history[0].version == version
    assert len(history) == 3
    assert history[0].message == f"Roll back contest.yaml to {created.version}"
    assert history[2].version == created.version
    with pytest.raises(NotFound):
        await files.rollback(setup, acme.ada, spring, "nothing.md", created.version, current.token)


async def test_a_tasks_write_is_a_save_and_its_rollback_a_save_too(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    statement = await files.read(setup, acme.ada, sum_task, "statement.md")
    (created,) = await files.history(setup, acme.ada, sum_task, "statement.md")

    first = await files.write(setup, acme.ada, sum_task, "statement.md", b"Add.\n", statement.token)
    current = await files.read(setup, acme.ada, sum_task, "statement.md")
    second = await files.rollback(
        setup, acme.ada, sum_task, "statement.md", VersionId(created.version), current.token
    )

    assert isinstance(first, Published) and isinstance(second, Published)
    assert (first.number, second.number) == (1, 2)
    assert (await files.read(setup, acme.ada, sum_task, "statement.md")).content == (
        statement.content
    )
    assert len(acme.fake.calls_to("write_file")) == 0
    assert len(acme.fake.calls_to("save_files")) == 2


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../other.task/task.yaml", "data//x", "a/./b", "x?ref=main", "a#b"]
)
async def test_a_path_that_is_not_plainly_inside_the_place_is_refused_unread(
    setup: Setup, acme: Acme, spring: ContestId, manager: Organiser, path: str
) -> None:
    with pytest.raises(InvalidPath):
        await files.read(setup, manager, spring, path)
    with pytest.raises(InvalidPath):
        await files.write(setup, manager, spring, path, b"x", None)
    assert acme.fake.calls_to("read_file") == acme.fake.calls_to("write_file") == []
