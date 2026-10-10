"""Creating an org makes everything it needs before the request answers; its
name leaves room for its service account's; a failure at any step fails the
request, records nothing, removes what the earlier steps made at the forge
and the CI, and leaves the name free for the next try, and so does a commit
that fails after every step; a removal that fails is logged and the step's
own error is raised; the service account's password is never written or
logged; the setting closes creation to everyone but the operator; and an
org's admin, not its manager, changes what the org says about itself.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.errors import (
    Conflict,
    Forbidden,
    InvalidName,
    NotFound,
    Rejected,
    Unavailable,
)
from forge.domain.ids import OrgId
from forge.domain.keys import key_from_name
from forge.domain.names import Named, OrgProfile
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.forges.fake.grading import token_in
from forge.log import JsonFormatter
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import access, making, names, org_accounts, orgs, sessions
from forge.settings import Settings
from forge.testing import APP_URL, CALLBACK_PATH, FORGE_URL, FakeClock, logged
from tests.services.conftest import forge_state

ACME = OrgId("acme")
STEPS = [
    ("orgs", "create_org"),
    ("orgs", "create_roles"),
    ("orgs", "create_thread_labels"),
    ("orgs", "create_event_push"),
    ("orgs", "grant_role"),
    ("identity", "create_user"),
    ("orgs", "ensure_account_membership"),
    ("identity", "mint_token"),
    ("grading", "set_up_org"),
]
OPERATIONS = [operation for _, operation in STEPS]
REMOVALS = ["tear_down_org", "remove_account_membership", "delete_user", "delete_org"]


@pytest.fixture
async def ada(setup: Setup, fake: FakeForge) -> Session:
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
        )


async def _nothing_recorded(setup: Setup, name: str) -> None:
    async with setup.unit_of_work() as ctx:
        assert await names.org_id(ctx, name) is None
        assert await org_accounts.org_ids(ctx) == ()


async def test_create_makes_the_org_before_it_answers(
    setup: Setup, fake: FakeForge, ada: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    made = await orgs.create(setup, ada, ACME, description="Acme")

    assert made == Named(ACME, "acme")
    assert [call.operation for call in fake.calls] == ["name_taken", *OPERATIONS]
    assert logged(caplog, "orgs.undone") == []
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
    assert fake.state.ci_tokens[token_in(identity.ci_state)] == "unicon-ci-acme"


async def test_a_second_create_of_the_same_name_is_a_conflict(setup: Setup, ada: Session) -> None:
    await orgs.create(setup, ada, ACME, description="Acme")
    with pytest.raises(Conflict, match="'acme' is taken"):
        await orgs.create(setup, ada, ACME, description="Again")


async def test_an_invalid_name_is_refused_before_anything_is_written(
    setup: Setup, ada: Session
) -> None:
    with pytest.raises(InvalidName):
        await orgs.create(setup, ada, OrgId("Acme!"), description="Acme")
    await _nothing_recorded(setup, "Acme!")


async def test_an_org_name_is_at_most_40_characters(setup: Setup, ada: Session) -> None:
    longest = OrgId("a" * 40)

    with pytest.raises(InvalidName, match="longer than 40"):
        await orgs.create(setup, ada, OrgId("a" * 41), description="Too long")
    made = await orgs.create(setup, ada, longest, description="Just right")

    assert made.name == longest


async def test_a_name_a_person_or_an_org_has_at_the_forge_is_refused_at_once(
    setup: Setup, fake: FakeForge, ada: Session
) -> None:
    fake.add_user(30, "Taken")

    with pytest.raises(Conflict, match="taken at the forge"):
        await orgs.create(setup, ada, OrgId("taken"), description="Taken")
    with pytest.raises(Conflict, match="taken at the forge"):
        await orgs.create_by_operator(
            setup, OrgId("taken"), description="Taken", admin_username="ada"
        )
    await _nothing_recorded(setup, "taken")
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
    built = Setup.build(
        settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock, keys=key_from_name
    )
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

    made = await orgs.create_by_operator(
        closed_setup, ACME, description="Acme", admin_username="Bob"
    )

    assert made == Named(ACME, "acme")
    assert 8 in fake.state.orgs["acme"].roles[(Scope("acme"), Role.ADMIN)]
    async with closed_setup.unit_of_work() as ctx:
        assert (await org_accounts.identity(ctx, ACME)).org == "acme"


async def test_the_operator_must_name_a_user_the_forge_knows(setup: Setup) -> None:
    with pytest.raises(NotFound, match="no user named 'nobody'"):
        await orgs.create_by_operator(setup, ACME, description="Acme", admin_username="nobody")
    await _nothing_recorded(setup, "acme")


@pytest.mark.parametrize(("area", "operation"), STEPS)
async def test_a_failure_at_any_step_removes_what_the_earlier_ones_made_and_the_next_try_works(
    setup: Setup,
    fake: FakeForge,
    ada: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    area: str,
    operation: str,
) -> None:
    caplog.set_level(logging.INFO)
    target = getattr(fake, area)
    original = getattr(target, operation)
    before = forge_state(fake)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(target, operation, broken)
    with pytest.raises(Unavailable) as failed:
        await orgs.create(setup, ada, ACME, description="Acme")

    assert failed.value.detail == making.NO_ANSWER
    assert forge_state(fake) == before
    await _nothing_recorded(setup, "acme")
    assert logged(caplog, "orgs.undo_left") == []
    assert len(logged(caplog, "orgs.undone")) == (0 if operation == "create_org" else 1)

    monkeypatch.setattr(target, operation, original)
    made = await orgs.create(setup, ada, ACME, description="Acme")

    assert made == Named(ACME, "acme")
    async with setup.unit_of_work() as ctx:
        assert (await org_accounts.identity(ctx, ACME)).org == "acme"


async def test_the_org_is_undone_in_the_reverse_of_the_order_it_was_made(
    setup: Setup, fake: FakeForge, ada: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the CI went away")

    # The sign-in fails after the account's user at the CI was made, so the
    # set-up stops partway and is torn down whole.
    monkeypatch.setattr(fake.grading, "_sign_in", broken)
    with pytest.raises(Unavailable):
        await orgs.create(setup, ada, ACME, description="Acme")

    assert fake.calls_to("create_user")
    assert [call.operation for call in fake.calls if call.operation in REMOVALS] == REMOVALS
    assert fake.calls_to("tear_down_org")[0].arguments == {"org": "acme"}
    assert fake.state.ci_users == {}
    (deleted,) = fake.calls_to("delete_user")
    assert deleted.arguments["user_id"] not in fake.state.users
    assert fake.calls_to("delete_org")[0].arguments == {"name": "acme"}


async def test_an_error_that_is_not_the_forges_is_raised_as_it_is_and_still_undoes(
    setup: Setup, fake: FakeForge, ada: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = forge_state(fake)
    bug = RuntimeError("a bug halfway")

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise bug

    monkeypatch.setattr(fake.identity, "mint_token", broken)
    with pytest.raises(RuntimeError) as failed:
        await orgs.create(setup, ada, ACME, description="Acme")

    assert failed.value is bug
    assert forge_state(fake) == before


async def test_a_removal_that_fails_is_logged_and_the_steps_own_error_is_raised(
    setup: Setup,
    fake: FakeForge,
    ada: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise Rejected("the CI said no")

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(fake.grading, "_sign_in", refused)
    monkeypatch.setattr(fake.orgs, "delete_org", down)
    with pytest.raises(Rejected) as failed:
        await orgs.create(setup, ada, ACME, description="Acme")

    assert failed.value.detail == making.SAID[Rejected]
    (left,) = logged(caplog, "orgs.undo_left")
    assert (left["level"], left["kind"], left["key"], left["org"], left["error"]) == (
        "WARNING",
        "org",
        "acme",
        "acme",
        "Unavailable",
    )
    assert logged(caplog, "orgs.undone") == []
    assert "acme" in fake.state.orgs
    assert all(user.username != "unicon-ci-acme" for user in fake.state.users.values())
    assert fake.state.ci_users == {}
    await _nothing_recorded(setup, "acme")


async def test_a_ci_set_up_that_cannot_be_undone_is_logged_by_the_accounts_name(
    setup: Setup,
    fake: FakeForge,
    ada: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise Rejected("the CI said no")

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(fake.grading, "_sign_in", refused)
    monkeypatch.setattr(fake.grading, "tear_down_org", down)
    with pytest.raises(Rejected):
        await orgs.create(setup, ada, ACME, description="Acme")

    (left,) = logged(caplog, "orgs.undo_left")
    assert (left["kind"], left["key"]) == ("ci_user", "unicon-ci-acme")


async def test_a_commit_that_fails_after_every_step_removes_what_they_made(
    setup: Setup, fake: FakeForge, ada: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = forge_state(fake)

    async def refuse(self: AsyncSession) -> None:
        raise RuntimeError("the database went away at commit")

    monkeypatch.setattr(AsyncSession, "commit", refuse)
    with pytest.raises(RuntimeError, match="went away at commit"):
        await orgs.create(setup, ada, ACME, description="Acme")
    monkeypatch.undo()

    assert fake.calls_to("set_up_org")
    assert forge_state(fake) == before
    await _nothing_recorded(setup, "acme")


async def test_the_service_accounts_password_is_never_written_or_logged(
    setup: Setup, fake: FakeForge, ada: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await orgs.create(setup, ada, ACME, description="Acme")
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
    (row,) = rows
    for value in row.values():
        blob = value if isinstance(value, bytes) else str(value).encode()
        assert password.encode() not in blob
        assert identity.forge_token.encode() not in blob
        assert identity.ci_state.encode() not in blob
        assert token_in(identity.ci_state).encode() not in blob
    assert row["username"] == "unicon-ci-acme"
    assert row["forge_user_id"] == account.id
    assert fake.state.ci_users == {"unicon-ci-acme": 1}


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


async def test_any_organiser_in_the_org_reads_its_own_fields_and_no_one_else(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(ACME, description="Acme")
    await fake.orgs.grant_role(7, Scope("acme"), Role.ADMIN)
    await fake.orgs.grant_role(8, Scope("acme", "spring", "sum"), Role.OBSERVER)
    admin = await _organiser(ctx, fake, 7, Scope("acme"), Role.ADMIN)
    of_one_task = await _organiser(ctx, fake, 8, Scope("acme", "spring", "sum"), Role.OBSERVER)

    assert await orgs.read(ctx, admin, ACME) == OrgProfile(display_name=None, description="Acme")
    await orgs.update(ctx, admin, ACME, description="Acme Corp", display_name="ACME")

    expected = OrgProfile(display_name="ACME", description="Acme Corp")
    assert await orgs.read(ctx, of_one_task, ACME) == expected
    with pytest.raises(Forbidden):
        await orgs.read(ctx, admin, OrgId("globex"))


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

    with pytest.raises(Conflict):
        await orgs.create(setup, ada, ACME, description="Acme")

    await _nothing_recorded(setup, "acme")
    assert fake.state.users[squatter.id] == squatter
    assert fake.state.passwords[squatter.id] == "the squatter's own"
    assert fake.calls_to("set_password") == []
    assert fake.calls_to("ensure_account_membership") == []
    assert fake.calls_to("delete_user") == []
    assert "acme" not in fake.state.orgs


async def test_a_name_someone_took_between_the_check_and_the_step_is_refused(
    setup: Setup, fake: FakeForge, ada: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def taken_meanwhile(name: str) -> bool:
        fake.add_user(31, "acme")
        return False

    monkeypatch.setattr(fake.orgs, "name_taken", taken_meanwhile)

    with pytest.raises(Conflict):
        await orgs.create(setup, ada, ACME, description="Acme")

    assert "acme" not in fake.state.orgs
    assert fake.calls_to("create_roles") == []
