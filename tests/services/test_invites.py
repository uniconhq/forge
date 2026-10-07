"""An organiser invites someone by username or by email address, to a role at a
scope or a contestant's place in a contest. The invite's mail goes out once
the request has committed and never holds it up; a mail that fails leaves
the invite made, and sending it again makes a new token. The person acts on
their own invites only: an address is matched against the ones the forge
has confirmed for them, and accepting grants the role, or the eligibility
an invite-only or hidden contest asks for. A lapsed invite does nothing.
"""

from datetime import timedelta

import pytest

from forge.db.tables import Contestant
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import (
    AlreadyInvited,
    Forbidden,
    InvalidInvite,
    InviteExpired,
    InviteLimit,
    InviteRequired,
    NotFound,
    Unavailable,
    WrongStatus,
)
from forge.domain.ids import ContestId
from forge.domain.invites import Grant, InviteStatus, MailStatus
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import contestants, invites, names, release
from forge.services.access import Organiser
from forge.testing import APP_URL, FakeClock
from tests.services.conftest import RUNNING, Acme, organiser, signed_in, write_contest

CONTEST = Scope("acme", "spring")

INVITE_ONLY = """name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: {visibility}
registration:
  invite_only: true
"""


@pytest.fixture
async def people(acme: Acme, spring: ContestId) -> Acme:
    """carol (20), with a confirmed address, beside ada and bob."""
    acme.fake.add_user(20, "carol", email="carol@example.test")
    return acme


async def _manager(setup: Setup, acme: Acme, scope: Scope = CONTEST) -> Organiser:
    return await organiser(setup, acme.fake, 7, scope, Role.MANAGER)


def _token(acme: Acme) -> str:
    """The token in the last mail sent."""
    text = acme.fake.mail.sent[-1].text
    return text.split("/invites#", 1)[1].split()[0]


async def test_an_invite_by_username_is_mailed_after_the_request_and_accepting_grants_the_role(
    setup: Setup, people: Acme
) -> None:
    acme = people
    manager = await _manager(setup, acme)

    made = await invites.create(setup, manager, CONTEST, Grant.MANAGER, username="carol")

    assert (made.username, made.email, made.status, made.mail_status) == (
        "carol",
        None,
        InviteStatus.PENDING,
        MailStatus.WAITING,
    )
    assert made.where.path == "acme/spring"
    await setup.settle()
    [mail] = acme.fake.mail.sent
    assert mail.to == "carol@example.test"
    assert "ada has invited you to be a manager of acme/spring" in mail.text
    assert f"{APP_URL}/invites#" in mail.text
    [listed] = await invites.at(setup, manager, CONTEST)
    assert (listed.id, listed.mail_status) == (made.id, MailStatus.SENT)

    carol = await signed_in(setup, acme.fake, 20)
    [mine] = await invites.mine(setup, carol)
    assert mine.id == made.id
    assert (await invites.by_token(setup, carol, _token(acme))).id == made.id
    accepted = await invites.accept(setup, carol, made.id)

    assert accepted.status is InviteStatus.ACCEPTED
    grants = await acme.fake.orgs.roles_of_user(20)
    assert RoleGrant(CONTEST, Role.MANAGER) in grants
    assert await invites.mine(setup, carol) == ()


async def test_an_invite_by_email_waits_for_the_account_and_lets_it_register_invite_only(
    setup: Setup, people: Acme, clock: FakeClock
) -> None:
    acme = people
    await write_contest(acme.fake, INVITE_ONLY.format(visibility="everyone"))
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, email="  Dan@Uni.Test ")
    assert made.email == "dan@uni.test"
    await setup.settle()
    assert acme.fake.mail.sent[-1].to == "dan@uni.test"

    # Someone else is refused, as before invites existed.
    bob = await signed_in(setup, acme.fake, 8)
    with pytest.raises(InviteRequired):
        await contestants.register(setup, bob, ContestId("acme/spring"))

    # dan makes an account with the address, confirms it and signs in.
    acme.fake.add_user(21, "dan", email="DAN@uni.test")
    dan = await signed_in(setup, acme.fake, 21)
    [waiting] = await invites.mine(setup, dan)
    assert waiting.id == made.id
    await invites.accept(setup, dan, made.id)

    registered = await contestants.register(setup, dan, ContestId("acme/spring"))

    assert registered.status.value == "pending"
    row = await _row(setup, 21)
    assert row.eligibility == {"invited": True}


async def test_a_hidden_contest_shows_itself_to_whoever_accepted_an_invite_to_it(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, INVITE_ONLY.format(visibility="hidden"))
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    carol = await signed_in(setup, acme.fake, 20)
    bob = await signed_in(setup, acme.fake, 8)
    with pytest.raises(NotFound):
        await contestants.register(setup, carol, ContestId("acme/spring"))

    await invites.accept(setup, carol, made.id)

    _, person = await _seen(setup, carol)
    assert person.invited
    await contestants.register(setup, carol, ContestId("acme/spring"))
    with pytest.raises(NotFound):
        await contestants.register(setup, bob, ContestId("acme/spring"))


async def test_an_address_the_forge_has_not_confirmed_never_matches(
    setup: Setup, people: Acme
) -> None:
    acme = people
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, email="carol@example.test")
    acme.fake.state.unverified.add("carol@example.test")
    carol = await signed_in(setup, acme.fake, 20)
    await setup.settle()

    assert await invites.mine(setup, carol) == ()
    with pytest.raises(NotFound):
        await invites.by_token(setup, carol, _token(acme))
    with pytest.raises(NotFound):
        await invites.accept(setup, carol, made.id)


async def test_nobody_acts_on_or_opens_an_invite_that_is_not_theirs(
    setup: Setup, people: Acme
) -> None:
    acme = people
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.MANAGER, username="carol")
    await setup.settle()
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(NotFound):
        await invites.accept(setup, bob, made.id)
    with pytest.raises(NotFound):
        await invites.decline(setup, bob, made.id)
    with pytest.raises(NotFound):
        await invites.by_token(setup, bob, _token(acme))
    with pytest.raises(NotFound):
        await invites.by_token(setup, bob, "not-a-token")
    assert RoleGrant(CONTEST, Role.MANAGER) not in await acme.fake.orgs.roles_of_user(8)


async def test_a_lapsed_or_decided_invite_does_nothing(
    setup: Setup, people: Acme, clock: FakeClock
) -> None:
    acme = people
    manager = await _manager(setup, acme)
    lapsing = await invites.create(
        setup, manager, CONTEST, Grant.MANAGER, username="carol", lifetime=timedelta(hours=1)
    )
    carol = await signed_in(setup, acme.fake, 20)
    clock.advance(timedelta(hours=1))

    assert await invites.mine(setup, carol) == ()
    with pytest.raises(InviteExpired):
        await invites.accept(setup, carol, lapsing.id)
    with pytest.raises(InviteExpired):
        await invites.decline(setup, carol, lapsing.id)

    # A lapsed one no longer stands in the way of a new one.
    fresh = await invites.create(setup, manager, CONTEST, Grant.MANAGER, username="carol")
    declined = await invites.decline(setup, carol, fresh.id)
    assert declined.status is InviteStatus.DECLINED
    with pytest.raises(WrongStatus):
        await invites.accept(setup, carol, fresh.id)
    assert RoleGrant(CONTEST, Role.MANAGER) not in await acme.fake.orgs.roles_of_user(20)


async def test_who_may_invite_and_to_what(setup: Setup, people: Acme) -> None:
    acme = people
    manager = await _manager(setup, acme)
    await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")

    with pytest.raises(AlreadyInvited):
        await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, Scope("acme"), Grant.CONTESTANT, username="carol")
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.OBSERVER, email="not an address")
    with pytest.raises(InvalidInvite):
        await invites.create(
            setup, manager, CONTEST, Grant.OBSERVER, username="carol", email="c@example.test"
        )
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.MANAGER, username="ada")
    with pytest.raises(NotFound):
        await invites.create(setup, manager, CONTEST, Grant.MANAGER, username="nobody")


async def test_only_an_admin_invites_an_admin_and_an_observer_invites_nobody(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await acme.fake.orgs.grant_role(20, CONTEST, Role.MANAGER)
    carol_manager = await organiser(setup, acme.fake, 20, CONTEST, Role.MANAGER)
    acme.fake.add_user(22, "erin", email="erin@example.test")
    await acme.fake.orgs.grant_role(22, CONTEST, Role.OBSERVER)
    erin = await organiser(setup, acme.fake, 22, CONTEST, Role.OBSERVER)

    with pytest.raises(Forbidden):
        await invites.create(setup, carol_manager, CONTEST, Grant.ADMIN, username="bob")
    with pytest.raises(Forbidden):
        await invites.create(setup, erin, CONTEST, Grant.OBSERVER, username="bob")
    assert await invites.at(setup, erin, CONTEST) == ()


async def test_an_invite_grants_only_while_its_sender_may_still_grant_it(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await acme.fake.orgs.grant_role(20, CONTEST, Role.MANAGER)
    carol = await organiser(setup, acme.fake, 20, CONTEST, Role.MANAGER)
    made = await invites.create(setup, carol, CONTEST, Grant.OBSERVER, username="bob")
    await acme.fake.orgs.revoke_role(20, CONTEST, Role.MANAGER)
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Forbidden):
        await invites.accept(setup, bob, made.id)
    assert RoleGrant(CONTEST, Role.OBSERVER) not in await acme.fake.orgs.roles_of_user(8)


async def test_accepting_never_lowers_a_role_held_already(setup: Setup, people: Acme) -> None:
    acme = people
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")
    await acme.fake.orgs.grant_role(20, Scope("acme"), Role.MANAGER)
    carol = await signed_in(setup, acme.fake, 20)

    await invites.accept(setup, carol, made.id)

    assert await acme.fake.orgs.roles_of_user(20) == (RoleGrant(Scope("acme"), Role.MANAGER),)


async def test_a_mail_server_that_fails_leaves_the_invite_and_sending_again_makes_a_new_token(
    setup: Setup, people: Acme, clock: FakeClock
) -> None:
    acme = people
    manager = await _manager(setup, acme)
    acme.fake.mail.down = True
    made = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")
    await setup.settle()
    [listed] = await invites.at(setup, manager, CONTEST)
    assert listed.mail_status is MailStatus.FAILED
    assert acme.fake.mail.sent == []

    acme.fake.mail.down = False
    await invites.send_again(setup, manager, CONTEST, made.id)
    await setup.settle()
    first = _token(acme)
    clock.advance(invites.SEND_AGAIN_AFTER)
    await invites.send_again(setup, manager, CONTEST, made.id)
    await setup.settle()

    [listed] = await invites.at(setup, manager, CONTEST)
    assert listed.mail_status is MailStatus.SENT
    assert listed.mailed_at is not None
    carol = await signed_in(setup, acme.fake, 20)
    with pytest.raises(NotFound):
        await invites.by_token(setup, carol, first)
    assert (await invites.by_token(setup, carol, _token(acme))).id == made.id


async def test_without_a_mail_server_nothing_is_mailed_and_the_invite_still_works(
    setup: Setup, people: Acme
) -> None:
    acme = people
    acme.fake.mail.configured = False
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")
    await setup.settle()

    assert made.mail_status is MailStatus.OFF
    with pytest.raises(InvalidInvite):
        await invites.send_again(setup, manager, CONTEST, made.id)
    carol = await signed_in(setup, acme.fake, 20)
    await invites.accept(setup, carol, made.id)
    assert RoleGrant(CONTEST, Role.OBSERVER) in await acme.fake.orgs.roles_of_user(20)


async def test_a_withdrawn_invite_cannot_be_accepted(setup: Setup, people: Acme) -> None:
    acme = people
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")

    withdrawn = await invites.withdraw(setup, manager, CONTEST, made.id)

    assert withdrawn.status is InviteStatus.WITHDRAWN
    carol = await signed_in(setup, acme.fake, 20)
    assert await invites.mine(setup, carol) == ()
    with pytest.raises(WrongStatus):
        await invites.accept(setup, carol, made.id)
    with pytest.raises(NotFound):
        await invites.withdraw(setup, manager, Scope("acme"), made.id)


async def _row(setup: Setup, user_id: int) -> Contestant:
    async with setup.unit_of_work() as ctx:
        row = await contestants.row_of(ctx, ContestId("acme/spring"), user_id)
        assert row is not None
        return row


async def _seen(setup: Setup, session: Session) -> tuple[ContestDefinition, release.Reader]:
    async with setup.unit_of_work() as ctx:
        return await release.seen(ctx, session, ContestId("acme/spring"))


async def test_a_registration_decides_once_there_is_one_and_an_accepted_place_can_be_withdrawn(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, INVITE_ONLY.format(visibility="hidden"))
    manager = await _manager(setup, acme)
    carol = await signed_in(setup, acme.fake, 20)
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    await invites.accept(setup, carol, made.id)
    await contestants.register(setup, carol, ContestId("acme/spring"))
    await contestants.reject(setup, manager, ContestId("acme/spring"), 20, "No.")

    # Rejected, she no longer sees the hidden contest, invite or not.
    with pytest.raises(NotFound):
        await _seen(setup, carol)

    acme.fake.add_user(21, "dan", email="dan@uni.test")
    dan = await signed_in(setup, acme.fake, 21)
    offered = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="dan")
    await invites.accept(setup, dan, offered.id)
    withdrawn = await invites.withdraw(setup, manager, CONTEST, offered.id)
    assert withdrawn.status is InviteStatus.WITHDRAWN
    with pytest.raises(NotFound):
        await contestants.register(setup, dan, ContestId("acme/spring"))


@pytest.mark.parametrize(
    "address",
    [
        "a@b.test,victim@x.test",
        "Foo <a@b.test>",
        "<a@b.test",
        "a@b@c.test",
        "a@localhost",
        "a b@c.test",
        "a@-b.test",
    ],
)
async def test_only_one_plain_address_is_taken(setup: Setup, people: Acme, address: str) -> None:
    manager = await _manager(setup, people)
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.OBSERVER, email=address)


async def test_an_org_makes_so_many_invites_a_day_and_mails_one_again_after_a_while(
    setup: Setup, people: Acme, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = people
    monkeypatch.setattr(invites, "ORG_DAILY_MAX", 2)
    manager = await _manager(setup, acme)
    made = await invites.create(
        setup, manager, CONTEST, Grant.OBSERVER, email="a@example.test", lifetime=timedelta(hours=2)
    )
    await invites.create(setup, manager, Scope("acme"), Grant.OBSERVER, email="b@example.test")
    with pytest.raises(InviteLimit):
        await invites.create(setup, manager, CONTEST, Grant.OBSERVER, email="c@example.test")
    await setup.settle()

    with pytest.raises(InviteLimit):
        await invites.send_again(setup, manager, CONTEST, made.id)
    clock.advance(timedelta(hours=3))
    again = await invites.send_again(setup, manager, CONTEST, made.id)

    # Lapsed, it is sent again with its whole lifetime from now.
    assert not again.expired
    assert again.expires_at == clock.now() + timedelta(hours=2)
    clock.advance(timedelta(days=1))
    await invites.create(setup, manager, CONTEST, Grant.OBSERVER, email="c@example.test")


async def test_a_mail_that_cannot_be_written_is_failed_and_never_left_waiting(
    setup: Setup, people: Acme, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = people
    manager = await _manager(setup, acme)
    without_address = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="bob")

    async def gone(*_: object) -> None:
        raise NotFound("the contest went")

    await setup.settle()
    monkeypatch.setattr(names, "scope_names", gone)
    broken = await invites.create(setup, manager, CONTEST, Grant.OBSERVER, username="carol")
    await setup.settle()

    shown = {invite.id: invite.mail_status for invite in await invites.at(setup, manager, CONTEST)}
    assert shown == {without_address.id: MailStatus.FAILED, broken.id: MailStatus.FAILED}
    assert acme.fake.mail.sent == []


async def test_a_place_and_a_role_are_refused_to_whoever_could_not_take_them(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    manager = await _manager(setup, acme)
    carol = await signed_in(setup, acme.fake, 20)
    await contestants.register(setup, carol, ContestId("acme/spring"))

    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, Scope("acme"), Grant.OBSERVER, username="carol")
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="ada")


async def test_a_place_is_taken_only_while_its_sender_may_still_offer_it(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await acme.fake.orgs.grant_role(20, CONTEST, Role.MANAGER)
    carol = await organiser(setup, acme.fake, 20, CONTEST, Role.MANAGER)
    made = await invites.create(setup, carol, CONTEST, Grant.CONTESTANT, username="bob")
    await acme.fake.orgs.revoke_role(20, CONTEST, Role.MANAGER)
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Forbidden):
        await invites.accept(setup, bob, made.id)


async def test_a_link_opened_while_the_forge_is_down_says_so(setup: Setup, people: Acme) -> None:
    acme = people
    manager = await _manager(setup, acme)
    await invites.create(setup, manager, CONTEST, Grant.OBSERVER, email="carol@example.test")
    await setup.settle()
    carol = await signed_in(setup, acme.fake, 20)
    acme.fake.state.unavailable = True
    try:
        with pytest.raises(Unavailable):
            await invites.by_token(setup, carol, _token(acme))
    finally:
        acme.fake.state.unavailable = False
