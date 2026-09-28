"""The setup and the actions over it: an action handed a setup opens, commits
and closes a transaction of its own, saves nothing when it raises or its
commit fails, runs inside a caller's unit of work when handed one, and uses
the setup the process holds when handed neither. `start`, `ready` and `stop`
build, ask and tear down that one setup; a database that does not answer is
`NotReady`, with the cause in the log and not in the error.
"""

import uuid

import pytest

import forge.api
from forge import actions
from forge.actions import action
from forge.context import Context
from forge.domain.errors import Conflict, NotReady, SessionExpired
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.services import identity, sessions, sign_in
from forge.settings import TEST_VALUES, Settings
from forge.setup import Setup
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

    assert not actions.holding()
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
