"""Whether the signed-in person sees a task and may submit to it, read from
the task's entry in `contest.yaml`, the latest publication, the clock and the
extension of the person's row. A task with no publication is not released,
nor one the contest does not list, nor one before its contest's start or its
entry's `release_at`; a released task stays visible after it closes until
the contest is archived; and it is open until the entry's close plus the
row's extension on that task, refused after with `task_closed` and the
reason `closed`. The contest's own visibility comes first: someone it is
hidden from is told there is no such task.
"""

from datetime import timedelta

import pytest
from sqlalchemy import update

from forge.db.tables import Contestant
from forge.domain.errors import NotFound, TaskClosed
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.release import Closed
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import contestants, release, submissions
from forge.services.release import TaskRelease
from forge.testing import FakeClock, register_contestant
from tests.services.conftest import SPRING, Acme, organiser, publish, signed_in, upload

CONTEST = """\
name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: {state}
visibility: {visibility}
tasks:
{tasks}"""

SUM = "  - id: sum\n"

OPEN = TaskRelease(released=True, visible=True, open=True, closed=None)
NOT_RELEASED = TaskRelease(released=False, visible=False, open=False, closed=Closed.NOT_RELEASED)


async def _contest(
    acme: Acme, *, state: str = "published", visibility: str = "everyone", tasks: str = SUM
) -> None:
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    await acme.fake.content.write_file(
        PLATFORM,
        SPRING,
        "contest.yaml",
        CONTEST.format(state=state, visibility=visibility, tasks=tasks).encode(),
        message="Settings",
        expected=current.token,
    )


@pytest.fixture
async def bob(setup: Setup, acme: Acme) -> Session:
    return await signed_in(setup, acme.fake, 8)


@pytest.fixture
async def ada(setup: Setup, acme: Acme) -> Session:
    """The org's admin, an organiser of every contest in it."""
    return await signed_in(setup, acme.fake, 7)


async def test_a_task_with_no_publication_is_not_released(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme)

    assert await release.of_task(setup, bob, sum_task) == NOT_RELEASED


async def test_a_published_task_of_a_running_contest_is_released_visible_and_open(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme)
    await publish(setup, acme, sum_task)
    acme.fake.reset_calls()

    assert await release.of_task(setup, bob, sum_task) == OPEN
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}


@pytest.mark.parametrize(
    ("tasks", "moved"),
    [
        (SUM, timedelta(hours=-3)),
        ("  - {id: sum, release_at: 2026-09-26T13:00:00Z}\n", timedelta(0)),
        ("  []\n", timedelta(0)),
        ("  - id: other\n", timedelta(0)),
    ],
    ids=["before-start", "release-at", "no-tasks", "not-listed"],
)
async def test_a_task_before_its_release_or_not_listed_is_not_released(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    bob: Session,
    clock: FakeClock,
    tasks: str,
    moved: timedelta,
) -> None:
    await _contest(acme)
    await publish(setup, acme, sum_task)
    await _contest(acme, tasks=tasks)
    clock.advance(moved)

    assert await release.of_task(setup, bob, sum_task) == NOT_RELEASED


async def test_a_future_release_at_hides_the_task_until_it_passes(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, clock: FakeClock
) -> None:
    await _contest(acme, tasks="  - {id: sum, release_at: 2026-09-26T13:00:00Z}\n")
    await publish(setup, acme, sum_task)

    clock.advance(timedelta(minutes=59, seconds=59))
    before = await release.of_task(setup, bob, sum_task)
    clock.advance(timedelta(seconds=1))
    at = await release.of_task(setup, bob, sum_task)

    assert (before, at) == (NOT_RELEASED, OPEN)


async def test_the_latest_publication_decides_and_not_the_files_saved_since(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme)
    await publish(setup, acme, sum_task)
    head = await acme.fake.content.list_files(PLATFORM, sum_task)
    await acme.fake.content.save_files(
        PLATFORM,
        sum_task,
        {"task.yaml": b"name: [\n"},
        expected={"task.yaml": head.tokens["task.yaml"]},
        message="Not published",
    )

    assert await release.of_task(setup, bob, sum_task) == OPEN


async def test_after_the_close_the_task_stays_visible_and_is_open_only_within_an_extension(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, clock: FakeClock
) -> None:
    await _contest(acme)
    await publish(setup, acme, sum_task)
    clock.advance(timedelta(hours=3, minutes=30))

    closed = await release.of_task(setup, bob, sum_task)
    await register_contestant(setup, SPRING, 8, time_extension=timedelta(hours=1))
    extended = await release.of_task(setup, bob, sum_task)

    assert closed == TaskRelease(released=True, visible=True, open=False, closed=Closed.CLOSED)
    assert extended == OPEN


async def test_a_task_closes_at_its_own_close_before_the_contest_ends(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, clock: FakeClock
) -> None:
    await _contest(acme, tasks="  - {id: sum, closes: 2026-09-26T13:00:00Z}\n")
    await publish(setup, acme, sum_task)

    clock.advance(timedelta(minutes=59, seconds=59))
    before = await release.of_task(setup, bob, sum_task)
    clock.advance(timedelta(seconds=1))
    at = await release.of_task(setup, bob, sum_task)

    assert before == OPEN
    assert at == TaskRelease(released=True, visible=True, open=False, closed=Closed.CLOSED)


async def test_an_extension_on_a_task_opens_that_task_alone_past_its_close(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, clock: FakeClock
) -> None:
    await _contest(
        acme,
        tasks=(
            "  - {id: sum, closes: 2026-09-26T13:00:00Z}\n"
            "  - {id: other, closes: 2026-09-26T13:00:00Z}\n"
        ),
    )
    await publish(setup, acme, sum_task)
    acme.fake.add_user(20, "cyd")
    for user_id in (8, 20):
        await contestants.register(setup, await signed_in(setup, acme.fake, user_id), SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    for user_id in (8, 20):
        await contestants.approve(setup, manager, SPRING, user_id)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(hours=1), tasks=["sum"])
    await contestants.extend(setup, manager, SPRING, 20, timedelta(hours=1), tasks=["other"])
    cyd = await signed_in(setup, acme.fake, 20)
    clock.advance(timedelta(hours=1, minutes=30))

    made = await upload(setup, acme.fake, bob, sum_task, b"print(1)\n")
    submitted = await submissions.submit(
        setup,
        bob,
        sum_task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )
    with pytest.raises(TaskClosed) as refused:
        await upload(setup, acme.fake, cyd, sum_task, b"print(2)\n")

    assert submitted.number == 1
    assert (refused.value.code, refused.value.extra) == ("task_closed", {"reason": "closed"})
    assert await release.of_task(setup, cyd, sum_task) == TaskRelease(
        released=True, visible=True, open=False, closed=Closed.CLOSED
    )


async def test_an_archived_contest_closes_the_task_and_shows_it_to_its_organisers_alone(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, ada: Session
) -> None:
    await _contest(acme)
    await publish(setup, acme, sum_task)

    await _contest(acme, state="archived")
    archived = await release.of_task(setup, ada, sum_task)

    assert archived == TaskRelease(released=True, visible=False, open=False, closed=Closed.ARCHIVED)
    with pytest.raises(NotFound):
        await release.of_task(setup, bob, sum_task)


async def test_a_draft_contest_is_no_such_task_to_anyone_but_its_organisers(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, ada: Session
) -> None:
    await _contest(acme, state="draft")
    await publish(setup, acme, sum_task)
    await register_contestant(setup, SPRING, 8)

    with pytest.raises(NotFound):
        await release.of_task(setup, bob, sum_task)
    assert await release.of_task(setup, ada, sum_task) == NOT_RELEASED


async def test_a_hidden_contest_shows_its_tasks_to_its_contestants_alone(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme, visibility="hidden")
    await publish(setup, acme, sum_task)

    with pytest.raises(NotFound):
        await release.of_task(setup, bob, sum_task)
    await register_contestant(setup, SPRING, 8, status="pending")
    with pytest.raises(NotFound):
        await release.of_task(setup, bob, sum_task)

    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Contestant).where(Contestant.user_id == 8).values(status="approved")
        )
    assert await release.of_task(setup, bob, sum_task) == OPEN
