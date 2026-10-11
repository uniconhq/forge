"""An organiser invites someone by username or by email address, to a role at a
scope or a place in a contest. The invite's mail goes out once the request
has committed and never holds it up; a mail that fails leaves the invite
made, and sending it again makes a new token. The person acts on their own
invites only: an address is matched against the ones the forge has
confirmed for them, and accepting grants the role, or registers them by the
contest's invite way, which an accepted place also lets them in by later,
and shows them a hidden contest while they are in no entrant there. A
lapsed invite does nothing.
"""

from datetime import timedelta

import pytest

from forge.db.tables import EntrantMember
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import (
    AlreadyInvited,
    AlreadyRegistered,
    Forbidden,
    InvalidInvite,
    InviteExpired,
    InviteLimit,
    InviteRequired,
    NoInviteWay,
    NotFound,
    Unavailable,
    WrongStatus,
)
from forge.domain.ids import ContestId
from forge.domain.invites import Grant, InviteStatus, MailStatus
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import entrants, invites, names, release
from forge.services.access import Organiser
from forge.testing import APP_URL, FakeClock
from tests.services.conftest import RUNNING, Acme, organiser, signed_in, write_contest

CONTEST = Scope("acme", "spring")
CONTEST_ID = ContestId("acme/spring")

INVITE_ONLY = """name: Spring 2026
start: 2026-09-27T10:00:00Z
end: 2026-09-27T15:00:00Z
state: published
visibility: {visibility}
registration:
  ways: [{{invite: {{approval: {approval}}}}}]
entrants: {{most: {most}}}
"""


def invite_only(visibility: str, approval: str = "manual", most: int = 1) -> str:
    """acme/spring, starting tomorrow, taking the people it invites alone."""
    return INVITE_ONLY.format(visibility=visibility, approval=approval, most=most)


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
    assert (made.where.path, made.contest_title) == ("acme/spring", "Spring 2026")
    await setup.settle()
    [mail] = acme.fake.mail.sent
    assert mail.to == "carol@example.test"
    assert "ada has invited you to be a manager of Spring 2026" in mail.text
    assert f"{APP_URL}/invites#" in mail.text
    [listed] = await invites.at(setup, manager, CONTEST)
    assert (listed.id, listed.mail_status) == (made.id, MailStatus.SENT)

    carol = await signed_in(setup, acme.fake, 20)
    [mine] = await invites.mine(setup, carol)
    assert mine.id == made.id
    assert (await invites.by_token(setup, carol, _token(acme))).id == made.id
    accepted = await invites.accept(setup, carol, made.id)

    assert (accepted.invite.status, accepted.entered) == (InviteStatus.ACCEPTED, None)
    grants = await acme.fake.orgs.roles_of_user(20)
    assert RoleGrant(CONTEST, Role.MANAGER) in grants
    assert await invites.mine(setup, carol) == ()


async def test_an_invite_by_email_waits_for_the_account_and_accepting_it_registers(
    setup: Setup, people: Acme, clock: FakeClock
) -> None:
    acme = people
    await write_contest(acme.fake, invite_only("everyone"))
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, email="  Dan@Uni.Test ")
    assert made.email == "dan@uni.test"
    await setup.settle()
    assert acme.fake.mail.sent[-1].to == "dan@uni.test"

    # Someone else is refused, as before invites existed.
    bob = await signed_in(setup, acme.fake, 8)
    with pytest.raises(InviteRequired):
        await entrants.register(setup, bob, ContestId("acme/spring"))

    # dan makes an account with the address, confirms it and signs in.
    acme.fake.add_user(21, "dan", email="DAN@uni.test")
    dan = await signed_in(setup, acme.fake, 21)
    [waiting] = await invites.mine(setup, dan)
    assert waiting.id == made.id

    accepted = await invites.accept(setup, dan, made.id)

    assert accepted.entered is not None
    assert (accepted.entered.how, accepted.entered.status.value) == ("made", "pending")
    assert accepted.entered.entrant.name == "dan"
    row = await _row(setup, 21)
    assert (row.way, row.way_kind, row.eligibility) == (0, "invite", {"invited": True})
    with pytest.raises(AlreadyRegistered):
        await entrants.register(setup, dan, ContestId("acme/spring"))


async def test_a_hidden_contest_shows_itself_to_whoever_accepted_an_invite_to_it(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, invite_only("hidden", "auto"))
    manager = await _manager(setup, acme)
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    carol = await signed_in(setup, acme.fake, 20)
    bob = await signed_in(setup, acme.fake, 8)
    with pytest.raises(NotFound):
        await entrants.register(setup, carol, ContestId("acme/spring"))

    accepted = await invites.accept(setup, carol, made.id)

    assert accepted.entered is not None and accepted.entered.status.value == "approved"
    _, person = await _seen(setup, carol)
    assert person.entrant is not None and not person.invited
    await entrants.leave(setup, carol, ContestId("acme/spring"))
    _, person = await _seen(setup, carol)
    assert person.invited
    await entrants.register(setup, carol, ContestId("acme/spring"))
    with pytest.raises(NotFound):
        await entrants.register(setup, bob, ContestId("acme/spring"))


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


async def _row(setup: Setup, user_id: int) -> EntrantMember:
    async with setup.unit_of_work() as ctx:
        row = await entrants.membership(ctx, ContestId("acme/spring"), user_id)
        assert row is not None
        return row


async def _seen(setup: Setup, session: Session) -> tuple[ContestDefinition, release.Reader]:
    async with setup.unit_of_work() as ctx:
        return await release.seen(ctx, session, ContestId("acme/spring"))


async def test_a_registration_decides_once_there_is_one_and_an_accepted_place_can_be_withdrawn(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, invite_only("hidden"))
    manager = await _manager(setup, acme)
    carol = await signed_in(setup, acme.fake, 20)
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    accepted = await invites.accept(setup, carol, made.id)
    assert accepted.entered is not None
    await entrants.reject(
        setup, manager, ContestId("acme/spring"), accepted.entered.entrant.id, "No."
    )

    # Rejected, she no longer sees the hidden contest, invite or not.
    with pytest.raises(NotFound):
        await _seen(setup, carol)

    acme.fake.add_user(21, "dan", email="dan@uni.test")
    dan = await signed_in(setup, acme.fake, 21)
    offered = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="dan")
    await invites.accept(setup, dan, offered.id)
    await entrants.leave(setup, dan, ContestId("acme/spring"))
    withdrawn = await invites.withdraw(setup, manager, CONTEST, offered.id)
    assert withdrawn.status is InviteStatus.WITHDRAWN
    with pytest.raises(NotFound):
        await entrants.register(setup, dan, ContestId("acme/spring"))


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
    await write_contest(acme.fake, RUNNING.format(visibility="everyone") + WAYS)
    manager = await _manager(setup, acme)
    carol = await signed_in(setup, acme.fake, 20)
    await entrants.register(setup, carol, ContestId("acme/spring"))

    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, Scope("acme"), Grant.OBSERVER, username="carol")
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="ada")
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    with pytest.raises(InvalidInvite):
        await invites.create(setup, manager, CONTEST, Grant.MEMBER, username="bob")


async def test_a_place_is_taken_only_while_its_sender_may_still_offer_it(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, invite_only("everyone"))
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


WAYS = "registration:\n  ways: [{invite: {}}, {signup: {}}]\n"


async def test_a_place_is_offered_only_while_the_contest_takes_people_by_invite(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    manager = await _manager(setup, acme)

    with pytest.raises(NoInviteWay) as refused:
        await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    assert refused.value.code == "no_invite_way"

    await write_contest(acme.fake, invite_only("everyone"))
    made = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    carol = await signed_in(setup, acme.fake, 20)

    # Accepting is refused and closes nothing.
    with pytest.raises(InviteRequired):
        await invites.accept(setup, carol, made.id)
    [still] = await invites.mine(setup, carol)
    assert still.id == made.id


async def test_accepting_a_place_names_the_entrant_after_its_person_and_finds_one_already_in(
    setup: Setup, people: Acme
) -> None:
    acme = people
    both = invite_only("everyone", "auto", most=3).replace(
        "[{invite: {approval: auto}}]", "[{invite: {approval: auto}}, {signup: {}}]"
    )
    await write_contest(acme.fake, both)
    manager = await _manager(setup, acme)
    carol = await signed_in(setup, acme.fake, 20)
    acme.fake.add_user(21, "dan", email="dan@uni.test")
    dan = await signed_in(setup, acme.fake, 21)
    bob = await signed_in(setup, acme.fake, 8)
    await entrants.register(setup, bob, CONTEST_ID, name="Carol")
    first = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="carol")
    second = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="dan")

    made = await invites.accept(setup, carol, first.id)
    assert made.entered is not None
    assert (made.entered.how, made.entered.entrant.name) == ("made", "carol (2)")
    into = made.entered.entrant.id
    moved_in = await invites.create(
        setup, manager, CONTEST, Grant.CONTESTANT, username="dan", entrant=into
    )
    joined = await invites.accept(setup, dan, moved_in.id)
    assert joined.entered is not None and joined.entered.how == "joined"
    found = await invites.accept(setup, dan, second.id)

    assert found.entered is not None
    assert (found.entered.how, found.entered.entrant.id) == ("already", made.entered.entrant.id)
    assert found.invite.status is InviteStatus.ACCEPTED


async def test_a_members_invite_is_mailed_as_an_invite_to_join_the_entrant(
    setup: Setup, people: Acme
) -> None:
    acme = people
    await write_contest(acme.fake, invite_only("everyone", "auto", most=2))
    manager = await _manager(setup, acme)
    bob = await signed_in(setup, acme.fake, 8)
    offered = await invites.create(setup, manager, CONTEST, Grant.CONTESTANT, username="bob")
    accepted = await invites.accept(setup, bob, offered.id)
    assert accepted.entered is not None
    await entrants.organise_rename(
        setup, manager, CONTEST_ID, accepted.entered.entrant.id, "Adders"
    )

    asked = await entrants.invite(setup, bob, CONTEST_ID, email="carol@example.test")
    await setup.settle()

    assert (asked.grants, asked.entrant is not None and asked.entrant.name) == (
        Grant.MEMBER,
        "Adders",
    )
    assert "bob has invited you to join Adders in Spring 2026" in acme.fake.mail.sent[-1].text
