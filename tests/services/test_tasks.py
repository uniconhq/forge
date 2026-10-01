"""Making a task is a request a manager of its contest makes and the poller
runs: the place with `task.yaml`, `statement.md` and an example testcase in
`data/testcases/`, then the roles of the contest and the task
with its protection and its publications reserved, then its entry at the
end of the `tasks` list in `contest.yaml`, written as the platform. Nothing
is published; a task the list holds already is left as it is, and a
`contest.yaml` that does not read is left alone; the status is for anyone
observing the contest; the list is read as the organiser; and the nightly pass puts back
what went missing.
"""

import logging

import pytest

from forge.domain.definitions import parse_contest, parse_task
from forge.domain.errors import Forbidden, NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contests, tasks
from forge.testing import logged, tick
from tests.services.conftest import ACME, SPRING, SUM, Acme, organiser, write_contest

CONTEST_HEAD = (
    "name: Spring 2026\nstart: 2026-10-01T10:00:00Z\nend: 2026-10-01T15:00:00Z\n"
    "state: draft\nvisibility: signed-in\n"
)


async def test_a_contest_manager_asks_and_the_poller_makes_the_task_with_its_files(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring"), Role.MANAGER)
    contest_before = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    acme.fake.reset_calls()

    record = await tasks.create(setup, bob, spring, "sum", title="Sum of Two")
    assert (record.kind, record.target_id, record.status) == ("task", "acme/spring/sum", "pending")
    await tick(setup, "provisioning")

    done = await tasks.status(setup, bob, SUM)
    assert done is not None
    assert (done.status, done.last_step) == ("ready", "contest_entry")
    assert [call.operation for call in acme.fake.calls if call.identity == PLATFORM] == [
        "exists",
        "create_task",
        "secure",
        "read_file",
        "write_file",
    ]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert sorted(repo.files) == [
        "data/testcases/1.ans",
        "data/testcases/1.in",
        "statement.md",
        "task.yaml",
    ]
    assert parse_task(repo.files["task.yaml"]).name == "Sum of Two"
    assert repo.teams == {Scope("acme", "spring"), Scope("acme", "spring", "sum")}
    assert repo.reserved == {"published/"}
    assert repo.rewrites_refused is True
    assert repo.versions == {}
    assert acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"] == (
        contest_before.replace(
            b"tasks: []\n", b"tasks:\n  - id: sum\n    label: A\n    points: 100\n"
        )
    )
    found = await acme.fake.content.read_file(bob.identity, SUM, "statement.md")
    assert found.content.startswith(b"Write the statement")


async def test_each_new_task_takes_the_next_free_label_and_a_listed_one_is_left_alone(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await write_contest(
        acme.fake,
        CONTEST_HEAD + "tasks:\n  # Ordered by difficulty.\n  - id: product\n    label: A\n"
        "    points: 50\n",
    )

    for name in ("product", "sum", "power"):
        await tasks.create(setup, acme.ada, spring, name)
        await tick(setup, "provisioning")

    written = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    assert [(entry.id, entry.label, entry.points) for entry in parse_contest(written).tasks] == [
        ("product", "A", 50),
        ("sum", "B", 100),
        ("power", "C", 100),
    ]
    assert b"# Ordered by difficulty." in written


async def test_a_contest_yaml_that_does_not_read_is_left_alone_and_the_task_is_made(
    setup: Setup, acme: Acme, spring: ContestId, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await write_contest(acme.fake, "name: [\n")

    await tasks.create(setup, acme.ada, spring, "sum")
    await tick(setup, "provisioning")

    done = await tasks.status(setup, acme.ada, SUM)
    assert done is not None
    assert (done.status, done.last_step) == ("ready", "contest_entry")
    assert acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"] == b"name: [\n"
    (skipped,) = logged(caplog, "tasks.not_listed")
    assert skipped["task"] == "acme/spring/sum"


async def test_a_task_needs_the_manager_role_at_its_contest(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.OBSERVER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring"))

    with pytest.raises(Forbidden, match="manager role at acme/spring"):
        await tasks.create(setup, bob, spring, "sum")
    assert acme.fake.calls_to("exists") == []


async def test_a_task_in_a_contest_that_is_not_there_is_refused(setup: Setup, acme: Acme) -> None:
    with pytest.raises(NotFound, match="no contest acme/winter"):
        await tasks.create(setup, acme.ada, ContestId("acme/winter"), "sum")
    await contests.create(setup, acme.ada, ACME, "spring")
    with pytest.raises(NotFound, match="no contest acme/spring"):
        await tasks.create(setup, acme.ada, SPRING, "sum")
    assert acme.fake.calls_to("create_task") == []


async def test_a_malformed_contest_names_nothing(setup: Setup, acme: Acme) -> None:
    with pytest.raises(NotFound):
        await tasks.create(setup, acme.ada, ContestId("acme"), "sum")
    with pytest.raises(NotFound):
        await tasks.status(setup, acme.ada, TaskId("acme/spring"))


async def test_the_status_is_for_anyone_observing_the_contest(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring", "other"), Role.ADMIN)
    task_only = await organiser(setup, acme.fake, 8, Scope("acme", "spring", "other"))
    await tasks.create(setup, acme.ada, spring, "sum")

    with pytest.raises(Forbidden, match="observer role at acme/spring"):
        await tasks.status(setup, task_only, SUM)
    pending = await tasks.status(setup, acme.ada, SUM)
    assert pending is not None
    assert pending.status == "pending"


async def test_the_tasks_of_a_contest_are_listed_as_the_organiser(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await tasks.create(setup, acme.ada, SPRING, "product")
    await tick(setup, "provisioning")

    listed = await tasks.list(setup, acme.ada, SPRING)

    assert listed == (TaskId("acme/spring/product"), SUM)
    (call,) = acme.fake.calls_to("list_tasks")
    assert call.identity == acme.ada.identity


async def test_a_new_task_is_a_draft_with_nothing_wrong_and_no_publication(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    state = await tasks.state(setup, acme.ada, sum_task)

    assert (state.draft, state.errors, state.latest) == (True, (), None)
    assert state.head == acme.fake.state.repos[("acme", "spring.sum.task")].head


async def test_the_nightly_pass_puts_back_a_tasks_roles_and_protection(
    setup: Setup, acme: Acme, sum_task: TaskId, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    repo.teams.discard(Scope("acme", "spring"))
    repo.reserved.clear()

    await tick(setup, "drift.nightly")

    assert repo.teams == {Scope("acme", "spring"), Scope("acme", "spring", "sum")}
    assert repo.reserved == {"published/"}
    (restored,) = logged(caplog, "drift.content_restored")
    assert (restored["place"], restored["put_back"]) == ("acme/spring/sum", 4)
