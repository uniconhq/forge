"""The setup and the actions over it: an action handed a setup opens, commits
and closes a transaction of its own, saves nothing when it raises or its
commit fails, runs inside a caller's unit of work when handed one, and uses
the setup the process holds when handed neither. `start`, `ready` and `stop`
build, ask and tear down that one setup.
"""

import uuid

import pytest
from sqlalchemy.exc import OperationalError

import forge
from forge import actions
from forge.actions import action
from forge.context import Context
from forge.domain.errors import Conflict, SessionExpired
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.services import identity, sessions, sign_in
from forge.settings import TEST_VALUES, Settings
from forge.setup import Setup
from forge.testing import APP_URL, CALLBACK, FORGE_URL, FakeClock


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
        await identity.current(setup, session.id)


async def test_an_action_that_raises_saves_nothing(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    with pytest.raises(Conflict):
        await _revoke_then_refuse(setup, session.id)

    assert (await identity.current(setup, session.id)).id == session.id


async def test_a_commit_that_fails_raises_out_of_the_action_and_saves_nothing(
    setup: Setup, ctx: Context, fake: FakeForge, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _signed_in(ctx, fake)

    with pytest.raises(RuntimeError, match="at commit"):
        await _revoke_then_fail_to_commit(setup, session.id, monkeypatch)

    assert (await identity.current(setup, session.id)).id == session.id


async def test_an_action_handed_a_context_runs_inside_the_callers_unit_of_work(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    await sessions.revoke_all(ctx, 7)
    await ctx.db.rollback()

    assert (await identity.current(setup, session.id)).id == session.id


async def test_an_action_handed_neither_uses_the_setup_the_process_holds(
    held_setup: Setup, ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session = await _signed_in(ctx, fake)

    await sessions.revoke_all(7)

    with pytest.raises(SessionExpired):
        await identity.current(session.id)
    assert forge.now() == clock.now()
    assert sign_in.sign_up_url() == f"{FORGE_URL}/user/sign_up"


async def test_before_start_an_action_names_forge_start() -> None:
    with pytest.raises(RuntimeError, match=r"forge\.start"):
        await sessions.revoke_all(7)
    with pytest.raises(RuntimeError, match=r"forge\.start"):
        forge.now()
    with pytest.raises(RuntimeError, match=r"forge\.start"):
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

    forge.start(sign_in_redirect_uri=CALLBACK)
    try:
        await forge.ready()
        assert forge.now().tzinfo is not None
        assert sign_in.start("/").url.startswith(FORGE_URL)
        await sessions.revoke_all(7)
        with pytest.raises(RuntimeError, match="already"):
            forge.start(sign_in_redirect_uri=CALLBACK)
    finally:
        await forge.stop()

    assert not actions.holding()
    with pytest.raises(RuntimeError, match=r"forge\.start"):
        await forge.ready()


async def test_ready_raises_when_the_database_does_not_answer() -> None:
    settings = Settings.for_tests(database_url="postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    setup = Setup.build(settings, sign_in_redirect_uri=CALLBACK)
    try:
        with pytest.raises((OperationalError, TimeoutError)):
            await setup.ready()
    finally:
        await setup.stop()
