"""The rules on who holds a role. An organiser lists the holders of a scope
they observe and changes them at a scope they manage; only an admin grants
or takes away admin. A scope keeps an admin, counting admins of broader
scopes, so a hand-over is granting first and removing afterwards. Nobody is
an organiser and a contestant of one contest, in either direction. A service
account, known by its id, is never listed or changed. A role is changed only
at a contest or task that is there. Every refusal is made before anything is
written, and two changes at once in one org are made one after the other.
"""

import asyncio
import logging

import pytest
from sqlalchemy import select

from forge.adapters.git.fake import FakeForge
from forge.db.tables import Contestant, OrgAccount
from forge.domain.errors import ContestantConflict, Forbidden, NotFound, SoleAdmin
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgId
from forge.domain.names import ScopeNames
from forge.domain.roles import Role, RoleGrant, Scope
from forge.log import JsonFormatter
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import access, org_accounts, roles, sessions
from forge.services.access import Organiser
from forge.services.roles import Holder
from forge.testing import logged, name_places

ACME = Scope("acme")
SPRING = Scope("acme", "spring")
SUM = Scope("acme", "spring", "sum")
AUTUMN = Scope("acme", "autumn")


@pytest.fixture
async def acme(fake: FakeForge, setup: Setup) -> FakeForge:
    """The org acme with its contests spring and autumn and spring's task sum,
    and carol and dan as two more people besides ada and bob.
    """
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    await fake.orgs.create_roles(OrgId("acme"))
    spring = await fake.content.create_contest(OrgId("acme"), "spring", {})
    await fake.content.create_task(spring, "sum", {})
    await fake.content.create_contest(OrgId("acme"), "autumn", {})
    fake.add_user(9, "carol")
    fake.add_user(10, "dan")
    await name_places(setup, "acme", "acme/spring", "acme/spring/sum", "acme/autumn")
    fake.reset_calls()
    return fake


async def _organiser(
    ctx: Context, fake: FakeForge, user_id: int, scope: Scope, role: Role = Role.MANAGER
) -> Organiser:
    session = await sessions.create(
        ctx, user=fake.users[user_id], credential=fake.mint(user_id), ip=None, user_agent=None
    )
    await ctx.db.commit()
    return await access.organiser(ctx, session, scope, role)


async def _register(ctx: Context, user_id: int, contest: str, status: str) -> None:
    ctx.db.add(
        Contestant(contest_id=contest, user_id=user_id, status=status, registered_at=ctx.now)
    )
    await ctx.db.flush()


async def _service_account(ctx: Context, fake: FakeForge, org: str) -> int:
    """The org's service account, made at the fake and recorded in its row."""
    await org_accounts.ensure_row(ctx, OrgId(org))
    account = fake.add_user(20, f"unicon-ci-{org}")
    row = (await ctx.db.execute(select(OrgAccount).where(OrgAccount.org_id == org))).scalar_one()
    row.forge_user_id = account.id
    await ctx.db.flush()
    return account.id


def _writes(fake: FakeForge) -> list[tuple[str, int, Scope, Role]]:
    return [
        (call.operation, call.arguments["user_id"], call.arguments["scope"], call.arguments["role"])
        for call in fake.calls
        if call.operation in {"grant_role", "revoke_role"}
    ]


def _held(fake: FakeForge, user_id: int) -> set[RoleGrant]:
    org = fake.state.orgs["acme"]
    return {
        RoleGrant(scope, role) for (scope, role), members in org.roles.items() if user_id in members
    }


async def test_holders_are_everyone_with_a_role_there_each_once_at_their_highest(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(8, ACME, Role.OBSERVER)
    await acme.orgs.grant_role(8, SPRING, Role.MANAGER)
    await acme.orgs.grant_role(9, SPRING, Role.OBSERVER)
    await acme.orgs.grant_role(9, ACME, Role.OBSERVER)
    await acme.orgs.grant_role(10, AUTUMN, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 9, SPRING, Role.OBSERVER)

    found = await roles.holders(ctx, organiser, SPRING)

    assert found == (
        Holder(user=acme.users[7], role=Role.ADMIN, at=ACME, at_names=ScopeNames("acme")),
        Holder(
            user=acme.users[8],
            role=Role.MANAGER,
            at=SPRING,
            at_names=ScopeNames("acme", "spring"),
        ),
        Holder(user=acme.users[9], role=Role.OBSERVER, at=ACME, at_names=ScopeNames("acme")),
    )


async def test_a_service_account_is_left_out_and_a_lookalike_name_is_not(
    ctx: Context, acme: FakeForge
) -> None:
    account = await _service_account(ctx, acme, "acme")
    acme.add_user(11, "unicon-ci-other")
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(account, ACME, Role.OBSERVER)
    await acme.orgs.grant_role(11, ACME, Role.OBSERVER)
    organiser = await _organiser(ctx, acme, 7, ACME)

    found = await roles.holders(ctx, organiser, ACME)

    assert [holder.user.id for holder in found] == [7, 11]


async def test_listing_needs_the_observer_role_at_the_scope(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(8, AUTUMN, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 8, AUTUMN)

    with pytest.raises(Forbidden, match="observer role at acme/spring"):
        await roles.holders(ctx, organiser, SPRING)


@pytest.mark.parametrize("role", list(Role))
async def test_an_admin_grants_and_removes_each_role_at_their_scope(
    ctx: Context, acme: FakeForge, role: Role
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "bob", role)
    assert _held(acme, 8) == {RoleGrant(SPRING, role)}

    await roles.revoke(ctx, organiser, SPRING, 8)
    assert _held(acme, 8) == set()


async def test_an_org_admin_changes_roles_at_a_task_inside_the_org(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SUM)

    await roles.grant(ctx, organiser, SUM, "bob", Role.ADMIN)

    assert _held(acme, 8) == {RoleGrant(SUM, Role.ADMIN)}


@pytest.mark.parametrize("role", [Role.MANAGER, Role.OBSERVER])
async def test_a_manager_grants_and_removes_roles_below_admin(
    ctx: Context, acme: FakeForge, role: Role
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.MANAGER)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "bob", role)
    await roles.revoke(ctx, organiser, SPRING, 8)

    assert _writes(acme)[-2:] == [
        ("grant_role", 8, SPRING, role),
        ("revoke_role", 8, SPRING, role),
    ]


async def test_a_managers_grant_of_admin_is_refused_and_writes_nothing(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.MANAGER)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    with pytest.raises(Forbidden, match="Only an admin of acme/spring may grant admin"):
        await roles.grant(ctx, organiser, SPRING, "bob", Role.ADMIN)

    assert _writes(acme) == []


async def test_a_manager_may_not_remove_or_demote_an_admin(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.MANAGER)
    await acme.orgs.grant_role(8, SPRING, Role.ADMIN)
    await acme.orgs.grant_role(9, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    with pytest.raises(Forbidden, match="may remove an admin"):
        await roles.revoke(ctx, organiser, SPRING, 8)
    with pytest.raises(Forbidden, match="may demote an admin"):
        await roles.grant(ctx, organiser, SPRING, "bob", Role.OBSERVER)

    assert _writes(acme) == []
    assert _held(acme, 8) == {RoleGrant(SPRING, Role.ADMIN)}


async def test_an_observer_or_an_outsider_changes_nothing(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.OBSERVER)
    await acme.orgs.grant_role(8, AUTUMN, Role.ADMIN)
    await acme.orgs.grant_role(9, SPRING, Role.OBSERVER)
    observer = await _organiser(ctx, acme, 7, SPRING, Role.OBSERVER)
    elsewhere = await _organiser(ctx, acme, 8, AUTUMN)
    acme.reset_calls()

    for organiser in (observer, elsewhere):
        with pytest.raises(Forbidden, match="manager role at acme/spring"):
            await roles.grant(ctx, organiser, SPRING, "dan", Role.OBSERVER)
        with pytest.raises(Forbidden, match="manager role at acme/spring"):
            await roles.revoke(ctx, organiser, SPRING, 9)

    assert _writes(acme) == []


async def test_granting_moves_a_holder_granting_first_and_revoking_afterwards(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    await acme.orgs.grant_role(8, SPRING, Role.OBSERVER)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    await roles.grant(ctx, organiser, SPRING, "bob", Role.MANAGER)

    assert _writes(acme) == [
        ("grant_role", 8, SPRING, Role.MANAGER),
        ("revoke_role", 8, SPRING, Role.OBSERVER),
    ]
    assert _held(acme, 8) == {RoleGrant(SPRING, Role.MANAGER)}


async def test_granting_a_role_already_held_writes_nothing(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    await acme.orgs.grant_role(8, SPRING, Role.MANAGER)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    await roles.grant(ctx, organiser, SPRING, "bob", Role.MANAGER)

    assert _writes(acme) == []


async def test_a_role_held_elsewhere_is_left_alone_by_a_revoke(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(8, ACME, Role.OBSERVER)
    await acme.orgs.grant_role(8, SPRING, Role.MANAGER)
    await acme.orgs.grant_role(8, SUM, Role.MANAGER)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.revoke(ctx, organiser, SPRING, 8)

    assert _held(acme, 8) == {RoleGrant(ACME, Role.OBSERVER), RoleGrant(SUM, Role.MANAGER)}


async def test_revoking_someone_with_no_role_there_writes_nothing(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    await roles.revoke(ctx, organiser, SPRING, 8)

    assert _writes(acme) == []


async def test_an_unknown_user_is_not_found_by_name_or_id(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    with pytest.raises(NotFound, match="no user named 'nobody'"):
        await roles.grant(ctx, organiser, SPRING, "nobody", Role.OBSERVER)
    with pytest.raises(NotFound):
        await roles.revoke(ctx, organiser, SPRING, 99)
    assert _writes(acme) == []


async def test_a_service_account_is_neither_granted_nor_revoked(
    ctx: Context, acme: FakeForge
) -> None:
    account = await _service_account(ctx, acme, "acme")
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(account, ACME, Role.OBSERVER)
    organiser = await _organiser(ctx, acme, 7, ACME)
    acme.reset_calls()

    with pytest.raises(Forbidden, match="service account"):
        await roles.grant(ctx, organiser, ACME, "unicon-ci-acme", Role.MANAGER)
    with pytest.raises(Forbidden, match="service account"):
        await roles.revoke(ctx, organiser, ACME, account)

    assert _writes(acme) == []


async def test_a_person_whose_name_looks_like_a_service_account_is_changed_as_anyone(
    ctx: Context, acme: FakeForge
) -> None:
    acme.add_user(11, "unicon-ci-acme")
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, ACME)

    await roles.grant(ctx, organiser, ACME, "unicon-ci-acme", Role.MANAGER)
    await roles.revoke(ctx, organiser, ACME, 11)

    assert _held(acme, 11) == set()


async def test_each_change_is_logged_with_ids_scope_and_role(
    ctx: Context, acme: FakeForge, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "bob", Role.OBSERVER)
    await roles.grant(ctx, organiser, SPRING, "bob", Role.MANAGER)
    await roles.revoke(ctx, organiser, SPRING, 8)

    granted = logged(caplog, "roles.granted")
    revoked = logged(caplog, "roles.revoked")
    assert [(r["user_id"], r["by_user_id"], r["scope"], r["role"]) for r in granted] == [
        (8, 7, "acme/spring", "observer"),
        (8, 7, "acme/spring", "manager"),
    ]
    assert [(r["user_id"], r["scope"], r["role"]) for r in revoked] == [
        (8, "acme/spring", "observer"),
        (8, "acme/spring", "manager"),
    ]
    written = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert organiser.identity.credential.access not in written


async def test_the_last_admin_of_an_org_may_not_step_down(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, ACME)
    acme.reset_calls()

    with pytest.raises(SoleAdmin) as removed:
        await roles.revoke(ctx, organiser, ACME, 7)
    with pytest.raises(SoleAdmin) as demoted:
        await roles.grant(ctx, organiser, ACME, "ada", Role.MANAGER)

    for refused in (removed, demoted):
        assert refused.value.code == "sole_admin"
        assert refused.value.extra["scopes"] == [{"kind": "org", "name": "acme"}]
    assert _writes(acme) == []
    assert _held(acme, 7) == {RoleGrant(ACME, Role.ADMIN)}


async def test_the_last_admin_of_a_task_is_refused_naming_the_task(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SUM, Role.ADMIN)
    await acme.orgs.grant_role(8, AUTUMN, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SUM)

    with pytest.raises(SoleAdmin) as refused:
        await roles.revoke(ctx, organiser, SUM, 7)

    assert refused.value.extra["scopes"] == [{"kind": "task", "name": "acme/spring/sum"}]


async def test_a_contests_only_admin_may_step_down_while_an_org_admin_stands(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    await acme.orgs.grant_role(8, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "ada", Role.OBSERVER)

    assert _held(acme, 7) == {RoleGrant(SPRING, Role.OBSERVER)}


async def test_the_same_removal_succeeds_once_a_second_admin_exists(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)
    with pytest.raises(SoleAdmin):
        await roles.revoke(ctx, organiser, SPRING, 7)

    await acme.orgs.grant_role(8, SPRING, Role.ADMIN)
    await roles.revoke(ctx, organiser, SPRING, 7)

    assert _held(acme, 7) == set()


async def test_an_admin_of_a_broader_scope_counts_themself(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.revoke(ctx, organiser, SPRING, 7)

    assert _held(acme, 7) == {RoleGrant(ACME, Role.ADMIN)}


async def test_two_admins_stepping_down_at_once_leave_one_of_them(
    setup: Setup, acme: FakeForge, monkeypatch: pytest.MonkeyPatch
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(8, ACME, Role.ADMIN)
    async with setup.unit_of_work() as ctx:
        ada = await _organiser(ctx, acme, 7, ACME)
        bob = await _organiser(ctx, acme, 8, ACME)
    revoke_role = acme.orgs.revoke_role

    async def slow_revoke(user_id: int, scope: Scope, role: Role) -> None:
        await asyncio.sleep(0.3)
        await revoke_role(user_id, scope, role)

    monkeypatch.setattr(acme.orgs, "revoke_role", slow_revoke)
    outcomes = await asyncio.gather(
        roles.revoke(setup, ada, ACME, 7),
        roles.revoke(setup, bob, ACME, 8),
        return_exceptions=True,
    )

    assert sorted(type(outcome).__name__ for outcome in outcomes) == ["NoneType", "SoleAdmin"]
    assert len(acme.state.orgs["acme"].roles[(ACME, Role.ADMIN)]) == 1


async def test_a_service_account_holding_admin_does_not_keep_a_scope_covered(
    ctx: Context, acme: FakeForge
) -> None:
    account = await _service_account(ctx, acme, "acme")
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(account, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, ACME)

    with pytest.raises(SoleAdmin):
        await roles.revoke(ctx, organiser, ACME, 7)


async def test_a_hand_over_is_granting_the_new_admin_then_removing_the_old(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "bob", Role.ADMIN)
    await roles.revoke(ctx, organiser, SPRING, 7)

    assert _held(acme, 8) == {RoleGrant(SPRING, Role.ADMIN)}
    assert _held(acme, 7) == set()


async def test_a_hand_over_the_other_way_round_is_refused(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, SPRING, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    with pytest.raises(SoleAdmin):
        await roles.revoke(ctx, organiser, SPRING, 7)
    with pytest.raises(SoleAdmin):
        await roles.grant(ctx, organiser, SPRING, "ada", Role.MANAGER)

    assert _held(acme, 7) == {RoleGrant(SPRING, Role.ADMIN)}


@pytest.mark.parametrize("status", ["pending", "approved"])
@pytest.mark.parametrize("scope", [SPRING, SUM, ACME])
async def test_a_contestant_is_given_no_role_in_their_contest_or_its_org(
    ctx: Context, acme: FakeForge, scope: Scope, status: str
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await _register(ctx, 8, "acme/spring", status)
    organiser = await _organiser(ctx, acme, 7, scope)
    acme.reset_calls()

    with pytest.raises(ContestantConflict) as refused:
        await roles.grant(ctx, organiser, scope, "bob", Role.OBSERVER)

    assert refused.value.code == "contestant_conflict"
    assert refused.value.extra["contests"] == ["acme/spring"]
    assert _writes(acme) == []


async def test_a_contestant_moving_roles_is_refused_before_either_write(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await acme.orgs.grant_role(8, SPRING, Role.OBSERVER)
    await _register(ctx, 8, "acme/spring", "pending")
    organiser = await _organiser(ctx, acme, 7, SPRING)
    acme.reset_calls()

    with pytest.raises(ContestantConflict):
        await roles.grant(ctx, organiser, SPRING, "bob", Role.MANAGER)

    assert _writes(acme) == []


@pytest.mark.parametrize("status", ["rejected", "withdrawn", "removed"])
async def test_someone_no_longer_registered_may_be_given_a_role(
    ctx: Context, acme: FakeForge, status: str
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await _register(ctx, 8, "acme/spring", status)
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "bob", Role.OBSERVER)

    assert _held(acme, 8) == {RoleGrant(SPRING, Role.OBSERVER)}


async def test_a_contestant_elsewhere_may_be_given_a_role(ctx: Context, acme: FakeForge) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    await _register(ctx, 8, "acme/autumn", "approved")
    await _register(ctx, 8, "acme_x/spring", "approved")
    await _register(ctx, 8, "acmex/spring", "approved")
    organiser = await _organiser(ctx, acme, 7, SPRING)

    await roles.grant(ctx, organiser, SPRING, "bob", Role.OBSERVER)
    with pytest.raises(ContestantConflict) as refused:
        await roles.grant(ctx, organiser, ACME, "bob", Role.OBSERVER)

    assert refused.value.extra["contests"] == ["acme/autumn"]


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (ACME, True),
        (SPRING, True),
        (SUM, True),
        (AUTUMN, False),
        (Scope("other"), False),
        (Scope("other", "spring"), False),
    ],
)
async def test_a_role_at_the_contest_its_tasks_or_its_org_stands_in_the_way_of_registering(
    ctx: Context, acme: FakeForge, scope: Scope, expected: bool
) -> None:
    await acme.orgs.create_org(OrgId("other"), description="Other")
    await acme.orgs.grant_role(8, scope, Role.OBSERVER)

    assert await roles.holds_role_in_contest(ctx, 8, ContestId("acme/spring")) is expected
    assert await roles.holds_role_in_contest(ctx, 9, ContestId("acme/spring")) is False
    assert acme.calls_to("roles_of_user")[0].arguments == {"user_id": 8}


@pytest.mark.parametrize(
    "scope", [Scope("acme", "winter"), Scope("acme", "spring", "product")], ids=["contest", "task"]
)
async def test_a_role_at_a_contest_or_task_that_is_not_there_is_refused_writing_nothing(
    ctx: Context, acme: FakeForge, scope: Scope
) -> None:
    await acme.orgs.grant_role(7, ACME, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 7, ACME)
    acme.reset_calls()

    with pytest.raises(NotFound, match=f"There is no {scope.kind.value} {scope.name}"):
        await roles.grant(ctx, organiser, scope, "bob", Role.MANAGER)
    with pytest.raises(NotFound, match=f"There is no {scope.kind.value} {scope.name}"):
        await roles.revoke(ctx, organiser, scope, 8)

    assert _writes(acme) == []
    assert [call.identity for call in acme.calls_to("exists")] == [PLATFORM, PLATFORM]


async def test_an_outsider_is_refused_before_the_scope_is_looked_up(
    ctx: Context, acme: FakeForge
) -> None:
    await acme.orgs.grant_role(8, AUTUMN, Role.ADMIN)
    organiser = await _organiser(ctx, acme, 8, AUTUMN)
    acme.reset_calls()

    with pytest.raises(Forbidden):
        await roles.grant(ctx, organiser, Scope("acme", "winter"), "ada", Role.OBSERVER)
    assert acme.calls_to("exists") == []
