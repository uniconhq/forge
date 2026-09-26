"""Deactivate and delete: both need a fresh session, deactivate is reversible
at the forge, and delete refuses to orphan a scope or a shared workflow.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from forge.db.engine import SessionFactory
from forge.domain.errors import (
    FreshSignInRequired,
    NotFound,
    SessionExpired,
    SharedWorkflowOwner,
    SoleAdmin,
)
from forge.domain.identity import AsUser
from forge.domain.ids import OrgName
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.workflows import Visibility
from forge.forges.fake import FakeForge
from forge.services import account, sessions
from forge.settings import Settings


async def _signed_in(factory: SessionFactory, settings: Settings, fake: FakeForge) -> Session:
    async with factory() as db:
        return await sessions.create(
            db, settings, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
        )


async def _aged(factory: SessionFactory, minutes: int) -> None:
    async with factory() as db:
        await db.execute(
            text("update sessions set created_at = :at"),
            {"at": datetime.now(UTC) - timedelta(minutes=minutes)},
        )
        await db.commit()


async def test_deactivating_signs_out_everywhere_and_turns_the_account_off(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    session = await _signed_in(factory, settings, fake)

    async with factory() as db:
        await account.deactivate(db, settings, fake, session)
        with pytest.raises(SessionExpired):
            await sessions.authenticate(db, settings, session.id)
    assert fake.users[7].active is False


async def test_both_actions_need_a_session_under_five_minutes_old(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    signed_in = await _signed_in(factory, settings, fake)
    await _aged(factory, 6)

    async with factory() as db:
        session = await sessions.authenticate(db, settings, signed_in.id)
        with pytest.raises(FreshSignInRequired):
            await account.deactivate(db, settings, fake, session)
        with pytest.raises(FreshSignInRequired):
            await account.delete(db, settings, fake, session)
    assert fake.users[7].active is True


async def test_a_delete_that_would_orphan_a_scope_is_refused_naming_it(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme", "spring"), Role.ADMIN)
    session = await _signed_in(factory, settings, fake)

    async with factory() as db:
        with pytest.raises(SoleAdmin) as refused:
            await account.delete(db, settings, fake, session)
    assert refused.value.extra["scopes"] == [{"kind": "contest", "name": "acme/spring"}]
    assert 7 in fake.users


async def test_an_admin_inherited_from_the_org_keeps_the_scope_covered(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme", "spring"), Role.ADMIN)
    await fake.grant_role(8, Scope("acme"), Role.ADMIN)
    session = await _signed_in(factory, settings, fake)

    async with factory() as db:
        await account.delete(db, settings, fake, session)
    assert 7 not in fake.users
    assert await fake.holders_of(Scope("acme", "spring"), Role.ADMIN) == ()


async def test_a_delete_that_would_orphan_a_shared_workflow_is_refused(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    ada = AsUser(7, fake.mint(7))
    await fake.create_workflow(ada, "ada", "classic", {}, Visibility.PUBLIC)
    session = await _signed_in(factory, settings, fake)

    async with factory() as db:
        with pytest.raises(SharedWorkflowOwner) as refused:
            await account.delete(db, settings, fake, session)
    assert refused.value.extra["workflows"] == ["ada/classic"]


async def test_a_delete_removes_roles_first_then_the_account(
    factory: SessionFactory, settings: Settings, fake: FakeForge
) -> None:
    await fake.create_org(OrgName("acme"), description="Acme")
    await fake.grant_role(7, Scope("acme"), Role.MANAGER)
    session = await _signed_in(factory, settings, fake)

    async with factory() as db:
        await account.delete(db, settings, fake, session)

    operations = [call.operation for call in fake.calls]
    assert operations.index("revoke_role") < operations.index("delete_user")
    with pytest.raises(NotFound):
        await fake.find_user(7)
