"""Deactivate and delete: both need a fresh session, deactivate is reversible
at the forge, and delete refuses to orphan a scope or a shared workflow.
"""

from datetime import timedelta

import pytest

from forge.context import Context
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
from forge.testing import FakeClock


async def _signed_in(ctx: Context, fake: FakeForge) -> Session:
    session = await sessions.create(
        ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
    )
    await ctx.db.commit()
    return session


async def test_deactivating_signs_out_everywhere_and_turns_the_account_off(
    ctx: Context, fake: FakeForge
) -> None:
    session = await _signed_in(ctx, fake)

    await account.deactivate(ctx, session)

    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, session.id)
    assert fake.users[7].active is False


async def test_both_actions_need_a_session_under_five_minutes_old(
    ctx: Context, fake: FakeForge, clock: FakeClock
) -> None:
    session = await _signed_in(ctx, fake)
    clock.advance(timedelta(minutes=6))

    with pytest.raises(FreshSignInRequired):
        await account.deactivate(ctx, session)
    with pytest.raises(FreshSignInRequired):
        await account.delete(ctx, session)
    assert fake.users[7].active is True


async def test_a_delete_that_would_orphan_a_scope_is_refused_naming_it(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme", "spring"), Role.ADMIN)
    session = await _signed_in(ctx, fake)

    with pytest.raises(SoleAdmin) as refused:
        await account.delete(ctx, session)
    assert refused.value.extra["scopes"] == [{"kind": "contest", "name": "acme/spring"}]
    assert 7 in fake.users


async def test_an_admin_inherited_from_the_org_keeps_the_scope_covered(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme", "spring"), Role.ADMIN)
    await fake.orgs.grant_role(8, Scope("acme"), Role.ADMIN)
    session = await _signed_in(ctx, fake)

    await account.delete(ctx, session)

    assert 7 not in fake.users
    assert await fake.orgs.holders_of(Scope("acme", "spring"), Role.ADMIN) == ()


async def test_a_delete_that_would_orphan_a_shared_workflow_is_refused(
    ctx: Context, fake: FakeForge
) -> None:
    ada = AsUser(7, fake.mint(7))
    await fake.workflows.create_workflow(ada, "ada", "classic", {}, Visibility.PUBLIC)
    session = await _signed_in(ctx, fake)

    with pytest.raises(SharedWorkflowOwner) as refused:
        await account.delete(ctx, session)
    assert refused.value.extra["workflows"] == ["ada/classic"]


async def test_a_delete_removes_roles_first_then_the_account(ctx: Context, fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.MANAGER)
    session = await _signed_in(ctx, fake)

    await account.delete(ctx, session)

    operations = [call.operation for call in fake.calls]
    assert operations.index("revoke_role") < operations.index("delete_user")
    with pytest.raises(NotFound):
        await fake.identity.find_user(7)
