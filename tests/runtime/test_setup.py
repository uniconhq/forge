"""The setup and the actions over it: an action handed a setup opens, commits
and closes a transaction of its own, saves nothing when it raises or its
commit fails, runs inside a caller's unit of work when handed one, and uses
the setup the process holds when handed neither. Work a unit of work leaves
for after its commit runs then, on a unit of work of its own, and never when
it rolls back. Work it leaves for the background starts then too, and
nobody waits for it: it fails into the log, and `stop` tells it the process
is stopping and cuts it short after a grace. Work
it leaves for a rollback runs, the latest first, when it
raises or its commit fails, never when it commits, and one that fails is
logged without hiding the error. Work left for its end runs either way, once
its connection is back in the pool, and an action that has only read can
hand its connection back partway while one that has written keeps it.
`start`, `ready` and `stop` build, ask and tear down that one setup; a
database that does not answer is `NotReady`, with the cause in the log and
not in the error.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update

import forge.api
from forge.db.tables import Session as SessionRow
from forge.domain.errors import Conflict, NotReady, SessionExpired, Unauthenticated
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.runtime import setup as runtime_setup
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.runtime.held import holding
from forge.runtime.setup import Setup
from forge.services import identity, sessions, sign_in
from forge.settings import TEST_VALUES, Settings
from forge.testing import APP_URL, CALLBACK_PATH, FORGE_URL, FakeClock, logged


async def _signed_in(ctx: Context, fake: FakeForge) -> Session:
    session = await sessions.create(
        ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
    )
    await ctx.db.commit()
    return session


async def _refuse_to_commit() -> None:
    raise RuntimeError("the database went away at commit")


@action
async def _revoke_then_refuse(ctx: Context, session_id: uuid.UUID) -> None:
    await sessions.revoke(ctx, session_id)
    raise Conflict("refused after writing")


@action
async def _revoke_then_fail_to_commit(
    ctx: Context, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sessions.revoke(ctx, session_id)
    monkeypatch.setattr(ctx.db, "commit", _refuse_to_commit)


async def test_an_action_handed_a_setup_commits_its_own_transaction(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    await sessions.revoke_all(setup, 7)

    with pytest.raises(SessionExpired):
        await identity.current(session.id, setup=setup)


async def test_an_action_that_raises_saves_nothing(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    with pytest.raises(Conflict):
        await _revoke_then_refuse(setup, session.id)

    assert (await identity.current(session.id, setup=setup)).id == session.id


async def test_a_commit_that_fails_raises_out_of_the_action_and_saves_nothing(
    setup: Setup, ctx: Context, fake: FakeForge, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _signed_in(ctx, fake)

    with pytest.raises(RuntimeError, match="at commit"):
        await _revoke_then_fail_to_commit(setup, session.id, monkeypatch)

    assert (await identity.current(session.id, setup=setup)).id == session.id


async def test_an_action_handed_a_context_runs_inside_the_callers_unit_of_work(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    await sessions.revoke_all(ctx, 7)
    await ctx.db.rollback()

    assert (await identity.current(session.id, setup=setup)).id == session.id


async def test_work_left_for_the_end_lands_when_the_unit_of_work_rolls_back(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    with pytest.raises(RuntimeError, match="halfway"):
        async with setup.unit_of_work() as own:
            sessions.revoke_at_end(own, session.id)
            raise RuntimeError("halfway")

    with pytest.raises(SessionExpired):
        await identity.current(session.id, setup=setup)


async def test_work_left_for_the_end_runs_after_a_commit_once_the_connection_is_back(
    settings: Settings, fake: FakeForge, clock: FakeClock
) -> None:
    one = Setup.build(
        settings.model_copy(
            update={
                "database_pool_size": 1,
                "database_pool_overflow": 0,
                "database_pool_wait": timedelta(seconds=1),
            }
        ),
        callback_path=CALLBACK_PATH,
        forge=fake,
        clock=clock,
    )
    try:
        async with one.unit_of_work() as ctx:
            session = await _signed_in(ctx, fake)
            await sessions.authenticate(ctx, session.id)
            sessions.revoke_at_end(ctx, session.id)
        with pytest.raises(SessionExpired):
            await identity.current(session.id, setup=one)
    finally:
        await one.stop()


async def test_an_action_that_has_only_read_lets_go_of_its_connection(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    async with setup.unit_of_work() as own:
        await sessions.authenticate(own, session.id)
        assert own.db.in_transaction()
        await own.let_go()
        assert not own.db.in_transaction()


async def test_an_action_that_has_written_keeps_its_connection(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    with pytest.raises(Conflict):
        async with setup.unit_of_work() as own:
            await own.db.execute(
                update(SessionRow).where(SessionRow.id == session.id).values(revoked_at=own.now)
            )
            await own.let_go()
            assert own.db.in_transaction()
            raise Conflict("refused after writing")

    assert (await identity.current(session.id, setup=setup)).id == session.id


async def test_a_sign_in_sweeps_the_sessions_that_ended_long_ago(
    setup: Setup, ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session = await _signed_in(ctx, fake)
    await sessions.revoke_all(ctx, 7)
    await ctx.db.commit()
    clock.advance(setup.settings.session_hard_ttl + timedelta(days=1))

    async with setup.unit_of_work() as later:
        await _signed_in(later, fake)

    with pytest.raises(Unauthenticated):
        await identity.current(session.id, setup=setup)


async def test_work_left_for_after_the_commit_runs_on_a_unit_of_work_of_its_own(
    setup: Setup, fake: FakeForge
) -> None:
    seen: list[int] = []

    async def count_sessions(later: Context) -> None:
        found = await sessions.list_for(later, await _signed_in(later, fake))
        seen.append(len(found))

    async with setup.unit_of_work() as ctx:
        await _signed_in(ctx, fake)
        ctx.after_commit(count_sessions)
        assert seen == []

    assert seen == [2]


async def test_work_left_for_the_background_starts_after_the_commit_and_nobody_waits_for_it(
    setup: Setup, fake: FakeForge
) -> None:
    started = asyncio.Event()
    go_on = asyncio.Event()
    seen: list[int] = []

    async def count_sessions(later: Context) -> None:
        started.set()
        await go_on.wait()
        found = await sessions.list_for(later, await _signed_in(later, fake))
        seen.append(len(found))

    async with setup.unit_of_work() as ctx:
        await _signed_in(ctx, fake)
        ctx.in_background(count_sessions)
        assert not started.is_set()

    await asyncio.wait_for(started.wait(), timeout=5)
    assert seen == []
    go_on.set()
    await setup.settle()
    assert seen == [2]


async def test_work_left_for_the_background_does_not_start_when_the_unit_of_work_rolls_back(
    setup: Setup,
) -> None:
    ran: list[bool] = []

    async def note(later: Context) -> None:
        ran.append(True)

    with pytest.raises(RuntimeError, match="halfway"):
        async with setup.unit_of_work() as ctx:
            ctx.in_background(note)
            raise RuntimeError("halfway")
    await setup.settle()

    assert ran == []


async def test_work_in_the_background_that_fails_is_logged(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(later: Context) -> None:
        raise RuntimeError("in the background")

    async with setup.unit_of_work() as ctx:
        ctx.in_background(fail)
    await setup.settle()

    assert logged(caplog, "setup.background_failed")


async def test_stop_lets_work_in_the_background_finish_what_it_is_doing(
    settings: Settings, fake: FakeForge, clock: FakeClock
) -> None:
    started = asyncio.Event()
    told: list[bool] = []

    async def brief(later: Context) -> None:
        started.set()
        await asyncio.sleep(0.2)
        told.append(later.stopping())

    built = Setup.build(settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock)
    async with built.unit_of_work() as ctx:
        ctx.in_background(brief)
        assert not ctx.stopping()
    await asyncio.wait_for(started.wait(), timeout=5)

    await built.stop()

    assert told == [True]


async def test_stop_cuts_short_the_work_running_in_the_background_past_its_grace(
    settings: Settings, fake: FakeForge, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_setup, "STOP_GRACE_SECONDS", 0.1)
    started = asyncio.Event()
    cut: list[bool] = []

    async def forever(later: Context) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cut.append(True)
            raise

    built = Setup.build(settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock)
    async with built.unit_of_work() as ctx:
        ctx.in_background(forever)
    await asyncio.wait_for(started.wait(), timeout=5)

    await built.stop()

    assert cut == [True]


async def test_work_left_for_after_the_commit_does_not_run_when_the_unit_of_work_rolls_back(
    setup: Setup,
) -> None:
    ran: list[bool] = []

    async def note(later: Context) -> None:
        ran.append(True)

    with pytest.raises(RuntimeError, match="halfway"):
        async with setup.unit_of_work() as ctx:
            ctx.after_commit(note)
            raise RuntimeError("halfway")

    assert ran == []


async def test_work_after_the_commit_that_fails_is_logged_and_the_commit_stands(
    setup: Setup, fake: FakeForge, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(later: Context) -> None:
        raise RuntimeError("after")

    async with setup.unit_of_work() as ctx:
        session = await _signed_in(ctx, fake)
        ctx.after_commit(fail)

    assert (await identity.current(session.id, setup=setup)).id == session.id
    assert logged(caplog, "setup.after_commit_failed")


async def test_work_left_for_a_rollback_runs_the_latest_first_and_the_error_goes_on(
    setup: Setup,
) -> None:
    ran: list[str] = []

    async def first() -> None:
        ran.append("first")

    async def second() -> None:
        ran.append("second")

    with pytest.raises(RuntimeError, match="halfway"):
        async with setup.unit_of_work() as ctx:
            ctx.after_rollback(first)
            ctx.after_rollback(second)
            raise RuntimeError("halfway")

    assert ran == ["second", "first"]


async def test_work_left_for_a_rollback_runs_when_the_commit_fails(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[bool] = []

    async def note() -> None:
        ran.append(True)

    with pytest.raises(RuntimeError, match="went away at commit"):
        async with setup.unit_of_work() as ctx:
            ctx.after_rollback(note)
            monkeypatch.setattr(ctx.db, "commit", _refuse_to_commit)

    assert ran == [True]


async def test_work_left_for_a_rollback_does_not_run_when_the_unit_of_work_commits(
    setup: Setup,
) -> None:
    ran: list[bool] = []

    async def note() -> None:
        ran.append(True)

    async with setup.unit_of_work() as ctx:
        ctx.after_rollback(note)

    assert ran == []


async def test_work_for_a_rollback_that_fails_is_logged_and_the_rest_and_the_error_stand(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[bool] = []
    error = Conflict("the step's own")

    async def note() -> None:
        ran.append(True)

    async def fail() -> None:
        raise RuntimeError("the undo went wrong")

    with pytest.raises(Conflict) as raised:
        async with setup.unit_of_work() as ctx:
            ctx.after_rollback(note)
            ctx.after_rollback(fail)
            raise error

    assert raised.value is error
    assert ran == [True]
    assert logged(caplog, "setup.after_rollback_failed")


async def test_an_action_handed_neither_uses_the_setup_the_process_holds(
    held_setup: Setup, ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session = await _signed_in(ctx, fake)

    await sessions.revoke_all(7)

    with pytest.raises(SessionExpired):
        await identity.current(session.id)
    assert forge.api.now() == clock.now()
    assert forge.api.public_url() == f"{APP_URL}/"
    assert sign_in.sign_up_url() == f"{FORGE_URL}/user/sign_up"


async def test_before_start_an_action_names_forge_start() -> None:
    with pytest.raises(RuntimeError, match=r"forge\.api\.start"):
        await sessions.revoke_all(7)
    with pytest.raises(RuntimeError, match=r"forge\.api\.start"):
        forge.api.now()
    with pytest.raises(RuntimeError, match=r"forge\.api\.start"):
        forge.api.public_url()
    with pytest.raises(RuntimeError, match=r"forge\.api\.start"):
        sign_in.start("/")


async def test_start_builds_the_setup_from_the_environment_and_stop_lets_it_go(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        **TEST_VALUES,
        "database_url": migrated_database_url,
        "public_url": APP_URL,
        "forge_public_url": FORGE_URL,
    }
    for name, value in values.items():
        monkeypatch.setenv(f"UNICON_{name.upper()}", str(value))

    forge.api.start(callback_path=CALLBACK_PATH)
    try:
        await forge.api.ready()
        assert forge.api.now().tzinfo is not None
        assert forge.api.public_url() == f"{APP_URL}/"
        assert sign_in.start("/").url.startswith(FORGE_URL)
        await sessions.revoke_all(7)
        with pytest.raises(RuntimeError, match="already"):
            forge.api.start(callback_path=CALLBACK_PATH)
    finally:
        await forge.api.stop()

    assert not holding()
    with pytest.raises(RuntimeError, match=r"forge\.api\.start"):
        await forge.api.ready()


async def test_the_sign_in_callback_is_the_path_joined_to_the_public_url() -> None:
    settings = Settings.for_tests(public_url="https://unicon.example.test")
    setup = Setup.build(settings, callback_path=CALLBACK_PATH)
    try:
        started = sign_in.start("/", setup=setup)
    finally:
        await setup.stop()

    assert "redirect_uri=https%3A%2F%2Funicon.example.test%2Fapi%2Fv1%2Fauth%2Fcallback" in (
        started.url
    )


def test_a_callback_that_is_not_a_path_is_refused() -> None:
    with pytest.raises(ValueError, match="not a path"):
        Setup.build(Settings.for_tests(), callback_path="https://elsewhere.test/callback")


async def test_a_database_that_does_not_answer_is_not_ready_and_only_the_log_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings.for_tests(database_url="postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    setup = Setup.build(settings, callback_path=CALLBACK_PATH)
    try:
        with pytest.raises(NotReady) as refused:
            await setup.ready()
    finally:
        await setup.stop()

    assert refused.value.detail == "The database did not answer."
    assert refused.value.extra == {}
    (record,) = logged(caplog, "setup.not_ready")
    assert record["error"]
