"""Whether the signed-in person sees a task and may submit to it, read from
`contest.yaml`, the latest publication's `task.yaml`, the clock and the
person's own extension. A task with no publication is not released; one
that misses any of the four parts is not; a released task stays visible
after the end until the contest is archived; and it is open until the end
plus the person's extension while submissions are not closed. The contest's
own visibility comes first: someone it is hidden from is told there is no
such task.
"""

from datetime import timedelta

import pytest
from sqlalchemy import update

from forge.db.tables import Contestant
from forge.domain.content import Edit
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.release import Closed
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import publications, release, sessions
from forge.services.publications import Published
from forge.services.release import TaskRelease
from forge.testing import FakeClock, register_contestant
from tests.services.conftest import SPRING, Acme

CONTEST = """\
name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: {state}
visibility: {visibility}
submissions_closed: {closed}
"""

OPEN = TaskRelease(released=True, visible=True, open=True, closed=None)


async def _contest(
    acme: Acme, *, state: str = "published", closed: str = "false", visibility: str = "public"
) -> None:
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    await acme.fake.content.write_file(
        PLATFORM,
        SPRING,
        "contest.yaml",
        CONTEST.format(state=state, closed=closed, visibility=visibility).encode(),
        message="Settings",
        expected=current.token,
    )


async def _publish(setup: Setup, acme: Acme, task: TaskId, extra: bytes = b"") -> None:
    head = await acme.fake.content.list_files(PLATFORM, task)
    task_yaml = acme.fake.state.repos[("acme", "spring.sum.task")].files["task.yaml"]
    result = await publications.save(
        setup, acme.ada, task, {"task.yaml": Edit(task_yaml + extra, head.tokens["task.yaml"])}
    )
    assert isinstance(result, Published)


async def _session(setup: Setup, acme: Acme, user_id: int) -> Session:
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx,
            user=acme.fake.users[user_id],
            credential=acme.fake.mint(user_id),
            ip=None,
            user_agent=None,
        )


@pytest.fixture
async def bob(setup: Setup, acme: Acme) -> Session:
    return await _session(setup, acme, 8)


@pytest.fixture
async def ada(setup: Setup, acme: Acme) -> Session:
    """The org's admin, an organiser of every contest in it."""
    return await _session(setup, acme, 7)


async def test_a_task_with_no_publication_is_not_released(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme)

    assert await release.of_task(setup, bob, sum_task) == TaskRelease(
        released=False, visible=False, open=False, closed=Closed.NOT_RELEASED
    )


async def test_a_published_task_of_a_running_contest_is_released_visible_and_open(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme)
    await _publish(setup, acme, sum_task)
    acme.fake.reset_calls()

    assert await release.of_task(setup, bob, sum_task) == OPEN
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}


@pytest.mark.parametrize(
    ("state", "extra", "moved"),
    [
        ("published", b"", timedelta(hours=-3)),
        ("published", b"release_at: 2026-09-26T13:00:00Z\n", timedelta(0)),
        ("published", b"hidden: true\n", timedelta(0)),
    ],
    ids=["before-start", "release-at", "hidden"],
)
async def test_a_task_missing_any_part_is_not_released(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    bob: Session,
    clock: FakeClock,
    state: str,
    extra: bytes,
    moved: timedelta,
) -> None:
    await _contest(acme, state=state)
    await _publish(setup, acme, sum_task, extra)
    clock.advance(moved)

    found = await release.of_task(setup, bob, sum_task)

    assert (found.released, found.visible, found.open, found.closed) == (
        False,
        False,
        False,
        Closed.NOT_RELEASED,
    )


async def test_the_latest_publication_decides_and_not_the_files_saved_since(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme)
    await _publish(setup, acme, sum_task)
    head = await acme.fake.content.list_files(PLATFORM, sum_task)
    await acme.fake.content.save_files(
        PLATFORM,
        sum_task,
        {"task.yaml": b"hidden: true\n"},
        expected={"task.yaml": head.tokens["task.yaml"]},
        message="Not published",
    )

    assert await release.of_task(setup, bob, sum_task) == OPEN


async def test_after_the_end_the_task_stays_visible_and_is_open_only_within_an_extension(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, clock: FakeClock
) -> None:
    await _contest(acme)
    await _publish(setup, acme, sum_task)
    clock.advance(timedelta(hours=3, minutes=30))

    ended = await release.of_task(setup, bob, sum_task)
    await register_contestant(setup, SPRING, 8, time_extension=timedelta(hours=1))
    extended = await release.of_task(setup, bob, sum_task)

    assert ended == TaskRelease(released=True, visible=True, open=False, closed=Closed.ENDED)
    assert extended == OPEN


async def test_closed_submissions_and_an_archived_contest_each_close_the_task(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, ada: Session
) -> None:
    await _contest(acme)
    await _publish(setup, acme, sum_task)

    await _contest(acme, closed="true")
    closed = await release.of_task(setup, bob, sum_task)
    await _contest(acme, state="archived")
    archived = await release.of_task(setup, ada, sum_task)

    assert closed == TaskRelease(
        released=True, visible=True, open=False, closed=Closed.SUBMISSIONS_CLOSED
    )
    assert archived == TaskRelease(released=True, visible=False, open=False, closed=Closed.ARCHIVED)
    with pytest.raises(NotFound):
        await release.of_task(setup, bob, sum_task)


async def test_a_draft_contest_is_no_such_task_to_anyone_but_its_organisers(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session, ada: Session
) -> None:
    await _contest(acme, state="draft")
    await _publish(setup, acme, sum_task)
    await register_contestant(setup, SPRING, 8)

    with pytest.raises(NotFound):
        await release.of_task(setup, bob, sum_task)
    assert await release.of_task(setup, ada, sum_task) == TaskRelease(
        released=False, visible=False, open=False, closed=Closed.NOT_RELEASED
    )


async def test_a_hidden_contest_shows_its_tasks_to_its_contestants_alone(
    setup: Setup, acme: Acme, sum_task: TaskId, bob: Session
) -> None:
    await _contest(acme, visibility="hidden")
    await _publish(setup, acme, sum_task)

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
