"""Registering for a contest and deciding a registration. A registration that
breaks a rule is refused with that rule's code; one that breaks none is one
pending row, and nothing is written at the forge. Two registrations for the
last place leave one row. A contest set to approve on its own approves at
once and asks for the workspace. An organiser managing the contest approves,
rejects with a reason, reopens a rejection, removes and extends, each only
from the statuses it is allowed from; anyone observing it lists the registrations, and nobody
else does.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from forge.db.tables import Contestant, Invite, Provisioning
from forge.domain.errors import (
    AlreadyRegistered,
    ContestantConflict,
    ContestFull,
    DomainNotAllowed,
    Forbidden,
    InvalidExtension,
    InvalidReason,
    InviteRequired,
    IsStaff,
    NotFound,
    RegistrationClosed,
    RegistrationRefused,
    WrongInviteCode,
    WrongStatus,
)
from forge.domain.registration import Status, WorkspaceState
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import contestants, contests, roles
from forge.services.access import Organiser
from forge.testing import tick
from tests.services.conftest import ACME, SPRING, Acme, organiser, signed_in, write_contest

READS = {"read_file", "find_user", "roles_of_user"}


def settings(*, visibility: str = "public", state: str = "published", **registration: str) -> str:
    """acme/spring running now, with the registration block's keys given as
    YAML text.
    """
    lines = [
        "name: Spring 2026",
        "start: 2026-09-26T10:00:00Z",
        "end: 2026-09-26T15:00:00Z",
        f"state: {state}",
        f"visibility: {visibility}",
    ]
    if registration:
        lines.append("registration:")
        lines.extend(f"  {key}: {value}" for key, value in registration.items())
    return "\n".join(lines) + "\n"


@pytest.fixture
async def people(acme: Acme) -> Acme:
    """acme with cyd (20), whose address is a student's, and dee (21), who has
    none, beside ada and bob.
    """
    acme.fake.add_user(20, "cyd", email="cyd@u.nus.edu")
    acme.fake.add_user(21, "dee")
    return acme


@pytest.fixture
async def manager(setup: Setup, people: Acme, spring: str) -> Organiser:
    """ada, checked to manage acme/spring."""
    return await organiser(setup, people.fake, 7, Scope("acme", "spring"), Role.MANAGER)


async def _register(
    setup: Setup, acme: Acme, user_id: int, **options: str
) -> contestants.Registration:
    session = await signed_in(setup, acme.fake, user_id)
    return await contestants.register(setup, session, SPRING, **options)


async def _rows(setup: Setup) -> list[Contestant]:
    async with setup.unit_of_work() as ctx:
        return list((await ctx.db.execute(select(Contestant))).scalars())


async def test_a_passing_registration_is_one_pending_row_and_writes_nothing_at_the_forge(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings())
    people.fake.reset_calls()

    registered = await _register(setup, people, 8)

    assert (registered.status, registered.user_id, registered.workspace) == (
        Status.PENDING,
        8,
        None,
    )
    assert registered.user is not None and registered.user.username == "bob"
    (row,) = await _rows(setup)
    assert (row.contest_id, row.status, row.eligibility) == ("acme/spring", "pending", {})
    assert {call.operation for call in people.fake.calls} <= READS


@pytest.mark.parametrize(
    ("contest", "user_id", "options", "refusal"),
    [
        (settings(opens="2026-09-27T00:00:00Z"), 8, {}, RegistrationClosed),
        (settings(closes="2026-09-26T11:00:00Z"), 8, {}, RegistrationClosed),
        (settings(), 7, {}, IsStaff),
        (settings(mode="invite-only"), 8, {}, InviteRequired),
        (
            settings(eligibility="{invite_code: sesame}"),
            8,
            {"invite_code": "open"},
            WrongInviteCode,
        ),
        (settings(eligibility="{email_pattern: '.*@u\\.nus\\.edu'}"), 21, {}, DomainNotAllowed),
    ],
    ids=["not-yet", "closed", "staff", "invite-only", "wrong-code", "wrong-address"],
)
async def test_a_registration_that_breaks_a_rule_is_refused_with_its_code(
    setup: Setup,
    people: Acme,
    spring: str,
    contest: str,
    user_id: int,
    options: dict[str, str],
    refusal: type[RegistrationRefused],
) -> None:
    await write_contest(people.fake, contest)

    with pytest.raises(refusal) as refused:
        await _register(setup, people, user_id, **options)

    assert refused.value.code == refusal.code
    assert await _rows(setup) == []


async def test_a_person_breaking_no_rule_passes_every_one_and_keeps_what_let_them_in(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(
        people.fake,
        settings(
            mode="invite-only",
            eligibility="{invite_code: sesame, email_pattern: '.*@u\\.nus\\.edu'}",
            capacity="1",
        ),
    )
    async with setup.unit_of_work() as ctx:
        ctx.db.add(
            Invite(
                scope_kind="contest",
                scope_id="acme/spring",
                target_user_id=20,
                grants={"contestant": True},
                token_hash=b"\x01" * 32,
                invited_by_user_id=7,
                expires_at=ctx.now + timedelta(days=1),
                status="accepted",
                accepted_by_user_id=20,
            )
        )

    registered = await _register(setup, people, 20, invite_code="sesame")

    assert registered.status is Status.PENDING
    (row,) = await _rows(setup)
    assert row.eligibility == {"invited": True, "invite_code": True, "email": "cyd@u.nus.edu"}


async def test_an_address_the_forge_never_confirmed_does_not_let_anyone_in(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(eligibility=r"{email_pattern: '.*@u\.nus\.edu'}"))
    people.fake.state.unverified.add("cyd@u.nus.edu")

    with pytest.raises(DomainNotAllowed):
        await _register(setup, people, 20)


async def test_a_second_registration_of_the_same_person_is_refused(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)

    with pytest.raises(AlreadyRegistered):
        await _register(setup, people, 8)


async def test_a_full_contest_refuses_the_next_person(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(capacity="1"))
    await _register(setup, people, 8)

    with pytest.raises(ContestFull) as refused:
        await _register(setup, people, 20)

    assert refused.value.code == "contest_full"


async def test_two_registrations_for_the_last_place_leave_one_row_and_one_refusal(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(capacity="1"))
    bob, cyd = await signed_in(setup, people.fake, 8), await signed_in(setup, people.fake, 20)

    outcomes = await asyncio.gather(
        contestants.register(setup, bob, SPRING),
        contestants.register(setup, cyd, SPRING),
        return_exceptions=True,
    )

    assert len(await _rows(setup)) == 1
    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "ContestFull",
        "Registration",
    ]


@pytest.mark.parametrize(
    "contest",
    [settings(visibility="hidden"), settings(state="draft"), settings(state="archived")],
    ids=["hidden", "draft", "archived"],
)
async def test_a_contest_the_person_may_not_see_is_no_such_contest(
    setup: Setup, people: Acme, spring: str, contest: str
) -> None:
    await write_contest(people.fake, contest)

    with pytest.raises(NotFound):
        await _register(setup, people, 8)


async def test_a_contest_that_approves_on_its_own_approves_at_once_and_asks_for_the_workspace(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(approval="auto"))

    registered = await _register(setup, people, 8)

    assert (registered.status, registered.workspace) == (Status.APPROVED, WorkspaceState.PREPARING)
    (row,) = await _rows(setup)
    assert (row.decided_by_user_id, row.decided_at) == (None, row.registered_at)
    async with setup.unit_of_work() as ctx:
        (asked,) = (
            await ctx.db.execute(select(Provisioning).where(Provisioning.kind == "workspace"))
        ).scalars()
    assert (asked.kind, asked.target_id, asked.status) == ("workspace", str(row.id), "pending")


async def test_a_role_cannot_be_given_to_someone_who_registered(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)

    with pytest.raises(ContestantConflict):
        await roles.grant(setup, manager, Scope("acme", "spring"), "bob", Role.OBSERVER)


async def test_an_organiser_approves_and_the_workspace_is_asked_for(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)

    approved = await contestants.approve(setup, manager, SPRING, 8)

    assert (approved.status, approved.workspace) == (Status.APPROVED, WorkspaceState.PREPARING)
    (row,) = await _rows(setup)
    assert row.decided_by_user_id == 7
    with pytest.raises(WrongStatus) as refused:
        await contestants.approve(setup, manager, SPRING, 8)
    assert refused.value.extra == {"current": Status.APPROVED}


async def test_a_rejection_carries_a_reason_the_person_reads(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)
    bob = await signed_in(setup, people.fake, 8)

    with pytest.raises(InvalidReason):
        await contestants.reject(setup, manager, SPRING, 8, "  ")
    await contestants.reject(setup, manager, SPRING, 8, "Not a student.")
    mine = await contestants.mine(setup, bob, SPRING)

    assert mine is not None
    assert (mine.status, mine.reason) == (Status.REJECTED, "Not a student.")
    with pytest.raises(WrongStatus):
        await contestants.approve(setup, manager, SPRING, 8)


async def test_a_rejection_is_taken_back_and_the_registration_is_pending_again(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)
    with pytest.raises(WrongStatus):
        await contestants.reopen(setup, manager, SPRING, 8)
    await contestants.reject(setup, manager, SPRING, 8, "Not a student.")

    reopened = await contestants.reopen(setup, manager, SPRING, 8)
    approved = await contestants.approve(setup, manager, SPRING, 8)

    assert (reopened.status, reopened.reason, reopened.decided_at) == (Status.PENDING, None, None)
    assert approved.status == Status.APPROVED
    with pytest.raises(WrongStatus):
        await contestants.reopen(setup, manager, SPRING, 8)


async def test_a_rejection_is_not_taken_back_into_a_full_contest_or_for_staff(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings(capacity="1"))
    await _register(setup, people, 8)
    await contestants.reject(setup, manager, SPRING, 8, "Not a student.")
    await _register(setup, people, 20)
    await contestants.reject(setup, manager, SPRING, 20, "Not yet.")
    await roles.grant(setup, manager, Scope("acme", "spring"), "bob", Role.OBSERVER)
    await _register(setup, people, 21)

    with pytest.raises(IsStaff):
        await contestants.reopen(setup, manager, SPRING, 8)
    with pytest.raises(ContestFull):
        await contestants.reopen(setup, manager, SPRING, 20)
    assert sorted((row.user_id, row.status) for row in await _rows(setup)) == [
        (8, "rejected"),
        (20, "rejected"),
        (21, "pending"),
    ]


async def test_reopening_needs_the_manager_role(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)
    await contestants.reject(setup, manager, SPRING, 8, "Not a student.")
    await people.fake.orgs.grant_role(21, Scope("acme", "spring"), Role.OBSERVER)
    observer = await organiser(setup, people.fake, 21, Scope("acme", "spring"))

    with pytest.raises(Forbidden):
        await contestants.reopen(setup, observer, SPRING, 8)


async def test_removing_is_for_an_approved_contestant_and_takes_their_access_away(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)
    with pytest.raises(WrongStatus):
        await contestants.remove(setup, manager, SPRING, 8)
    await contestants.approve(setup, manager, SPRING, 8)
    await tick(setup, "provisioning")
    desk = people.fake.state.repos[("acme", "spring.bob.desk")]
    assert 8 in desk.writers

    removed = await contestants.remove(setup, manager, SPRING, 8)

    assert (removed.status, removed.workspace) == (Status.REMOVED, None)
    assert 8 not in desk.writers
    assert ("acme", "spring.bob.desk") in people.fake.state.repos


async def test_an_extension_is_set_on_the_row_and_replaces_the_last_one(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)

    await contestants.extend(setup, manager, SPRING, 8, timedelta(minutes=30))
    extended = await contestants.extend(setup, manager, SPRING, 8, timedelta(minutes=20))

    assert extended.time_extension == timedelta(minutes=20)
    (row,) = await _rows(setup)
    assert row.time_extension_seconds == 1200
    with pytest.raises(InvalidExtension):
        await contestants.extend(setup, manager, SPRING, 8, timedelta(minutes=-1))
    await contestants.reject(setup, manager, SPRING, 8, "No.")
    with pytest.raises(WrongStatus):
        await contestants.extend(setup, manager, SPRING, 8, timedelta(minutes=5))


async def test_a_decision_on_someone_who_never_registered_is_not_found(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    with pytest.raises(NotFound):
        await contestants.approve(setup, manager, SPRING, 8)


async def test_observers_list_the_registrations_and_only_managers_decide(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)
    await _register(setup, people, 20)
    await contestants.approve(setup, manager, SPRING, 20)
    await roles.grant(setup, manager, Scope("acme", "spring"), "dee", Role.OBSERVER)
    observer = await organiser(setup, people.fake, 21, Scope("acme", "spring"))

    listed = await contestants.list(setup, observer, SPRING)

    assert [(entry.user_id, entry.status, entry.workspace) for entry in listed] == [
        (8, Status.PENDING, None),
        (20, Status.APPROVED, WorkspaceState.PREPARING),
    ]
    assert listed[1].user is not None and listed[1].user.email == "cyd@u.nus.edu"
    with pytest.raises(Forbidden):
        await contestants.approve(setup, observer, SPRING, 8)


async def test_an_organiser_of_another_contest_decides_nothing_here(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)
    await contests.create(setup, people.ada, ACME, "autumn")
    await tick(setup, "provisioning")
    await roles.grant(setup, manager, Scope("acme"), "dee", Role.OBSERVER)
    await roles.revoke(setup, manager, Scope("acme"), 21)
    await roles.grant(setup, people.ada, Scope("acme", "autumn"), "dee", Role.MANAGER)
    elsewhere = await organiser(setup, people.fake, 21, Scope("acme", "autumn"), Role.MANAGER)

    with pytest.raises(Forbidden):
        await contestants.list(setup, elsewhere, SPRING)
    with pytest.raises(Forbidden):
        await contestants.approve(setup, elsewhere, SPRING, 8)


async def test_a_person_with_no_registration_has_none(
    setup: Setup, people: Acme, spring: str
) -> None:
    session: Session = await signed_in(setup, people.fake, 8)

    assert await contestants.mine(setup, session, SPRING) is None
