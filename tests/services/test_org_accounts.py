"""The nightly pass calls the CI as every org account and signs in again
the ones the CI no longer answers, records a failure without stopping,
and puts an account back in its place in its org; the account's identity
and event secret are read from its row.
"""

import logging

import pytest

from forge.domain.errors import NotFound, Unavailable
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import OrgName
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.runtime.context import Context
from forge.runtime.setup import MAKERS, Setup
from forge.services import org_accounts, orgs, provisioning, sessions
from forge.testing import FakeClock, logged

ACME = OrgName("acme")
BETA = OrgName("beta")


@pytest.fixture
async def provisioned(setup: Setup, fake: FakeForge) -> None:
    """Two orgs made through the poller, `acme` and `beta`."""
    async with setup.unit_of_work() as ctx:
        session: Session = await sessions.create(
            ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
        )
    await orgs.create(setup, session, ACME, description="Acme")
    await orgs.create(setup, session, BETA, description="Beta")
    assert await provisioning.poller(MAKERS).tick(setup.unit_of_work) == 2
    fake.reset_calls()


async def _identity(setup: Setup, org: OrgName) -> AsOrgAccount:
    async with setup.unit_of_work() as ctx:
        return await org_accounts.identity(ctx, org)


async def test_keepalive_calls_the_ci_as_each_account_and_records_when(
    setup: Setup, fake: FakeForge, provisioned: None, clock: FakeClock
) -> None:
    acme, beta = await _identity(setup, ACME), await _identity(setup, BETA)

    async with setup.unit_of_work() as ctx:
        alive = await org_accounts.keepalive(ctx)

    assert alive == 2
    assert [call.identity for call in fake.calls] == [acme, beta]
    assert [call.operation for call in fake.calls] == ["ci_user_is_alive"] * 2
    async with setup.unit_of_work() as ctx:
        rows = await org_accounts._rows(ctx)
        assert [(row.last_kept_alive_at, row.keepalive_error) for row in rows] == [
            (clock.now(), None),
            (clock.now(), None),
        ]


async def test_an_account_the_ci_no_longer_answers_is_signed_in_again(
    setup: Setup, fake: FakeForge, provisioned: None, caplog: pytest.LogCaptureFixture
) -> None:
    before = await _identity(setup, ACME)
    fake.ci_dead.add("unicon-ci-acme")

    async with setup.unit_of_work() as ctx:
        alive = await org_accounts.keepalive(ctx)

    assert alive == 2
    assert [call.operation for call in fake.calls] == [
        "ci_user_is_alive",
        "set_password",
        "mint_ci_token",
        "ci_user_is_alive",
    ]
    after = await _identity(setup, ACME)
    assert after.ci_token != before.ci_token
    assert after.forge_token == before.forge_token
    assert fake.state.ci_tokens[after.ci_token] == "unicon-ci-acme"
    assert "unicon-ci-acme" not in fake.ci_dead
    assert [record["org"] for record in logged(caplog, "org_accounts.signed_in_again")] == ["acme"]


async def test_a_keepalive_that_fails_is_recorded_and_the_pass_goes_on(
    setup: Setup,
    fake: FakeForge,
    provisioned: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(username: str, forge_password: str) -> str:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(fake.grading, "mint_ci_token", broken)
    fake.ci_dead.add("unicon-ci-acme")

    async with setup.unit_of_work() as ctx:
        alive = await org_accounts.keepalive(ctx)

    assert alive == 1
    async with setup.unit_of_work() as ctx:
        acme, beta = await org_accounts._rows(ctx)
        assert acme.keepalive_error == "Unavailable: the CI went away"
        assert acme.last_kept_alive_at is not None
        assert beta.keepalive_error is None
    (failure,) = logged(caplog, "org_accounts.keepalive_failed")
    assert (failure["org"], failure["error"]) == ("acme", "Unavailable")


async def test_the_nightly_pass_restores_a_missing_account_membership(
    setup: Setup, fake: FakeForge, provisioned: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    account = fake.state.user_named("unicon-ci-acme")
    fake.state.orgs["acme"].account_members.discard(account.id)
    (nightly,) = [timed for timed in setup._loops.passes if timed.name == "drift.nightly"]

    assert await nightly.tick(setup.unit_of_work) is True

    assert account.id in fake.state.orgs["acme"].account_members
    assert [record["org"] for record in logged(caplog, "org_accounts.membership_restored")] == [
        "acme"
    ]
    (summary,) = logged(caplog, "drift.nightly")
    assert (summary["membership_restored"], summary["accounts_alive"]) == (1, 2)


async def test_the_identity_and_the_event_secret_come_from_the_row(ctx: Context) -> None:
    with pytest.raises(NotFound):
        await org_accounts.identity(ctx, OrgName("nowhere"))
    with pytest.raises(NotFound):
        await org_accounts.event_secret_of(ctx, OrgName("nowhere"))

    await org_accounts.ensure_row(ctx, ACME)
    secret = await org_accounts.event_secret_of(ctx, ACME)

    assert len(secret) >= 32
    with pytest.raises(NotFound, match="not ready"):
        await org_accounts.identity(ctx, ACME)
    await org_accounts.ensure_row(ctx, ACME)
    assert await org_accounts.event_secret_of(ctx, ACME) == secret
