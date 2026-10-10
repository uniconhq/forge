"""`organiser` reads a person's roles once and applies inheritance: an org
admin reaches every scope below with what they hold, one org role is seen
at every contest and task inside, a contest manager reaches the contest's
tasks and not the org or a sibling contest, someone holding no role is
refused at every scope naming the scope and the role, and a credential the
forge refuses ends the session.
"""

import pytest

from forge.adapters.fakes import FakeForge
from forge.domain.errors import Forbidden, SessionExpired
from forge.domain.identity import User
from forge.domain.ids import OrgId
from forge.domain.roles import RANK, Role, RoleGrant, Scope
from forge.domain.sessions import Session
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import access, sessions


async def _session(ctx: Context, fake: FakeForge, user_id: int) -> Session:
    session = await sessions.create(
        ctx, user=fake.users[user_id], credential=fake.mint(user_id), ip=None, user_agent=None
    )
    await ctx.db.commit()
    return session


async def test_an_org_admin_is_an_organiser_at_every_scope_below(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    session = await _session(ctx, fake, 7)

    found = await access.organiser(ctx, session, Scope("acme", "spring", "sum"), Role.MANAGER)

    assert found.user == User(id=7, username="ada")
    assert found.grants == (RoleGrant(Scope("acme"), Role.ADMIN),)
    assert found.scope == Scope("acme", "spring", "sum")
    assert found.role is Role.MANAGER
    assert found.identity.user_id == 7
    assert found.identity.credential.access not in repr(found)
    assert len(fake.calls_to("roles_of")) == 1


async def test_a_contest_manager_reaches_its_tasks_and_not_the_org_or_a_sibling(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme", "spring"), Role.MANAGER)
    session = await _session(ctx, fake, 7)

    for task in ("sum", "product"):
        await access.organiser(ctx, session, Scope("acme", "spring", task), Role.MANAGER)
    await access.organiser(ctx, session, Scope("acme", "spring"), Role.OBSERVER)
    with pytest.raises(Forbidden, match="observer role at acme"):
        await access.organiser(ctx, session, Scope("acme"), Role.OBSERVER)
    with pytest.raises(Forbidden, match="admin role at acme/spring"):
        await access.organiser(ctx, session, Scope("acme", "spring"), Role.ADMIN)
    for scope in (Scope("acme", "autumn"), Scope("acme", "autumn", "sum")):
        with pytest.raises(Forbidden, match=f"observer role at {scope.name}"):
            await access.organiser(ctx, session, scope, Role.OBSERVER)


@pytest.mark.parametrize(
    "scope", [Scope("acme"), Scope("acme", "spring"), Scope("acme", "spring", "sum")]
)
async def test_someone_holding_no_role_is_refused_at_every_scope_naming_it(
    ctx: Context, fake: FakeForge, scope: Scope
) -> None:
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    session = await _session(ctx, fake, 8)

    with pytest.raises(Forbidden) as refused:
        await access.organiser(ctx, session, scope, Role.OBSERVER)
    assert refused.value.detail == f"This needs the observer role at {scope.name}."


async def test_a_credential_the_forge_refuses_ends_the_session(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    session = await _session(ctx, fake, 7)
    fake.state.revoke_credentials(7)

    with pytest.raises(SessionExpired):
        await access.organiser(setup, session, Scope("acme"), Role.OBSERVER)
    with pytest.raises(SessionExpired):
        await sessions.authenticate(ctx, session.id)


@pytest.mark.parametrize("held", list(Role))
async def test_one_org_role_is_seen_at_a_contest_and_a_task_inside_the_org(
    ctx: Context, fake: FakeForge, held: Role
) -> None:
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), held)
    session = await _session(ctx, fake, 7)

    for scope in (Scope("acme", "spring"), Scope("acme", "spring", "sum")):
        for role in Role:
            if RANK[role] <= RANK[held]:
                found = await access.organiser(ctx, session, scope, role)
                assert found.grants == (RoleGrant(Scope("acme"), held),)
            else:
                with pytest.raises(Forbidden, match=f"{role.value} role at {scope.name}"):
                    await access.organiser(ctx, session, scope, role)
