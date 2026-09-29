"""What a signed-in person reads of a contest. The home of a contest they see
carries its dates, their registration, their own deadline and the server's
clock, and lists only the tasks released to them, in the contest's order. A
task's page gives the statement and limits its latest publication froze, and
nothing for a task that is not visible. A hidden contest is seen by its
approved contestants and organisers alone, and anyone else is told there is
no such contest, as for one that is not there. The list holds every contest
the person sees, with their own status. Everything is read as the platform.
"""

from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.registration import Status
from forge.domain.release import Closed
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contest_home, contestants, contests
from forge.testing import FakeClock, register_contestant, tick
from tests.services.conftest import (
    ACME,
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    publish,
    signed_in,
    write_contest,
)

ORDERED = (
    RUNNING
    + """\
tasks:
  - id: diff
    label: A
    points: 100
  - id: sum
    label: B
    points: 50
"""
)


@pytest.fixture
async def published(setup: Setup, acme: Acme, sum_task: TaskId) -> list[TaskId]:
    """acme/spring running and public, with sum and diff published, a hidden
    task, one released only later, and one never published.
    """
    await write_contest(acme.fake, ORDERED.format(visibility="public"))
    diff = await make_task(setup, acme, "diff")
    hidden = await make_task(setup, acme, "hidden")
    later = await make_task(setup, acme, "later")
    await make_task(setup, acme, "unsaved")
    await publish(setup, acme, sum_task)
    await publish(setup, acme, diff)
    await publish(setup, acme, hidden, b"hidden: true\n")
    await publish(setup, acme, later, b"release_at: 2026-09-26T13:00:00Z\n")
    acme.fake.reset_calls()
    return [diff, sum_task]


async def test_the_home_lists_only_the_released_tasks_in_the_contests_order(
    setup: Setup, acme: Acme, published: list[TaskId]
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    home = await contest_home.home(setup, bob, SPRING)

    assert [(task.name, task.label, task.points) for task in home.tasks] == [
        ("diff", "A", 100),
        ("sum", "B", 50),
    ]
    assert all(task.release.open for task in home.tasks)
    assert home.name == "Spring 2026"
    assert (home.start, home.end) == (
        datetime(2026, 9, 26, 10, tzinfo=UTC),
        datetime(2026, 9, 26, 15, tzinfo=UTC),
    )
    assert (home.registration, home.registration_open, home.deadline) == (None, True, home.end)
    assert home.now == datetime(2026, 9, 26, 12, tzinfo=UTC)
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}


async def test_a_task_released_later_joins_the_home_when_its_time_comes(
    setup: Setup, acme: Acme, published: list[TaskId], clock: FakeClock
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    clock.advance(timedelta(hours=1))

    home = await contest_home.home(setup, bob, SPRING)

    assert [task.name for task in home.tasks] == ["diff", "sum", "later"]


async def test_a_contestants_deadline_and_openness_carry_their_extension(
    setup: Setup, acme: Acme, published: list[TaskId], clock: FakeClock
) -> None:
    await register_contestant(setup, SPRING, 8, time_extension=timedelta(minutes=30))
    bob = await signed_in(setup, acme.fake, 8)
    clock.set(datetime(2026, 9, 26, 15, 10, tzinfo=UTC))

    home = await contest_home.home(setup, bob, SPRING)
    page = await contest_home.task(setup, bob, TaskId("acme/spring/sum"))

    assert home.deadline == datetime(2026, 9, 26, 15, 30, tzinfo=UTC)
    assert home.registration is not None and home.registration.status is Status.APPROVED
    assert page.release.open
    await register_contestant(setup, SPRING, 20, time_extension=timedelta(0))
    acme.fake.add_user(20, "cyd")
    cyd = await signed_in(setup, acme.fake, 20)
    theirs = await contest_home.task(setup, cyd, TaskId("acme/spring/sum"))
    assert (theirs.release.open, theirs.release.closed) == (False, Closed.ENDED)


async def test_a_task_page_gives_the_published_statement_and_limits(
    setup: Setup, acme: Acme, published: list[TaskId]
) -> None:
    sum_task = TaskId("acme/spring/sum")
    statement = await acme.fake.content.read_file(PLATFORM, sum_task, "statement.md")
    await acme.fake.content.write_file(
        acme.ada.identity,
        sum_task,
        "statement.md",
        b"A draft nobody published.\n",
        message="Draft",
        expected=statement.token,
    )
    bob = await signed_in(setup, acme.fake, 8)

    page = await contest_home.task(setup, bob, sum_task)

    assert (page.name, page.label, page.points) == ("sum", "B", 50)
    assert page.statement == statement.content.decode()
    assert (page.limits.submissions, page.limits.max_size) == (50, 10 * 1024 * 1024)


@pytest.mark.parametrize("name", ["hidden", "later", "unsaved", "nothing"])
async def test_a_task_that_is_not_visible_has_no_page(
    setup: Setup, acme: Acme, published: list[TaskId], name: str
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(NotFound, match="no such task"):
        await contest_home.task(setup, bob, TaskId(f"acme/spring/{name}"))


async def test_a_hidden_contest_is_seen_by_its_contestants_and_organisers_alone(
    setup: Setup, acme: Acme, published: list[TaskId]
) -> None:
    await write_contest(acme.fake, ORDERED.format(visibility="hidden"))
    acme.fake.add_user(20, "cyd")
    await register_contestant(setup, SPRING, 8)
    await register_contestant(setup, SPRING, 20, status="pending")
    bob, cyd = await signed_in(setup, acme.fake, 8), await signed_in(setup, acme.fake, 20)
    ada = await signed_in(setup, acme.fake, 7)

    theirs, organisers = (
        await contest_home.home(setup, bob, SPRING),
        await contest_home.home(setup, ada, SPRING),
    )
    assert (len(theirs.tasks), theirs.organises) == (2, False)
    assert (len(organisers.tasks), organisers.organises) == (2, True)
    with pytest.raises(NotFound, match="no such contest"):
        await contest_home.home(setup, cyd, SPRING)
    with pytest.raises(NotFound, match="no such task"):
        await contest_home.task(setup, cyd, TaskId("acme/spring/sum"))
    with pytest.raises(NotFound, match="no such contest"):
        await contest_home.home(setup, cyd, ContestId("acme/nowhere"))


async def test_the_list_holds_every_contest_the_person_sees_with_their_status(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    manager = await organiser(setup, acme.fake, 7, Scope("acme"), Role.MANAGER)
    for name in ("autumn", "winter", "draft"):
        await contests.create(setup, manager, ACME, name)
    await tick(setup, "provisioning")
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    await write_contest(acme.fake, RUNNING.format(visibility="signed-in"), ContestId("acme/autumn"))
    await write_contest(acme.fake, RUNNING.format(visibility="hidden"), ContestId("acme/winter"))
    bob = await signed_in(setup, acme.fake, 8)
    await contestants.register(setup, bob, SPRING)

    listed = await contest_home.contests(setup, bob)

    assert sorted((entry.contest, entry.status) for entry in listed) == [
        ("acme/autumn", None),
        ("acme/spring", Status.PENDING),
    ]
    await register_contestant(setup, ContestId("acme/winter"), 8)
    again = await contest_home.contests(setup, bob)
    assert "acme/winter" in {entry.contest for entry in again}
