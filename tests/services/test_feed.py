"""A contest's gradings read together, over a real Postgres and the fake.
The feed lists every grading of the contest's tasks the organiser observes,
newest first, each attempt a row of its own with who submitted it, a
contestant by username or a team by name, and narrows by task, by who
submitted and by status, an overdue or lost grading reading as a system
error there as everywhere. The queue depth counts the waiting ones by
status in one read. An organiser of one task sees that task's alone, and
someone with no role in the contest is refused.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading
from forge.domain.errors import Forbidden
from forge.domain.grading import (
    LOST_CHECK_AFTER,
    MACHINE_WAIT,
    NEVER_BEGAN,
    START_WAIT,
    GradingStatus,
)
from forge.domain.identity import User
from forge.domain.ids import TaskId, new_id
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import contestants, gradings, submissions, teams
from forge.services.access import Organiser
from forge.testing import FakeClock
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    make_task,
    organiser,
    publish,
    signed_in,
    upload,
    write_contest,
)

TWO_TASKS = RUNNING + "  - id: max\n"
CONTEST = Scope("acme", "spring")


async def _submit(setup: Setup, acme: Acme, person: Session, task: TaskId, key: str) -> uuid.UUID:
    made = await upload(setup, acme.fake, person, task, f"print({key!r})\n".encode())
    submitted = await submissions.submit(
        setup,
        person,
        task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    assert submitted.grading is not None
    return submitted.grading.id


async def _set(setup: Setup, grading: uuid.UUID, **values: Any) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).where(Grading.id == grading).values(**values))


async def _enter(setup: Setup, acme: Acme, user_id: int, name: str) -> Session:
    """Someone else registered and approved in acme/spring."""
    acme.fake.add_user(user_id, name)
    person = await signed_in(setup, acme.fake, user_id)
    await contestants.register(setup, person, SPRING)
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, user_id)
    return person


def _held(acme: Acme, scope: Scope) -> Organiser:
    """Someone checked as observing `scope` and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, Role.OBSERVER),),
        scope=scope,
        role=Role.OBSERVER,
        identity=acme.ada.identity,
    )


@pytest.fixture
async def busy(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> dict[str, uuid.UUID]:
    """acme/spring with sum and max both running: bob's submission to sum,
    retried once after a system error, his to max, and carol's to sum, in
    that order.
    """
    other = await make_task(setup, acme, "max")
    await publish(setup, acme, other)
    await write_contest(acme.fake, TWO_TASKS.format(visibility="everyone"))
    carol = await _enter(setup, acme, 20, "carol")
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)

    first = await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    await _set(setup, first, status=GradingStatus.SYSTEM_ERROR, error="The checker crashed.")
    clock.advance(timedelta(seconds=1))
    retried = (await gradings.retry(setup, manager, first)).id
    clock.advance(timedelta(seconds=1))
    on_max = await _submit(setup, acme, entered.session, other, "key-0002-bbbb")
    clock.advance(timedelta(seconds=1))
    by_carol = await _submit(setup, acme, carol, entered.task, "key-0003-cccc")
    return {"first": first, "retried": retried, "on_max": on_max, "by_carol": by_carol}


async def test_the_feed_lists_every_attempt_newest_first_with_who_submitted_it(
    setup: Setup, acme: Acme, entered: Entered, busy: dict[str, uuid.UUID]
) -> None:
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)

    listed = await gradings.feed(setup, observer, SPRING)

    assert [entry.grading.id for entry in listed] == [
        busy["by_carol"],
        busy["on_max"],
        busy["retried"],
        busy["first"],
    ]
    assert [(entry.by.user_id, entry.by.team, entry.by.name) for entry in listed] == [
        (20, None, "carol"),
        (8, None, "bob"),
        (8, None, "bob"),
        (8, None, "bob"),
    ]
    first, retried = listed[3].grading, listed[2].grading
    # The two attempts of one submission group by its workspace and number.
    assert (first.workspace, first.submission_number, first.attempt) == (
        retried.workspace,
        retried.submission_number,
        1,
    )
    assert retried.attempt == 2
    assert [entry.grading.latest for entry in listed] == [True, True, True, False]
    assert [(entry.task_name, entry.label) for entry in listed] == [
        ("sum", "A"),
        ("max", "B"),
        ("sum", "A"),
        ("sum", "A"),
    ]
    assert (first.status, first.error) == (GradingStatus.SYSTEM_ERROR, "The checker crashed.")
    assert [
        entry.grading.id for entry in await gradings.feed(setup, observer, SPRING, limit=2)
    ] == [
        busy["by_carol"],
        busy["on_max"],
    ]


async def test_the_feed_narrows_by_task_by_who_submitted_and_by_status(
    setup: Setup, acme: Acme, entered: Entered, busy: dict[str, uuid.UUID], clock: FakeClock
) -> None:
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)

    async def ids(**filters: Any) -> list[uuid.UUID]:
        return [
            entry.grading.id for entry in await gradings.feed(setup, observer, SPRING, **filters)
        ]

    assert await ids(task=TaskId("acme/spring/max")) == [busy["on_max"]]
    assert await ids(task=TaskId("acme/autumn/sum")) == []
    assert await ids(user="carol") == [busy["by_carol"]]
    assert await ids(user="bob", task=entered.task) == [busy["retried"], busy["first"]]
    assert await ids(user="nobody") == []
    assert await ids(status=GradingStatus.SYSTEM_ERROR) == [busy["first"]]
    # Alone on its page, the first attempt is still not the latest.
    (stuck,) = await gradings.feed(setup, observer, SPRING, status=GradingStatus.SYSTEM_ERROR)
    assert stuck.grading.latest is False
    assert await ids(status=GradingStatus.DISPATCHED) == [
        busy["by_carol"],
        busy["on_max"],
        busy["retried"],
    ]
    assert await ids(status=GradingStatus.DONE) == []

    # Overdue, they read as system errors and no longer as dispatched.
    clock.advance(MACHINE_WAIT)
    late = await gradings.feed(setup, observer, SPRING, status=GradingStatus.SYSTEM_ERROR, limit=2)
    assert [(entry.grading.id, entry.grading.error) for entry in late] == [
        (busy["by_carol"], NEVER_BEGAN),
        (busy["on_max"], NEVER_BEGAN),
    ]
    assert await ids(status=GradingStatus.DISPATCHED) == []


async def test_an_organiser_of_one_task_sees_its_gradings_alone(
    setup: Setup, acme: Acme, busy: dict[str, uuid.UUID]
) -> None:
    of_max = _held(acme, Scope("acme", "spring", "max"))

    listed = await gradings.feed(setup, of_max, SPRING)

    assert [entry.grading.id for entry in listed] == [busy["on_max"]]
    assert await gradings.feed(setup, of_max, SPRING, task=TaskId("acme/spring/sum")) == ()
    assert await gradings.queue_depth(setup, of_max, SPRING) == gradings.QueueDepth(0, 1)
    elsewhere = _held(acme, Scope("acme", "autumn"))
    with pytest.raises(Forbidden):
        await gradings.feed(setup, elsewhere, SPRING)
    with pytest.raises(Forbidden):
        await gradings.queue_depth(setup, elsewhere, SPRING)


async def test_the_queue_depth_counts_the_waiting_gradings_by_status(
    setup: Setup, acme: Acme, busy: dict[str, uuid.UUID], clock: FakeClock
) -> None:
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)
    await _set(setup, busy["by_carol"], status=GradingStatus.QUEUED, run_id=None)

    assert await gradings.queue_depth(setup, observer, SPRING) == gradings.QueueDepth(1, 2)
    clock.advance(START_WAIT)
    # The one never started is overdue, and reads as a system error.
    assert await gradings.queue_depth(setup, observer, SPRING) == gradings.QueueDepth(0, 2)


async def test_a_teams_submission_is_shown_as_the_team(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="everyone") + "team_size: 2\n")
    team = await teams.create(setup, entered.session, SPRING, "Adders")
    made = await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)

    (entry,) = await gradings.feed(setup, observer, SPRING, team=team.id)

    assert (entry.grading.id, entry.by) == (made, gradings.Submitter(None, team.id, "Adders"))
    assert await gradings.feed(setup, observer, SPRING, user="bob") == ()


async def test_a_task_the_contest_no_longer_lists_keeps_its_name_and_has_no_label(
    setup: Setup, acme: Acme, entered: Entered, busy: dict[str, uuid.UUID]
) -> None:
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))

    listed = await gradings.feed(setup, observer, SPRING, task=TaskId("acme/spring/max"))

    assert [(entry.grading.id, entry.task_name, entry.label) for entry in listed] == [
        (busy["on_max"], "max", None)
    ]


async def test_a_tasks_gradings_say_who_submitted_each_as_the_feed_does(
    setup: Setup, acme: Acme, entered: Entered, busy: dict[str, uuid.UUID]
) -> None:
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)

    listed = await gradings.list(setup, observer, entered.task)

    assert [
        (entry.grading.id, entry.by.name, entry.task_name, entry.label) for entry in listed
    ] == [
        (busy["by_carol"], "carol", "sum", "A"),
        (busy["retried"], "bob", "sum", "A"),
        (busy["first"], "bob", "sum", "A"),
    ]
    assert listed[0].by == gradings.Submitter(20, None, "carol")


async def test_a_filter_by_status_finds_each_grading_as_the_unfiltered_feed_reads_it(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    made = await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    now = clock.now()
    async with setup.unit_of_work() as ctx:
        base = (await ctx.db.execute(select(Grading).where(Grading.id == made))).scalar_one()
        run = base.run_id
        # One row in each state the deadlines and the CI tell apart.
        states: dict[str, dict[str, Any]] = {
            "queued": {"status": GradingStatus.QUEUED},
            "queued too long": {
                "status": GradingStatus.QUEUED,
                "queued_at": now - START_WAIT,
            },
            "dispatched": {
                "status": GradingStatus.DISPATCHED,
                "run_id": run,
                "dispatched_at": now - timedelta(minutes=1),
            },
            "held by the CI": {
                "status": GradingStatus.DISPATCHED,
                "run_id": run,
                "dispatched_at": now - LOST_CHECK_AFTER,
            },
            "lost by the CI": {
                "status": GradingStatus.DISPATCHED,
                "run_id": "9/999",
                "dispatched_at": now - LOST_CHECK_AFTER,
            },
            "dispatched with no run": {
                "status": GradingStatus.DISPATCHED,
                "dispatched_at": now - LOST_CHECK_AFTER,
            },
            "never began": {
                "status": GradingStatus.DISPATCHED,
                "run_id": run,
                "dispatched_at": now - MACHINE_WAIT,
            },
            "running": {
                "status": GradingStatus.RUNNING,
                "run_id": run,
                "deadline_at": now + timedelta(minutes=1),
            },
            "running past its deadline": {
                "status": GradingStatus.RUNNING,
                "run_id": run,
                "deadline_at": now,
            },
            "running with no deadline": {"status": GradingStatus.RUNNING, "run_id": run},
            "done": {"status": GradingStatus.DONE},
            "cancelled": {"status": GradingStatus.CANCELLED},
            "system error": {"status": GradingStatus.SYSTEM_ERROR},
        }
        rows = {
            name: Grading(
                id=new_id(),
                task_id=base.task_id,
                workspace_id=base.workspace_id,
                submission_id=f"{base.submission_id}-{index}",
                submission_number=base.submission_number,
                submission_version=base.submission_version,
                submitted_at=base.submitted_at,
                publication_id=base.publication_id,
                attempt=1,
                **({"queued_at": now} | values),
            )
            for index, (name, values) in enumerate(states.items())
        }
        ctx.db.add_all(rows.values())
    observer = await organiser(setup, acme.fake, 7, CONTEST, Role.OBSERVER)

    every = await gradings.feed(setup, observer, SPRING)

    read = {entry.grading.id: entry.grading.status for entry in every}
    assert {name: read[row.id] for name, row in rows.items()} == {
        "queued": GradingStatus.QUEUED,
        "queued too long": GradingStatus.SYSTEM_ERROR,
        "dispatched": GradingStatus.DISPATCHED,
        "held by the CI": GradingStatus.DISPATCHED,
        "lost by the CI": GradingStatus.SYSTEM_ERROR,
        "dispatched with no run": GradingStatus.DISPATCHED,
        "never began": GradingStatus.SYSTEM_ERROR,
        "running": GradingStatus.RUNNING,
        "running past its deadline": GradingStatus.SYSTEM_ERROR,
        "running with no deadline": GradingStatus.RUNNING,
        "done": GradingStatus.DONE,
        "cancelled": GradingStatus.CANCELLED,
        "system error": GradingStatus.SYSTEM_ERROR,
    }
    for status in GradingStatus:
        filtered = await gradings.feed(setup, observer, SPRING, status=status)
        assert [entry.grading.id for entry in filtered] == [
            entry.grading.id for entry in every if entry.grading.status == status
        ], status
    # A page of system errors smaller than what waits fills from the query alone.
    (first,) = await gradings.feed(
        setup, observer, SPRING, status=GradingStatus.SYSTEM_ERROR, limit=1
    )
    assert first.grading.status == GradingStatus.SYSTEM_ERROR
