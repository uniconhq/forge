"""Live updates, over a real Postgres and the fake. Every change of a
grading's status publishes its id once, and nothing is published for a unit
of work that rolls back. A push from the forge that `check` let in nudges
the asker and the organisers of a clarification, the contestants of an
announcement, and a task's contestants only once it is released. A session's
stream hears what it may and nothing else, whichever process published it,
carries a kind and an id and never what changed, says it is still there
when nothing happens, and ends once its session has, without ever keeping
an idle session alive itself. A stream opened before its process was
listening, or too slow to keep up, is told to resync, and a session that
opens too many streams has its oldest ended.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import psycopg
import pytest

from forge.adapters.git.fake import FakeForge
from forge.db.tables import Session as SessionRow
from forge.domain.ids import OrgId
from forge.domain.live import CHANNEL, Audience, Nudge, NudgeKind, read_payload
from forge.domain.roles import Role, RoleGrant, Scope, contest_scope, task_scope
from forge.domain.submissions import SubmittedInput
from forge.runtime.broker import QUEUE_MOST, STREAMS_PER_SESSION, Subscription
from forge.runtime.setup import Setup
from forge.services import events, gradings, live, runs, sessions, submissions
from forge.settings import Settings
from forge.testing import CALLBACK_PATH, FakeClock
from tests.services.conftest import SPRING, Acme, Entered, make_task, organiser, signed_in, upload

SOURCE = b"print(1)\n"


@pytest.fixture
async def listening(migrated_database_url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """A connection listening on the channel nudges are published on."""
    url = migrated_database_url.replace("postgresql+psycopg://", "postgresql://")
    connection = await psycopg.AsyncConnection.connect(url, autocommit=True)
    await connection.execute(f"LISTEN {CHANNEL}")
    try:
        yield connection
    finally:
        await connection.close()


async def _heard(connection: psycopg.AsyncConnection, wait: float = 0.5) -> list[Nudge]:
    found = []
    async for notify in connection.notifies(timeout=wait):
        nudge = read_payload(notify.payload)
        assert nudge is not None
        found.append(nudge)
    return found


async def _submit(setup: Setup, acme: Acme, entered: Entered) -> uuid.UUID:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    submitted = await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )
    assert submitted.grading is not None
    return submitted.grading.id


async def test_each_status_change_of_a_grading_publishes_its_id_once(
    setup: Setup,
    acme: Acme,
    entered: Entered,
    listening: psycopg.AsyncConnection,
    clock: FakeClock,
) -> None:
    grading = await _submit(setup, acme, entered)
    queued_and_dispatched = await _heard(listening)

    async with setup.unit_of_work() as ctx:
        row = await gradings.find(ctx, grading)
        assert row is not None
        key = gradings.envelope_key_of(ctx, row)
        token = gradings.callback_token_of(ctx, row)
    await runs.envelope(setup, grading, key)
    running = await _heard(listening)
    report = {"event": "progress", "step": "run", "done": 1, "total": 2}
    await runs.callback(setup, grading, f"Bearer {token}", json.dumps(report).encode())
    progressed = await _heard(listening)
    # Staff cancel only a grading in system_error: this one is past its
    # deadline, which writes nothing and so nudges nobody.
    async with setup.unit_of_work() as ctx:
        running_row = await gradings.find(ctx, grading)
        assert running_row is not None and running_row.deadline_at is not None
        clock.set(running_row.deadline_at)
    manager = await organiser(setup, acme.fake, 7, task_scope(entered.task), Role.MANAGER)
    await gradings.cancel(setup, manager, grading, "The checker crashed; this one is not counted.")
    cancelled = await _heard(listening)

    for heard in (queued_and_dispatched, running, progressed, cancelled):
        assert {(nudge.kind, nudge.id) for nudge in heard} == {(NudgeKind.GRADING, str(grading))}
    assert [len(heard) for heard in (queued_and_dispatched, running, progressed, cancelled)] == [
        2,
        1,
        1,
        1,
    ]
    nudge = cancelled[0]
    assert (nudge.user, nudge.scope, nudge.contest) == (8, task_scope(entered.task), None)


async def test_a_unit_of_work_that_rolls_back_publishes_nothing(
    setup: Setup, acme: Acme, entered: Entered, listening: psycopg.AsyncConnection
) -> None:
    with pytest.raises(RuntimeError, match="halfway"):
        async with setup.unit_of_work() as ctx:
            ctx.nudge(Nudge(NudgeKind.GRADING, "g", user=8))
            raise RuntimeError("halfway")

    assert await _heard(listening) == []


def _event(repository: str, number: int = 1, *, label: str | None = None) -> bytes:
    kind = "clarification" if repository.endswith(".desk") else "announcement"
    return json.dumps(
        {
            "repository": {"name": repository, "owner": {"login": "acme"}},
            "issue": {"number": number, "labels": [{"name": label if label else kind}]},
        }
    ).encode()


async def test_a_push_nudges_who_may_hear_of_each_kind_of_thread(
    setup: Setup, acme: Acme, entered: Entered, listening: psycopg.AsyncConnection
) -> None:
    hidden = await make_task(setup, acme, "hidden")

    await events.publish(setup, OrgId("acme"), "issue_comment", _event("spring.u8.desk", 2))
    await events.publish(setup, OrgId("acme"), "issues", _event("spring.contest"))
    await events.publish(setup, OrgId("acme"), "issues", _event("spring.sum.task"))
    await events.publish(setup, OrgId("acme"), "issues", _event("spring.hidden.task"))
    await events.publish(setup, OrgId("other"), "issues", _event("spring.contest"))
    await events.publish(setup, OrgId("acme"), "push", _event("spring.contest"))
    await events.publish(setup, OrgId("acme"), "issues", _event("spring.contest", label="bug"))

    asked, contest, released, unreleased = await _heard(listening)
    assert (asked.kind, asked.user, asked.scope, asked.contest) == (
        NudgeKind.CLARIFICATION,
        8,
        contest_scope(SPRING),
        None,
    )
    assert (contest.kind, contest.scope, contest.contest) == (
        NudgeKind.ANNOUNCEMENT,
        contest_scope(SPRING),
        SPRING,
    )
    assert (released.scope, released.contest) == (task_scope(entered.task), SPRING)
    assert (unreleased.scope, unreleased.contest) == (task_scope(hidden), None)


async def _first(stream: AsyncIterator[Nudge | None]) -> Nudge:
    async with asyncio.timeout(5):
        async for nudge in stream:
            if nudge is not None:
                return nudge
    raise AssertionError("the stream ended")


async def _within(stream: AsyncIterator[Nudge | None], beats: int) -> list[Nudge]:
    """Every nudge the stream gives until it has sent `beats` heartbeats,
    which it does whenever it is quiet for its heartbeat, so the stream is
    read for about that many heartbeats and never cut off mid-read.
    """
    heard: list[Nudge] = []
    async with asyncio.timeout(5):
        while beats:
            nudge = await anext(stream)
            if nudge is None:
                beats -= 1
            else:
                heard.append(nudge)
    return heard


async def test_a_stream_hears_what_another_process_publishes_and_a_stranger_does_not(
    setup: Setup,
    settings: Settings,
    acme: Acme,
    entered: Entered,
    fake: FakeForge,
    clock: FakeClock,
) -> None:
    other = Setup.build(settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock)
    fake.add_user(50, "eve")
    eve = await signed_in(setup, fake, 50)
    mine = live.stream(entered.session.id, setup=other, heartbeat=0.1)
    theirs = live.stream(eve.id, setup=other, heartbeat=0.1)
    try:
        assert await anext(mine) is None
        assert await anext(theirs) is None
        await asyncio.wait_for(other.broker.ready.wait(), 5)
        # The first stream started the broker, so it subscribed before it
        # listened and is told to resync. The second is told too only if it
        # subscribed before the broker's connection came up, which is a race.
        assert (await _first(mine)).kind is NudgeKind.RESYNC
        assert {nudge.kind for nudge in await _within(theirs, 3)} <= {NudgeKind.RESYNC}

        grading = await _submit(setup, acme, entered)

        heard = await _first(mine)
        assert (heard.kind, heard.id) == (NudgeKind.GRADING, str(grading))
        assert await _within(theirs, 3) == []
    finally:
        await mine.aclose()
        await theirs.aclose()
        await other.stop()


async def test_a_stream_ends_once_its_session_has(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    stream = live.stream(entered.session.id, setup=setup, heartbeat=0.05, recheck=0.0)
    assert await anext(stream) is None

    await sessions.revoke(setup, entered.session.id)

    with pytest.raises(StopAsyncIteration):
        async with asyncio.timeout(5):
            while True:
                await anext(stream)


async def test_an_organisers_audience_holds_their_roles_and_a_contestants_their_contests(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    bob = await live.audience(setup, entered.session)
    ada = await live.audience(setup, await signed_in(setup, acme.fake, 7))

    assert (bob.user_id, bob.contests) == (8, frozenset({SPRING}))
    assert ada.contests == frozenset()
    assert RoleGrant(Scope("acme"), Role.ADMIN) in ada.grants


async def test_a_session_opening_one_stream_too_many_ends_its_oldest(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    streams = [
        live.stream(entered.session.id, setup=setup, heartbeat=0.05)
        for _ in range(STREAMS_PER_SESSION + 1)
    ]
    try:
        for stream in streams:
            assert await anext(stream) is None

        with pytest.raises(StopAsyncIteration):
            async with asyncio.timeout(5):
                while True:
                    await anext(streams[0])
        async with asyncio.timeout(5):
            await anext(streams[1])
    finally:
        for stream in streams:
            await stream.aclose()


async def test_a_stream_never_keeps_an_idle_session_alive(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    stream = live.stream(entered.session.id, setup=setup, heartbeat=0.05, recheck=0.0)
    try:
        assert await anext(stream) is None
        seen = await _last_seen(setup, entered.session.id)
        clock.advance(timedelta(minutes=5))
        for _ in range(3):
            await anext(stream)
    finally:
        await stream.aclose()

    assert await _last_seen(setup, entered.session.id) == seen


async def _last_seen(setup: Setup, session_id: uuid.UUID) -> object:
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(SessionRow, session_id)
        assert row is not None
        return row.last_seen_at


def test_a_stream_too_slow_to_keep_up_is_told_to_resync() -> None:
    subscription = Subscription(Audience(8, (), frozenset()))
    for number in range(QUEUE_MOST + 5):
        subscription.offer(Nudge(NudgeKind.GRADING, str(number), user=8))
    subscription.offer(Nudge(NudgeKind.GRADING, "someone else's", user=9))

    assert subscription.queue.qsize() == 5
    assert subscription.queue.get_nowait().kind is NudgeKind.RESYNC
