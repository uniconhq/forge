"""Sessions: the credential is ciphertext at rest, the two lifetimes end a
session, the refresh is a compare-and-set that survives another process
winning it, and listing and revoking work.
"""

import asyncio
from datetime import timedelta

import psycopg
import pytest

from forge.domain.errors import NotFound, SessionExpired, Unauthenticated
from forge.domain.identity import Credential
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import sessions
from forge.settings import Settings
from forge.testing import CALLBACK_PATH, FakeClock


async def _signed_in(ctx: Context, fake: FakeForge, user_id: int = 7) -> tuple[Session, Credential]:
    credential = fake.mint(user_id)
    session = await sessions.create(
        ctx, user=fake.users[user_id], credential=credential, ip="203.0.113.7", user_agent="Browser"
    )
    await ctx.db.commit()
    return session, credential


def _column(database_url: str, column: str) -> object:
    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://")) as db:
        row = db.execute(f"select {column} from sessions").fetchone()
        assert row is not None
        return row[0]


async def test_the_credential_is_ciphertext_at_rest(
    ctx: Context, fake: FakeForge, migrated_database_url: str
) -> None:
    session, credential = await _signed_in(ctx, fake)

    stored = _column(migrated_database_url, "credential")
    assert isinstance(stored, bytes)
    assert credential.access.encode() not in stored
    assert await sessions.credential_for(ctx, session.id) == credential


async def test_a_session_older_than_the_hard_limit_is_refused(
    ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(ctx.settings.session_hard_ttl)

    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, session.id)


async def test_a_session_unused_for_the_idle_limit_is_refused(
    ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(ctx.settings.session_idle_ttl + timedelta(seconds=1))

    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, session.id)


async def test_a_fresh_session_is_accepted_and_an_unknown_one_is_not(
    ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(timedelta(days=13))

    assert (await sessions.authenticate(ctx, session.id)).user_id == 7
    with pytest.raises(Unauthenticated):
        await sessions.authenticate(ctx, session.id.__class__(int=0))


async def test_use_moves_the_idle_clock(ctx: Context, fake: FakeForge, clock: FakeClock) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(timedelta(days=10))
    await sessions.authenticate(ctx, session.id)
    clock.advance(timedelta(days=10))

    assert (await sessions.authenticate(ctx, session.id)).user_id == 7


async def test_a_credential_near_expiry_is_refreshed_once_for_two_callers(
    setup: Setup, ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(timedelta(minutes=57))

    async def use() -> Credential:
        async with setup.unit_of_work() as own:
            return await sessions.credential_for(own, session.id)

    first, second = await asyncio.gather(use(), use())

    assert first == second
    assert fake.refreshes == 1
    assert first.access in fake.state.credentials


async def test_a_refresh_another_process_won_is_read_back(
    setup: Setup, settings: Settings, ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(timedelta(minutes=57))
    other = Setup.build(settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock)

    async def use(on: Setup) -> Credential:
        async with on.unit_of_work() as own:
            return await sessions.credential_for(own, session.id)

    try:
        first, second = await asyncio.gather(use(setup), use(other))
    finally:
        await other.stop()

    assert first == second
    assert (await sessions.authenticate(ctx, session.id)).user_id == 7


async def test_a_refresh_the_forge_refuses_ends_the_session(
    ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    clock.advance(timedelta(minutes=57))
    fake.refuse_refresh = True

    with pytest.raises(SessionExpired):
        await sessions.credential_for(ctx, session.id)
    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, session.id)


async def test_a_user_sees_and_ends_their_other_sessions(ctx: Context, fake: FakeForge) -> None:
    first, _ = await _signed_in(ctx, fake)
    second, _ = await _signed_in(ctx, fake)
    other, _ = await _signed_in(ctx, fake, user_id=8)

    listed = await sessions.list_for(ctx, first)
    assert {info.id for info in listed} == {first.id, second.id}
    assert [info.current for info in listed if info.id == first.id] == [True]
    assert listed[0].ip is not None and str(listed[0].ip) == "203.0.113.7"

    await sessions.revoke(ctx, second.id, owner=7)
    with pytest.raises(NotFound):
        await sessions.revoke(ctx, other.id, owner=7)
    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, second.id)

    await sessions.revoke_all(ctx, 7)
    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, first.id)
    assert (await sessions.authenticate(ctx, other.id)).user_id == 8


async def test_the_sweeper_drops_sessions_nobody_can_be_shown(
    ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session, _ = await _signed_in(ctx, fake)
    await sessions.revoke(ctx, session.id)
    await ctx.db.commit()
    clock.advance(ctx.settings.session_hard_ttl + timedelta(days=1))

    assert await sessions.sweep(ctx) == 1
    with pytest.raises(Unauthenticated):
        await sessions.authenticate(ctx, session.id)
