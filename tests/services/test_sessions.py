"""Sessions: the credential is ciphertext at rest, the two lifetimes end a
session, the refresh is a compare-and-set, and listing and revoking work.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy import text

from forge.db.engine import SessionFactory
from forge.domain.errors import NotFound, SessionExpired, Unauthenticated
from forge.domain.identity import Credential
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.services import sessions
from forge.settings import Settings


async def _signed_in(
    factory: SessionFactory, settings: Settings, fake: FakeForge, user_id: int = 7
) -> tuple[Session, Credential]:
    credential = fake.mint(user_id)
    async with factory() as db:
        session = await sessions.create(
            db,
            settings,
            user=fake.users[user_id],
            credential=credential,
            ip="203.0.113.7",
            user_agent="Browser",
        )
    return session, credential


def _column(database_url: str, column: str) -> object:
    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://")) as db:
        return db.execute(f"select {column} from sessions").fetchone()[0]  # type: ignore[index]


async def _age(factory: SessionFactory, column: str, value: datetime) -> None:
    async with factory() as db:
        await db.execute(text(f"update sessions set {column} = :value"), {"value": value})
        await db.commit()


async def test_the_credential_is_ciphertext_at_rest(
    factory: SessionFactory, settings: Settings, fake: FakeForge, migrated_database_url: str
) -> None:
    session, credential = await _signed_in(factory, settings, fake)

    stored = _column(migrated_database_url, "credential")
    assert isinstance(stored, bytes)
    assert credential.access.encode() not in stored
    async with factory() as db:
        assert await sessions.credential_for(db, settings, fake, session.id) == credential


async def test_a_session_older_than_the_hard_limit_is_refused(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session, _ = await _signed_in(factory, settings, fake)
    await _age(factory, "expires_at", datetime.now(UTC) - timedelta(seconds=1))

    async with factory() as db:
        with pytest.raises(SessionExpired):
            await sessions.authenticate(db, settings, session.id)


async def test_a_session_unused_for_the_idle_limit_is_refused(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session, _ = await _signed_in(factory, settings, fake)
    await _age(
        factory, "last_seen_at", datetime.now(UTC) - settings.session_idle_ttl - timedelta(1)
    )

    async with factory() as db:
        with pytest.raises(SessionExpired):
            await sessions.authenticate(db, settings, session.id)


async def test_a_fresh_session_is_accepted_and_an_unknown_one_is_not(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session, _ = await _signed_in(factory, settings, fake)
    async with factory() as db:
        assert (await sessions.authenticate(db, settings, session.id)).user_id == 7
        with pytest.raises(Unauthenticated):
            await sessions.authenticate(db, settings, session.id.__class__(int=0))


async def test_a_credential_near_expiry_is_refreshed_once_for_two_callers(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session, _ = await _signed_in(factory, settings, fake)
    await _age(factory, "credential_expires_at", datetime.now(UTC) + timedelta(minutes=1))

    async def use() -> Credential:
        async with factory() as db:
            return await sessions.credential_for(db, settings, fake, session.id)

    first, second = await asyncio.gather(use(), use())

    assert first == second
    assert fake.refreshes == 1
    assert first.access in fake.credentials


async def test_a_refresh_the_forge_refuses_ends_the_session(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session, _ = await _signed_in(factory, settings, fake)
    await _age(factory, "credential_expires_at", datetime.now(UTC))
    fake.refuse_refresh = True

    async with factory() as db:
        with pytest.raises(SessionExpired):
            await sessions.credential_for(db, settings, fake, session.id)
        with pytest.raises(SessionExpired):
            await sessions.authenticate(db, settings, session.id)


async def test_a_user_sees_and_ends_their_other_sessions(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    first, _ = await _signed_in(factory, settings, fake)
    second, _ = await _signed_in(factory, settings, fake)
    other, _ = await _signed_in(factory, settings, fake, user_id=8)

    async with factory() as db:
        listed = await sessions.list_for(db, settings, first)
        assert {info.id for info in listed} == {first.id, second.id}
        assert [info.current for info in listed if info.id == first.id] == [True]
        assert listed[0].ip is not None and str(listed[0].ip) == "203.0.113.7"

        await sessions.revoke(db, second.id, owner=7)
        with pytest.raises(NotFound):
            await sessions.revoke(db, other.id, owner=7)
        with pytest.raises(SessionExpired):
            await sessions.authenticate(db, settings, second.id)

        await sessions.revoke_all(db, 7)
        with pytest.raises(SessionExpired):
            await sessions.authenticate(db, settings, first.id)
        assert (await sessions.authenticate(db, settings, other.id)).user_id == 8


async def test_the_sweeper_drops_sessions_nobody_can_be_shown(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session, _ = await _signed_in(factory, settings, fake)
    await _age(factory, "revoked_at", datetime.now(UTC) - settings.session_hard_ttl - timedelta(1))

    async with factory() as db:
        assert await sessions.sweep(db, settings) == 1
        with pytest.raises(Unauthenticated):
            await sessions.authenticate(db, settings, session.id)
