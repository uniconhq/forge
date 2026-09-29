"""Creating an org is a request answered at once and ten recorded steps the
poller runs; its name leaves room for its service account's; a failure at
any step is named and a rerun does not repeat the steps before it; the
service account's password is never written or logged; the setting closes
creation to everyone but the operator; and an org's admin, not its manager,
changes what the org says about itself.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from forge.domain.errors import Conflict, Forbidden, InvalidName, NotFound, Unavailable
from forge.domain.ids import OrgName
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.log import JsonFormatter
from forge.runtime.context import Context
from forge.runtime.setup import MAKERS, Setup
from forge.services import access, org_accounts, orgs, provisioning, sessions
from forge.services.provisioning import Record
from forge.settings import Settings
from forge.testing import APP_URL, CALLBACK_PATH, FORGE_URL, FakeClock

ACME = OrgName("acme")
STEPS = [
    "account_row",
    "org",
    "roles",
    "labels",
    "event_push",
    "first_admin",
    "service_account",
    "service_token",
    "ci_user",
    "ci_login",
]
OPERATIONS = [
    "create_org",
    "create_roles",
    "create_thread_labels",
    "create_event_push",
    "grant_role",
    "create_user",
    "ensure_account_membership",
    "mint_token",
    "create_ci_user",
    "mint_ci_token",
]


@pytest.fixture
async def ada(setup: Setup, fake: FakeForge) -> Session:
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
        )


async def _tick(setup: Setup) -> int:
    return await provisioning.poller(MAKERS).tick(setup.unit_of_work)


async def _record(setup: Setup, name: str) -> Record:
    async with setup.unit_of_work() as ctx:
        record = await provisioning.record_of(ctx, "org", name)
    assert record is not None
    return record


async def test_create_answers_at_once_and_the_poller_makes_the_org(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    record = await orgs.create(setup, ada, ACME, description="Acme")

    assert (record.status, record.kind, record.target_id) == ("pending", "org", "acme")
    assert [call.operation for call in fake.calls] == ["name_taken"]
    fake.reset_calls()

    assert await _tick(setup) == 1

    done = await _record(setup, "acme")
    assert (done.status, done.last_step, done.attempts, done.error) == (
        "ready",
        "ci_login",
        1,
        None,
    )
    assert (done.steps, done.failed_step, done.retry_at) == (tuple(STEPS), None, None)
    assert [call.operation for call in fake.calls] == OPERATIONS
    org = fake.state.orgs["acme"]
    assert org.roles_ready is True
    assert org.labels == {"announcement", "clarification", "answered"}
    assert org.event_push is not None
    assert org.event_push[0] == f"{APP_URL}/api/v1/events/forge/acme"
    assert len(org.event_push[1]) >= 32
    assert 7 in org.roles[(Scope("acme"), Role.ADMIN)]
    account = fake.state.user_named("unicon-ci-acme")
    assert account.id in org.account_members
    assert fake.state.ci_users == {"unicon-ci-acme": 1}
    async with setup.unit_of_work() as ctx:
        identity = await org_accounts.identity(ctx, ACME)
    assert identity.org == "acme"
    assert fake.state.tokens[identity.forge_token] == account.id
    assert fake.state.ci_tokens[identity.ci_token] == "unicon-ci-acme"
    assert await _tick(setup) == 0


async def test_the_status_follows_the_row_for_the_person_who_asked(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    async with setup.unit_of_work() as ctx:
        bob = await sessions.create(
            ctx, user=fake.users[8], credential=fake.mint(8), ip=None, user_agent=None
        )
    assert await orgs.status(setup, ada, ACME) is None
    await orgs.create(setup, ada, ACME, description="Acme")
    pending = await orgs.status(setup, ada, ACME)
    assert pending is not None
    assert pending.status == "pending"
    assert await orgs.status(setup, bob, ACME) is None
    await _tick(setup)
    ready = await orgs.status(setup, ada, ACME)
    assert ready is not None
    assert ready.status == "ready"


async def test_a_second_create_of_the_same_name_is_a_conflict(setup: Setup, ada: Session) -> None:
    await orgs.create(setup, ada, ACME, description="Acme")
    with pytest.raises(Conflict, match="being made"):
        await orgs.create(setup, ada, ACME, description="Again")
    await _tick(setup)
    with pytest.raises(Conflict, match="already exists"):
        await orgs.create(setup, ada, ACME, description="Again")


async def test_an_invalid_name_is_refused_before_anything_is_written(
    setup: Setup, ada: Session
) -> None:
    with pytest.raises(InvalidName):
        await orgs.create(setup, ada, OrgName("Acme!"), description="Acme")
    async with setup.unit_of_work() as ctx:
        assert await provisioning.record_of(ctx, "org", "Acme!") is None


async def test_an_org_name_leaves_room_for_its_service_accounts(setup: Setup, ada: Session) -> None:
    longest = OrgName("a" * 30)

    with pytest.raises(InvalidName, match="longer than 30"):
        await orgs.create(setup, ada, OrgName("a" * 31), description="Too long")
    await orgs.create(setup, ada, longest, description="Just right")
    await _tick(setup)

    assert (await _record(setup, longest)).status == "ready"


async def test_a_name_a_person_or_an_org_has_at_the_forge_is_refused_at_once(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    fake.add_user(30, "Taken")

    with pytest.raises(Conflict, match="taken at the forge"):
        await orgs.create(setup, ada, OrgName("taken"), description="Taken")
    with pytest.raises(Conflict, match="taken at the forge"):
        await orgs.create_by_operator(
            setup, OrgName("taken"), description="Taken", admin_username="ada"
        )
    async with setup.unit_of_work() as ctx:
        assert await provisioning.record_of(ctx, "org", "taken") is None
    assert fake.calls_to("create_org") == []


@pytest.fixture
async def closed_setup(
    migrated_database_url: str, fake: FakeForge, clock: FakeClock
) -> AsyncIterator[Setup]:
    settings = Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_URL,
        org_creation_open=False,
    )
    built = Setup.build(settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock)
    try:
        yield built
    finally:
        await built.stop()


async def test_with_creation_closed_only_the_operator_makes_an_org(
    closed_setup: Setup, fake: FakeForge
) -> None:
    async with closed_setup.unit_of_work() as ctx:
        session = await sessions.create(
            ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
        )

    with pytest.raises(Forbidden, match="closed on this deployment"):
        await orgs.create(closed_setup, session, ACME, description="Acme")
    assert fake.calls == []

    record = await orgs.create_by_operator(
        closed_setup, ACME, description="Acme", admin_username="Bob"
    )

    assert (record.status, record.last_step, record.attempts) == ("ready", "ci_login", 1)
    assert 8 in fake.state.orgs["acme"].roles[(Scope("acme"), Role.ADMIN)]
    async with closed_setup.unit_of_work() as ctx:
        assert (await org_accounts.identity(ctx, ACME)).org == "acme"


async def test_the_operator_must_name_a_user_the_forge_knows(setup: Setup) -> None:
    with pytest.raises(NotFound, match="no user named 'nobody'"):
        await orgs.create_by_operator(setup, ACME, description="Acme", admin_username="nobody")
    async with setup.unit_of_work() as ctx:
        assert await provisioning.record_of(ctx, "org", "acme") is None


async def test_an_operators_org_that_fails_halfway_is_left_for_the_poller(
    setup: Setup, fake: FakeForge, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fake.grading.create_ci_user

    async def broken(username: str) -> int:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(fake.grading, "create_ci_user", broken)
    failed = await orgs.create_by_operator(setup, ACME, description="Acme", admin_username="ada")
    assert (failed.status, failed.last_step, failed.failed_step) == (
        "failed",
        "service_token",
        "ci_user",
    )
    assert failed.error == "the forge or the CI did not answer"

    monkeypatch.setattr(fake.grading, "create_ci_user", original)
    await _tick(setup)

    assert (await _record(setup, "acme")).status == "ready"


@pytest.mark.parametrize(
    ("step", "area", "operation"),
    [
        ("org", "orgs", "create_org"),
        ("roles", "orgs", "create_roles"),
        ("labels", "orgs", "create_thread_labels"),
        ("event_push", "orgs", "create_event_push"),
        ("first_admin", "orgs", "grant_role"),
        ("service_account", "identity", "create_user"),
        ("service_token", "identity", "mint_token"),
        ("ci_user", "grading", "create_ci_user"),
        ("ci_login", "grading", "mint_ci_token"),
    ],
)
async def test_a_failure_at_any_step_is_named_and_a_rerun_does_not_repeat_earlier_steps(
    setup: Setup,
    fake: FakeForge,
    ada: Session,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    area: str,
    operation: str,
) -> None:
    target = getattr(fake, area)
    original = getattr(target, operation)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(target, operation, broken)
    await orgs.create(setup, ada, ACME, description="Acme")
    await _tick(setup)

    failed = await _record(setup, "acme")
    assert failed.status == "failed"
    assert failed.error == "the forge or the CI did not answer"
    assert failed.failed_step == step
    assert failed.last_step == STEPS[STEPS.index(step) - 1]
    assert failed.steps == tuple(STEPS)
    assert failed.retry_at is not None
    assert failed.attempts == 1

    monkeypatch.setattr(target, operation, original)
    fake.reset_calls()
    await _tick(setup)

    done = await _record(setup, "acme")
    assert (done.status, done.last_step, done.attempts, done.error) == (
        "ready",
        "ci_login",
        2,
        None,
    )
    later = [call.operation for call in fake.calls]
    assert operation in later
    earlier_steps = OPERATIONS[: OPERATIONS.index(operation)]
    repeated = [name for name in later if name in earlier_steps]
    if operation == "mint_token":
        assert repeated == ["ensure_account_membership"]
    else:
        assert repeated == []
    if step in ("service_token", "ci_login"):
        assert later[0] == "set_password"


async def test_the_service_accounts_password_is_never_written_or_logged(
    setup: Setup, fake: FakeForge, ada: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await orgs.create(setup, ada, ACME, description="Acme")
    await _tick(setup)
    account = fake.state.user_named("unicon-ci-acme")
    password = fake.state.passwords[account.id]
    assert len(password) >= 24

    formatter = JsonFormatter()
    written = "\n".join(formatter.format(record) for record in caplog.records)
    assert password not in written
    assert all(password not in str(call.arguments) for call in fake.calls)

    async with setup.unit_of_work() as ctx:
        identity = await org_accounts.identity(ctx, ACME)
        rows = (await ctx.db.execute(text("select * from org_accounts"))).mappings().all()
        jobs = (await ctx.db.execute(text("select * from provisioning"))).mappings().all()
    (row,) = rows
    for value in [*row.values(), *jobs[0].values()]:
        blob = value if isinstance(value, bytes) else str(value).encode()
        assert password.encode() not in blob
        assert identity.forge_token.encode() not in blob
        assert identity.ci_token.encode() not in blob
    assert row["username"] == "unicon-ci-acme"
    assert row["forge_user_id"] == account.id
    assert row["ci_user_id"] == 1


async def _organiser(
    ctx: Context, fake: FakeForge, user_id: int, scope: Scope, role: Role
) -> access.Organiser:
    session = await sessions.create(
        ctx, user=fake.users[user_id], credential=fake.mint(user_id), ip=None, user_agent=None
    )
    await ctx.db.commit()
    return await access.organiser(ctx, session, scope, role)


async def test_an_admin_updates_the_org_and_a_manager_is_refused(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(ACME, description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    await fake.orgs.grant_role(8, Scope("acme"), Role.MANAGER)
    admin = await _organiser(ctx, fake, 7, Scope("acme"), Role.ADMIN)
    manager = await _organiser(ctx, fake, 8, Scope("acme"), Role.MANAGER)

    await orgs.update(ctx, admin, ACME, description="Acme Corp", display_name="ACME")

    assert fake.state.orgs["acme"].description == "Acme Corp"
    assert fake.state.orgs["acme"].display_name == "ACME"
    with pytest.raises(Forbidden, match="admin role at acme"):
        await orgs.update(ctx, manager, ACME, description="Mine now")
    assert fake.state.orgs["acme"].description == "Acme Corp"


async def test_an_admin_of_one_contest_does_not_update_the_org(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(ACME, description="Acme")
    await fake.orgs.grant_role(7, Scope("acme", "spring"), Role.ADMIN)
    contest_admin = await _organiser(ctx, fake, 7, Scope("acme", "spring"), Role.ADMIN)

    with pytest.raises(Forbidden):
        await orgs.update(ctx, contest_admin, ACME, description="Mine now")


async def test_a_service_account_name_someone_else_took_is_refused_and_not_adopted(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    squatter = fake.add_user(40, "Unicon-CI-acme")
    fake.state.passwords[squatter.id] = "the squatter's own"
    await orgs.create(setup, ada, ACME, description="Acme")
    await _tick(setup)

    failed = await _record(setup, "acme")
    assert (failed.status, failed.last_step, failed.failed_step) == (
        "failed",
        "first_admin",
        "service_account",
    )
    assert failed.error == "the forge already holds something by this name"
    assert fake.state.passwords[squatter.id] == "the squatter's own"
    assert fake.calls_to("set_password") == []
    assert fake.calls_to("ensure_account_membership") == []


async def test_a_name_someone_took_between_the_check_and_the_step_is_refused(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    await orgs.create(setup, ada, ACME, description="Acme")
    fake.add_user(31, "acme")

    await _tick(setup)

    failed = await _record(setup, "acme")
    assert (failed.status, failed.failed_step) == ("failed", "org")
    assert failed.error == "the forge already holds something by this name"
    assert "acme" not in fake.state.orgs
    assert fake.calls_to("create_roles") == []


async def test_an_org_an_earlier_try_made_is_taken_as_made(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    await orgs.create(setup, ada, ACME, description="Acme")
    await fake.orgs.create_org(ACME, description="Acme")

    await _tick(setup)

    done = await _record(setup, "acme")
    assert done.status == "ready"
    assert [call.operation for call in fake.calls_to("platform_owns")] == ["platform_owns"]
