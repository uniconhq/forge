"""What a visitor with no session reads: the contests that are public and
published, each with the tasks released now, and a released task's
statement. A contest that is not public and a task that is not visible are
no such contest or task, as for one that is not there, and everything is
read as the platform. The list is kept five seconds.
"""

from datetime import timedelta

import pytest

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contests, landing
from forge.testing import FakeClock
from tests.services.conftest import (
    ACME,
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    publish,
    write_contest,
)


@pytest.fixture
async def public(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """acme/spring public and running with sum published and a hidden task,
    and acme/autumn for anyone signed in.
    """
    manager = await organiser(setup, acme.fake, 7, Scope("acme"), Role.MANAGER)
    await contests.create(setup, manager, ACME, "autumn")
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    await write_contest(acme.fake, RUNNING.format(visibility="signed-in"), ContestId("acme/autumn"))
    hidden = await make_task(setup, acme, "hidden")
    await publish(setup, acme, sum_task)
    await publish(setup, acme, hidden, b"hidden: true\n")
    acme.fake.reset_calls()
    return sum_task


async def test_the_public_contests_are_listed_and_nothing_else(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    listed = await landing.contests(setup)

    assert [(entry.contest, entry.name, entry.tasks) for entry in listed] == [
        (SPRING, "Spring 2026", ())
    ]
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}


async def test_a_public_contest_shows_its_released_tasks_and_their_statements(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    contest = await landing.contest(setup, SPRING)
    statement = await landing.statement(setup, public)

    assert [(task.task, task.label, task.title) for task in contest.tasks] == [
        (public, "sum", "Sum of Two")
    ]
    assert statement.task.task == public
    stored = await acme.fake.content.read_file(PLATFORM, public, "statement.md")
    assert statement.statement == stored.content.decode()


@pytest.mark.parametrize("contest", ["acme/autumn", "acme/nowhere", "nowhere", "acme/spring/sum"])
async def test_any_other_contest_is_no_such_contest(
    setup: Setup, acme: Acme, public: TaskId, contest: str
) -> None:
    with pytest.raises(NotFound, match="no such contest"):
        await landing.contest(setup, ContestId(contest))


@pytest.mark.parametrize("task", ["acme/spring/hidden", "acme/spring/nothing", "acme/autumn/sum"])
async def test_any_other_task_is_no_such_task(
    setup: Setup, acme: Acme, public: TaskId, task: str
) -> None:
    with pytest.raises(NotFound, match="no such task"):
        await landing.statement(setup, TaskId(task))


async def test_the_list_is_kept_five_seconds_and_read_again_after(
    setup: Setup, acme: Acme, public: TaskId, clock: FakeClock
) -> None:
    first = await landing.contests(setup)
    await write_contest(acme.fake, RUNNING.format(visibility="signed-in"))
    acme.fake.reset_calls()

    kept = await landing.contests(setup)
    clock.advance(timedelta(seconds=5))
    after = await landing.contests(setup)

    assert kept == first
    assert after == ()
    assert acme.fake.calls_to("list_contests") != []
