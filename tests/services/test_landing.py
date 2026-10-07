"""What a visitor with no session reads: the contests that are for everyone
and published, each with the tasks released now, labelled by their place in
the contest, and a released task's statement. A contest that is not for
everyone and a task that is not visible are
no such contest or task, as for one that is not there, and everything is
read as the platform. The list starts from every contest as this process
read it at most half a minute ago, and a contest's settings saved through
the platform show there at once.
"""

import pytest

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contests, files, landing, published
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

LATER = RUNNING + "  - {{id: later, release_at: 2026-09-26T13:00:00Z}}\n"


@pytest.fixture
async def public(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """acme/spring for everyone and running with sum published and a task
    released only later, and acme/autumn for anyone signed in.
    """
    manager = await organiser(setup, acme.fake, 7, Scope("acme"), Role.MANAGER)
    await contests.create(setup, manager, ACME, "autumn")
    later = await make_task(setup, acme, "later")
    await write_contest(acme.fake, LATER.format(visibility="everyone"))
    await write_contest(acme.fake, RUNNING.format(visibility="signed-in"), ContestId("acme/autumn"))
    await publish(setup, acme, sum_task)
    await publish(setup, acme, later)
    acme.fake.reset_calls()
    return sum_task


async def test_the_contests_for_everyone_are_listed_and_nothing_else(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    listed = await landing.contests(setup)

    assert [(entry.contest, entry.name, entry.tasks) for entry in listed] == [
        (SPRING, "Spring 2026", ())
    ]
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}


async def test_a_contest_for_everyone_shows_its_released_tasks_and_their_statements(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    contest = await landing.contest(setup, SPRING)
    statement = await landing.statement(setup, public)

    assert [(task.task, task.label, task.title) for task in contest.tasks] == [
        (public, "A", "Sum of Two")
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


@pytest.mark.parametrize("task", ["acme/spring/later", "acme/spring/nothing", "acme/autumn/sum"])
async def test_any_other_task_is_no_such_task(
    setup: Setup, acme: Acme, public: TaskId, task: str
) -> None:
    with pytest.raises(NotFound, match="no such task"):
        await landing.statement(setup, TaskId(task))


async def test_the_list_is_kept_half_a_minute_and_read_again_after(
    setup: Setup, acme: Acme, public: TaskId, clock: FakeClock
) -> None:
    first = await landing.contests(setup)
    await write_contest(acme.fake, RUNNING.format(visibility="signed-in"))
    acme.fake.reset_calls()

    kept = await landing.contests(setup)
    assert acme.fake.calls_to("list_contests") == []
    clock.advance(published.EVERY_CONTEST_KEPT)
    after = await landing.contests(setup)

    assert kept == first
    assert after == ()
    assert acme.fake.calls_to("list_contests") != []


async def test_settings_saved_through_the_platform_show_at_once(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    assert await landing.contests(setup) != ()
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")

    await files.write(
        setup,
        acme.ada,
        SPRING,
        "contest.yaml",
        RUNNING.format(visibility="signed-in").encode(),
        current.token,
    )

    assert await landing.contests(setup) == ()
