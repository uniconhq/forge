"""An approved contestant's workspace at the forge. Approval in a contest with
two published tasks gives the contestant a desk and a place to submit each,
all of which they write and none of which is made twice; a place that fails
is made again alone. A task published after the approval gets a place for
every approved contestant, and for nobody else. A place asked for before the
desk is open waits for it. A contestant removed before their workspace is
made gets none, and one removed after it keeps it without access, even when
the workspace's name never reached their row.
"""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Contestant, Provisioning
from forge.domain.errors import Unavailable
from forge.domain.ids import TaskId
from forge.domain.registration import WorkspaceState
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contestants
from forge.services.access import Organiser
from forge.testing import FakeClock, tick
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    publish,
    signed_in,
    write_contest,
)


@pytest.fixture
async def manager(setup: Setup, acme: Acme, spring: str) -> Organiser:
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    acme.fake.add_user(20, "cyd")
    return await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)


@pytest.fixture
async def two_tasks(setup: Setup, acme: Acme, sum_task: TaskId, manager: Organiser) -> list[TaskId]:
    """sum and diff, both published."""
    diff = await make_task(setup, acme, "diff")
    await publish(setup, acme, sum_task)
    await publish(setup, acme, diff)
    acme.fake.reset_calls()
    return [sum_task, diff]


async def _approve(setup: Setup, acme: Acme, manager: Organiser, user_id: int) -> None:
    await contestants.register(setup, await signed_in(setup, acme.fake, user_id), SPRING)
    await contestants.approve(setup, manager, SPRING, user_id)


async def _workspace(setup: Setup, manager: Organiser, user_id: int) -> WorkspaceState | None:
    (found,) = [
        entry
        for entry in await contestants.list(setup, manager, SPRING)
        if entry.user_id == user_id
    ]
    return found.workspace


async def _ticks(setup: Setup, count: int = 3) -> None:
    for _ in range(count):
        await tick(setup, "provisioning")


def _writers(acme: Acme, repo: str) -> set[int]:
    return acme.fake.state.repos[("acme", repo)].writers


async def test_approval_opens_a_desk_and_a_place_for_each_published_task(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    await _approve(setup, acme, manager, 8)
    assert await _workspace(setup, manager, 8) is WorkspaceState.PREPARING

    await _ticks(setup)

    for repo in ("spring.bob.desk", "spring.sum.bob.sub", "spring.diff.bob.sub"):
        assert 8 in _writers(acme, repo)
    assert "submission/" in acme.fake.state.repos[("acme", "spring.sum.bob.sub")].reserved
    assert len(acme.fake.calls_to("open_workspace")) == 1
    assert len(acme.fake.calls_to("open_submission_place")) == 2
    assert await _workspace(setup, manager, 8) is WorkspaceState.READY
    async with setup.unit_of_work() as ctx:
        row = (await ctx.db.execute(select(Contestant))).scalar_one()
    assert row.workspace_id == "acme/spring/@bob"


async def test_a_place_that_fails_is_made_again_alone(
    setup: Setup,
    acme: Acme,
    manager: Organiser,
    two_tasks: list[TaskId],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff = two_tasks[1]
    original = acme.fake.workspaces.open_submission_place

    async def failing_for_diff(workspace: Any, task: TaskId, members: Any) -> None:
        if task == diff:
            raise Unavailable("the forge went away")
        await original(workspace, task, members)

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", failing_for_diff)
    await _approve(setup, acme, manager, 8)
    await _ticks(setup)

    (listed,) = await contestants.list(setup, manager, SPRING)
    assert (listed.workspace, listed.workspace_error) == (
        WorkspaceState.PREPARING,
        "the forge or the CI did not answer",
    )
    assert ("acme", "spring.diff.bob.sub") not in acme.fake.state.repos

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", original)
    acme.fake.reset_calls()
    clock.advance(timedelta(minutes=1))
    await _ticks(setup)

    assert [call.arguments["task"] for call in acme.fake.calls_to("open_submission_place")] == [
        diff
    ]
    assert acme.fake.calls_to("open_workspace") == []
    assert await _workspace(setup, manager, 8) is WorkspaceState.READY


async def test_a_task_published_later_gets_a_place_for_every_approved_contestant(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    await _approve(setup, acme, manager, 8)
    await contestants.register(setup, await signed_in(setup, acme.fake, 20), SPRING)
    await _ticks(setup)
    late = await make_task(setup, acme, "late")

    await publish(setup, acme, late)
    assert await _workspace(setup, manager, 8) is WorkspaceState.PREPARING
    await _ticks(setup)

    assert 8 in _writers(acme, "spring.late.bob.sub")
    assert ("acme", "spring.late.cyd.sub") not in acme.fake.state.repos
    assert await _workspace(setup, manager, 8) is WorkspaceState.READY
    await publish(setup, acme, late, b"# again\n")
    async with setup.unit_of_work() as ctx:
        places = (
            await ctx.db.execute(
                select(Provisioning).where(Provisioning.kind == "submission_place")
            )
        ).scalars()
        assert sorted(place.target_id.split("/", 1)[1] for place in places) == [
            "acme/spring/diff",
            "acme/spring/late",
            "acme/spring/sum",
        ]


async def test_a_contestant_removed_before_their_workspace_is_made_gets_none(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    await _approve(setup, acme, manager, 8)
    await contestants.remove(setup, manager, SPRING, 8)

    await _ticks(setup)

    assert ("acme", "spring.bob.desk") not in acme.fake.state.repos
    assert acme.fake.calls_to("open_workspace") == []
    assert [name for (_, name) in acme.fake.state.repos if ".bob." in name] == []


async def test_a_removed_contestant_keeps_their_workspace_without_access(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    await _approve(setup, acme, manager, 8)
    await _ticks(setup)

    await contestants.remove(setup, manager, SPRING, 8)
    late = await make_task(setup, acme, "late")
    await publish(setup, acme, late)
    await _ticks(setup)

    for repo in ("spring.bob.desk", "spring.sum.bob.sub", "spring.diff.bob.sub"):
        assert 8 not in _writers(acme, repo)
    assert ("acme", "spring.late.bob.sub") not in acme.fake.state.repos


async def test_a_place_asked_for_before_the_desk_waits_for_it(
    setup: Setup,
    acme: Acme,
    manager: Organiser,
    two_tasks: list[TaskId],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = acme.fake.workspaces.open_workspace

    async def down(*args: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(acme.fake.workspaces, "open_workspace", down)
    await _approve(setup, acme, manager, 8)
    await _ticks(setup)
    late = await make_task(setup, acme, "late")
    await publish(setup, acme, late)
    await _ticks(setup)

    assert acme.fake.calls_to("open_submission_place") == []
    (listed,) = await contestants.list(setup, manager, SPRING)
    assert listed.workspace is WorkspaceState.PREPARING

    monkeypatch.setattr(acme.fake.workspaces, "open_workspace", original)
    for _ in range(3):
        clock.advance(timedelta(minutes=5))
        await _ticks(setup)

    for repo in ("spring.bob.desk", "spring.sum.bob.sub", "spring.late.bob.sub"):
        assert 8 in _writers(acme, repo)
    assert await _workspace(setup, manager, 8) is WorkspaceState.READY


async def test_removal_closes_a_desk_whose_name_never_reached_the_row(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    await _approve(setup, acme, manager, 8)
    await _ticks(setup)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Contestant).values(workspace_id=None))

    await contestants.remove(setup, manager, SPRING, 8)

    for repo in ("spring.bob.desk", "spring.sum.bob.sub", "spring.diff.bob.sub"):
        assert 8 not in _writers(acme, repo)
