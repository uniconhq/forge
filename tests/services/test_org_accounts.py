"""An org account's identity is read from its row, its state at the CI
refreshed first once the CI's implementation says so (the fake, like
Woodpecker, once its last sign-in is older than two thirds of the session's
hard lifetime, 20 days by default), by one caller at a time; a state the CI
refused is refreshed once however many callers were refused; the account's
event secret is read from its row too.
"""

import asyncio
import logging
from datetime import timedelta

import pytest

from forge.adapters.git.fake import FakeForge
from forge.adapters.git.fake.grading import token_in
from forge.domain.errors import NotFound, Unavailable
from forge.domain.identity import AsOrgAccount, CiState
from forge.domain.ids import OrgId
from forge.domain.sessions import Session
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import org_accounts, orgs, sessions
from forge.testing import FakeClock, logged

ACME = OrgId("acme")


@pytest.fixture
async def made(setup: Setup, fake: FakeForge) -> None:
    """The org `acme`, made."""
    async with setup.unit_of_work() as ctx:
        session: Session = await sessions.create(
            ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
        )
    await orgs.create(setup, session, ACME, description="Acme")
    fake.reset_calls()


async def _identity(setup: Setup, org: OrgId) -> AsOrgAccount:
    async with setup.unit_of_work() as ctx:
        return await org_accounts.identity(ctx, org)


async def test_a_fresh_sign_in_is_handed_out_as_it_is(
    setup: Setup, fake: FakeForge, made: None, clock: FakeClock
) -> None:
    first = await _identity(setup, ACME)
    clock.advance(timedelta(days=20))

    assert await _identity(setup, ACME) == first
    assert fake.calls == []


async def test_an_account_last_signed_in_long_ago_signs_in_again_first(
    setup: Setup, fake: FakeForge, made: None, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = await _identity(setup, ACME)
    clock.advance(timedelta(days=21))

    after = await _identity(setup, ACME)

    assert [call.operation for call in fake.calls] == ["refresh"]
    assert token_in(after.ci_state) != token_in(before.ci_state)
    assert after.forge_token == before.forge_token
    assert fake.state.ci_tokens[token_in(after.ci_state)] == "unicon-ci-acme"
    assert [record["org"] for record in logged(caplog, "org_accounts.signed_in_again")] == ["acme"]
    fake.reset_calls()
    assert await _identity(setup, ACME) == after
    assert fake.calls == []


async def test_two_callers_at_once_sign_the_account_in_once(
    setup: Setup, fake: FakeForge, made: None, clock: FakeClock
) -> None:
    clock.advance(timedelta(days=21))

    first, second = await asyncio.gather(_identity(setup, ACME), _identity(setup, ACME))

    assert first == second
    assert len(fake.calls_to("refresh")) == 1


async def test_a_sign_in_that_fails_fails_the_caller_and_is_tried_again_next_time(
    setup: Setup,
    fake: FakeForge,
    made: None,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake.grading.refresh

    async def broken(org: OrgId, state: CiState) -> CiState:
        raise Unavailable("the CI went away")

    clock.advance(timedelta(days=21))
    monkeypatch.setattr(fake.grading, "refresh", broken)
    with pytest.raises(Unavailable):
        await _identity(setup, ACME)

    monkeypatch.setattr(fake.grading, "refresh", original)
    await _identity(setup, ACME)
    assert len(fake.calls_to("refresh")) == 1


async def test_the_identity_and_the_event_secret_come_from_the_row(ctx: Context) -> None:
    with pytest.raises(NotFound):
        await org_accounts.identity(ctx, OrgId("nowhere"))
    with pytest.raises(NotFound):
        await org_accounts.event_secret_of(ctx, OrgId("nowhere"))

    await org_accounts.ensure_row(ctx, ACME)
    secret = await org_accounts.event_secret_of(ctx, ACME)

    assert len(secret) >= 32
    with pytest.raises(NotFound, match="not ready"):
        await org_accounts.identity(ctx, ACME)
    await org_accounts.ensure_row(ctx, ACME)
    assert await org_accounts.event_secret_of(ctx, ACME) == secret


async def test_a_shorter_session_lifetime_signs_the_account_in_sooner(
    setup: Setup,
    fake: FakeForge,
    made: None,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fake.grading, "login_lifetime", timedelta(days=6))
    clock.advance(timedelta(days=4, seconds=1))

    await _identity(setup, ACME)

    assert [call.operation for call in fake.calls] == ["refresh"]


async def test_a_refused_credential_is_renewed_once_for_every_caller_it_failed(
    setup: Setup, fake: FakeForge, made: None
) -> None:
    refused = await _identity(setup, ACME)
    fake.state.revoked_ci_tokens.add(token_in(refused.ci_state))

    async def renewed() -> AsOrgAccount:
        async with setup.unit_of_work() as ctx:
            return await org_accounts.renew(ctx, refused)

    first, second = await asyncio.gather(renewed(), renewed())

    assert first == second
    assert first.ci_state != refused.ci_state
    assert len(fake.calls_to("refresh")) == 1
    assert await _identity(setup, ACME) == first
