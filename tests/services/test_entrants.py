"""Registering an entrant and deciding it. The rules are the `registration`
block's window, capacity and ways, and the `entrants` block's sizes: a
registration that breaks a rule is refused with that rule's code; one that
breaks none is one pending entrant with the person as its only member, the
way they came in by and what let them through kept on their member row, and
nothing is written at the forge. Two registrations for the last place leave
one entrant. An entrant that is complete and whose every member came in by a
way that approves straight away is approved at once. An organiser managing
the contest approves, rejects with a reason, reopens a rejection, removes
and extends, each only from the statuses it is allowed from; anyone
observing it lists the entrants, and nobody else does. An extension is for
the tasks it names, every task when it names none, and is refused on a task
the contest does not list, on a task whose reveal has passed, and when
shortening it would leave a submission after the due or the close it was
made before; a removed entrant's holds no reveal back. The registration
panel says whether and how the person may register, asking for a code only
when a code would let them in.
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from forge.db.tables import Entrant, EntrantMember
from forge.domain.errors import (
    AlreadyRegistered,
    ContestantConflict,
    ContestFull,
    DomainNotAllowed,
    EntrantNameRequired,
    EntrantNameTaken,
    EntrantsAlone,
    Forbidden,
    InvalidEntrantName,
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
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import EntrantOwner
from forge.domain.registration import Status, WayKind
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import account, contests, entrants, published, roles, submissions, timelines
from forge.services.access import Organiser
from forge.testing import FakeClock
from tests.services.conftest import (
    ACME,
    SPRING,
    Acme,
    Entered,
    organiser,
    signed_in,
    upload,
    write_contest,
)

READS = {"read_file", "find_user", "roles_of_user", "verified_emails"}


def settings(
    *,
    visibility: str = "everyone",
    state: str = "published",
    ways: str | None = None,
    entrants_block: str | None = None,
    **registration: str,
) -> str:
    """acme/spring running now, with the registration block's keys and its
    ways, and the entrants block, given as YAML text.
    """
    lines = [
        "name: Spring 2026",
        "start: 2026-09-26T10:00:00Z",
        "end: 2026-09-26T15:00:00Z",
        f"state: {state}",
        f"visibility: {visibility}",
    ]
    if registration or ways is not None:
        lines.append("registration:")
        lines.extend(f"  {key}: {value}" for key, value in registration.items())
        if ways is not None:
            lines.append(f"  ways: {ways}")
    if entrants_block is not None:
        lines.append(f"entrants: {entrants_block}")
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


async def _register(setup: Setup, acme: Acme, user_id: int, **options: str) -> entrants.Entrant:
    session = await signed_in(setup, acme.fake, user_id)
    return await entrants.register(setup, session, SPRING, **options)


async def _rows(setup: Setup) -> list[Entrant]:
    async with setup.unit_of_work() as ctx:
        return list(
            (await ctx.db.execute(select(Entrant).order_by(Entrant.registered_at))).scalars()
        )


async def _members(setup: Setup) -> list[EntrantMember]:
    async with setup.unit_of_work() as ctx:
        return list((await ctx.db.execute(select(EntrantMember))).scalars())


async def _of(setup: Setup, user_id: int) -> uuid.UUID:
    async with setup.unit_of_work() as ctx:
        found = await entrants.membership(ctx, SPRING, user_id)
        assert found is not None
        return found.entrant_id


async def test_a_passing_registration_is_one_pending_entrant_and_writes_nothing_at_the_forge(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings())
    people.fake.reset_calls()

    registered = await _register(setup, people, 8)

    assert (registered.status, registered.name, registered.display_name) == (
        Status.PENDING,
        None,
        "bob",
    )
    assert [(member.user_id, member.way_kind) for member in registered.members] == [
        (8, WayKind.SIGNUP)
    ]
    (row,) = await _rows(setup)
    (member,) = await _members(setup)
    assert (row.contest_id, row.status, row.registered_by_user_id) == ("acme/spring", "pending", 8)
    assert (member.way, member.way_kind, member.approval, member.eligibility) == (
        0,
        "signup",
        "manual",
        {},
    )
    assert {call.operation for call in people.fake.calls} <= READS


@pytest.mark.parametrize(
    ("contest", "user_id", "options", "refusal"),
    [
        (settings(opens="2026-09-27T00:00:00Z"), 8, {}, RegistrationClosed),
        (settings(closes="2026-09-26T11:00:00Z"), 8, {}, RegistrationClosed),
        (settings(ways="[]"), 8, {}, RegistrationClosed),
        (settings(), 7, {}, IsStaff),
        (settings(ways="[{invite: {}}]"), 8, {}, InviteRequired),
        (settings(ways="[{signup: {code: sesame}}]"), 8, {"code": "open"}, WrongInviteCode),
        (settings(ways="[{signup: {code: sesame}}]"), 8, {}, WrongInviteCode),
        (settings(ways="[{signup: {domains: [u.nus.edu]}}]"), 21, {}, DomainNotAllowed),
        (
            settings(ways="[{signup: {email_pattern: '.*@u\\.nus\\.edu'}}]"),
            21,
            {},
            DomainNotAllowed,
        ),
    ],
    ids=[
        "not-yet",
        "closed",
        "no-way",
        "staff",
        "invite-only",
        "wrong-code",
        "no-code",
        "wrong-domain",
        "wrong-address",
    ],
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


async def test_registration_is_closed_once_the_contest_has_ended(
    setup: Setup, people: Acme, spring: str, clock: FakeClock
) -> None:
    await write_contest(people.fake, settings())
    clock.advance(timedelta(hours=3))

    with pytest.raises(RegistrationClosed, match="ended"):
        await _register(setup, people, 8)


async def test_a_person_meeting_every_condition_of_a_way_keeps_what_let_them_in(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(
        people.fake,
        settings(
            ways="[{signup: {name: Students, code: sesame, domains: [u.nus.edu]}}]", capacity="1"
        ),
    )

    registered = await _register(setup, people, 20, code="sesame")

    assert registered.status is Status.PENDING
    assert [(member.way_kind, member.way_name) for member in registered.members] == [
        (WayKind.SIGNUP, "Students")
    ]
    (member,) = await _members(setup)
    assert member.eligibility == {"code": True, "email": "cyd@u.nus.edu"}


async def test_the_most_lenient_way_that_admits_the_person_is_the_one_kept(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(
        people.fake,
        settings(
            ways=(
                "[{signup: {name: Anyone}}, "
                "{signup: {name: Students, domains: [u.nus.edu], approval: auto}}]"
            )
        ),
    )

    cyd = await _register(setup, people, 20)
    dee = await _register(setup, people, 21)

    assert (cyd.status, cyd.members[0].way_name) == (Status.APPROVED, "Students")
    assert (dee.status, dee.members[0].way_name) == (Status.PENDING, "Anyone")


async def test_an_address_the_forge_never_confirmed_does_not_let_anyone_in(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(ways="[{signup: {domains: [u.nus.edu]}}]"))
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


async def test_a_full_contest_refuses_the_next_entrant(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(capacity="1"))
    await _register(setup, people, 8)

    with pytest.raises(ContestFull) as refused:
        await _register(setup, people, 20)

    assert refused.value.code == "contest_full"


async def test_two_registrations_for_the_last_place_leave_one_entrant_and_one_refusal(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(capacity="1"))
    bob, cyd = await signed_in(setup, people.fake, 8), await signed_in(setup, people.fake, 20)

    outcomes = await asyncio.gather(
        entrants.register(setup, bob, SPRING),
        entrants.register(setup, cyd, SPRING),
        return_exceptions=True,
    )

    assert len(await _rows(setup)) == 1
    assert sorted(type(outcome).__name__ for outcome in outcomes) == ["ContestFull", "Entrant"]


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


async def test_a_way_that_approves_on_its_own_approves_at_once(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(ways="[{signup: {approval: auto}}]"))
    people.fake.reset_calls()

    registered = await _register(setup, people, 8)

    assert registered.status == Status.APPROVED
    (row,) = await _rows(setup)
    assert (row.decided_by_user_id, row.decided_at) == (None, row.registered_at)
    assert {call.operation for call in people.fake.calls} <= READS


async def test_an_entrant_of_several_needs_a_name_no_other_entrant_has(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(entrants_block="{most: 3}"))

    with pytest.raises(EntrantNameRequired) as refused:
        await _register(setup, people, 8)
    assert refused.value.code == "entrant_name_required"
    for bad in ("  ", "x" * 61, "tab\there"):
        with pytest.raises(InvalidEntrantName):
            await _register(setup, people, 8, name=bad)
    named = await _register(setup, people, 8, name="  Adders ")
    with pytest.raises(EntrantNameTaken) as taken:
        await _register(setup, people, 20, name="ADDERS")

    assert (named.name, named.display_name) == ("Adders", "Adders")
    assert taken.value.code == "entrant_name_taken"


async def test_an_entrant_of_one_person_takes_no_name(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings())

    with pytest.raises(EntrantsAlone) as refused:
        await _register(setup, people, 8, name="Adders")

    assert refused.value.code == "entrants_alone"


async def test_a_role_cannot_be_given_to_someone_who_registered(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 8)

    with pytest.raises(ContestantConflict):
        await roles.grant(setup, manager, Scope("acme", "spring"), "bob", Role.OBSERVER)


async def test_an_organiser_approves(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    registered = await _register(setup, people, 8)

    approved = await entrants.approve(setup, manager, SPRING, registered.id)

    assert approved.status == Status.APPROVED
    (row,) = await _rows(setup)
    assert row.decided_by_user_id == 7
    with pytest.raises(WrongStatus) as refused:
        await entrants.approve(setup, manager, SPRING, registered.id)
    assert refused.value.extra == {"current": Status.APPROVED}


async def test_a_rejection_carries_a_reason_the_members_read(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    registered = await _register(setup, people, 8)
    bob = await signed_in(setup, people.fake, 8)

    with pytest.raises(InvalidReason):
        await entrants.reject(setup, manager, SPRING, registered.id, "  ")
    await entrants.reject(setup, manager, SPRING, registered.id, "Not a student.")
    mine = (await entrants.mine(setup, bob, SPRING)).entrant

    assert mine is not None
    assert (mine.status, mine.reason) == (Status.REJECTED, "Not a student.")
    with pytest.raises(WrongStatus):
        await entrants.approve(setup, manager, SPRING, registered.id)


async def test_a_rejection_is_taken_back_and_the_entrant_is_pending_again(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    registered = await _register(setup, people, 8)
    with pytest.raises(WrongStatus):
        await entrants.reopen(setup, manager, SPRING, registered.id)
    await entrants.reject(setup, manager, SPRING, registered.id, "Not a student.")

    reopened = await entrants.reopen(setup, manager, SPRING, registered.id)
    approved = await entrants.approve(setup, manager, SPRING, registered.id)

    assert (reopened.status, reopened.reason, reopened.decided_at) == (Status.PENDING, None, None)
    assert approved.status == Status.APPROVED
    with pytest.raises(WrongStatus):
        await entrants.reopen(setup, manager, SPRING, registered.id)


async def test_a_rejection_is_not_taken_back_into_a_full_contest_or_for_staff(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings(capacity="1"))
    bob = await _register(setup, people, 8)
    await entrants.reject(setup, manager, SPRING, bob.id, "Not a student.")
    cyd = await _register(setup, people, 20)
    await entrants.reject(setup, manager, SPRING, cyd.id, "Not yet.")
    await roles.grant(setup, manager, Scope("acme", "spring"), "bob", Role.OBSERVER)
    await _register(setup, people, 21)

    with pytest.raises(IsStaff):
        await entrants.reopen(setup, manager, SPRING, bob.id)
    with pytest.raises(ContestFull):
        await entrants.reopen(setup, manager, SPRING, cyd.id)
    assert [row.status for row in await _rows(setup)] == ["rejected", "rejected", "pending"]


async def test_reopening_needs_the_manager_role(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    registered = await _register(setup, people, 8)
    await entrants.reject(setup, manager, SPRING, registered.id, "Not a student.")
    await people.fake.orgs.grant_role(21, Scope("acme", "spring"), Role.OBSERVER)
    observer = await organiser(setup, people.fake, 21, Scope("acme", "spring"))

    with pytest.raises(Forbidden):
        await entrants.reopen(setup, observer, SPRING, registered.id)


async def test_removing_is_for_an_approved_entrant_and_takes_its_members_access_away(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    registered = await _register(setup, people, 8)
    with pytest.raises(WrongStatus):
        await entrants.remove(setup, manager, SPRING, registered.id)
    await entrants.approve(setup, manager, SPRING, registered.id)
    owner = EntrantOwner(registered.id)
    await people.fake.workspaces.open_submission_place(
        WorkspaceId(f"acme/spring/@{owner.segment}"), TaskId("acme/spring/sum"), [8]
    )
    place = people.fake.state.repos[("acme", f"spring.sum.{owner.segment}.sub")]
    assert 8 in place.writers

    removed = await entrants.remove(setup, manager, SPRING, registered.id)

    assert removed.status == Status.REMOVED
    assert 8 not in place.writers
    assert ("acme", f"spring.sum.{owner.segment}.sub") in people.fake.state.repos
    # Still in it, so nobody removed registers again.
    with pytest.raises(AlreadyRegistered):
        await _register(setup, people, 8)


async def test_an_extension_is_set_on_the_entrant_and_replaces_the_last_one(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    registered = await _register(setup, people, 8)

    await entrants.extend(setup, manager, SPRING, registered.id, timedelta(minutes=30))
    extended = await entrants.extend(setup, manager, SPRING, registered.id, timedelta(minutes=20))

    assert extended.time_extension == timedelta(minutes=20)
    (row,) = await _rows(setup)
    assert row.time_extension_seconds == 1200
    with pytest.raises(InvalidExtension):
        await entrants.extend(setup, manager, SPRING, registered.id, timedelta(minutes=-1))
    await entrants.reject(setup, manager, SPRING, registered.id, "No.")
    with pytest.raises(WrongStatus):
        await entrants.extend(setup, manager, SPRING, registered.id, timedelta(minutes=5))


TIMED = """name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: everyone
tasks:
  - {{id: sum, {times}}}
  - id: other
"""


async def _submit_sum(setup: Setup, acme: Acme, entered: Entered, key: str) -> int:
    made = await upload(setup, acme.fake, entered.session, entered.task, b"print(3)\n")
    submitted = await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    return submitted.number


async def test_an_extension_on_named_tasks_is_kept_with_them_and_needs_tasks_the_contest_lists(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, TIMED.format(times="closes: 2026-09-26T13:00:00Z"))
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    bob = entered.entrant

    named = await entrants.extend(
        setup, manager, SPRING, bob, timedelta(minutes=30), tasks=["sum", "sum"]
    )

    assert (named.time_extension, named.extension_tasks) == (timedelta(minutes=30), ("sum",))
    (row,) = await _rows(setup)
    assert (row.time_extension_seconds, row.extension_tasks) == (1800, ["sum"])
    for tasks in (["nope"], ["sum", "nope"], []):
        with pytest.raises(InvalidExtension):
            await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=1), tasks=tasks)
    every = await entrants.extend(setup, manager, SPRING, bob, timedelta(minutes=10))
    assert (every.time_extension, every.extension_tasks) == (timedelta(minutes=10), None)


async def test_an_extension_is_refused_on_a_task_whose_reveal_has_passed(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await write_contest(acme.fake, TIMED.format(times="closes: 2026-09-26T13:00:00Z"))
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    bob = entered.entrant
    clock.advance(timedelta(hours=1, minutes=30))

    with pytest.raises(InvalidExtension, match="sum's hidden results were shown"):
        await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=1), tasks=["sum"])
    with pytest.raises(InvalidExtension):
        await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=1))
    other = await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=1), tasks=["other"])

    assert other.extension_tasks == ("other",)


async def test_shortening_an_extension_is_refused_when_it_would_make_a_submission_late(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await write_contest(
        acme.fake, TIMED.format(times="due: 2026-09-26T12:30:00Z, closes: 2026-09-26T14:00:00Z")
    )
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    bob = entered.entrant
    await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=1), tasks=["sum"])
    clock.advance(timedelta(hours=1, minutes=15))
    assert await _submit_sum(setup, acme, entered, "key-0001-aaaa") == 1

    made = "sum's submission 1, made at 2026-09-26T13:15:00+00:00, was on time"
    for shorter, tasks in (
        (timedelta(minutes=30), ["sum"]),
        (timedelta(0), None),
        (timedelta(hours=1), ["other"]),
    ):
        with pytest.raises(InvalidExtension, match=re.escape(made)):
            await entrants.extend(setup, manager, SPRING, bob, shorter, tasks=tasks)
    kept = await entrants.extend(setup, manager, SPRING, bob, timedelta(minutes=45), tasks=["sum"])

    assert kept.time_extension == timedelta(minutes=45)


async def test_shortening_an_extension_is_refused_when_it_would_close_before_a_submission(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await write_contest(acme.fake, TIMED.format(times="closes: 2026-09-26T13:00:00Z"))
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    bob = entered.entrant
    await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=1), tasks=["sum"])
    clock.advance(timedelta(hours=1, minutes=30))
    assert await _submit_sum(setup, acme, entered, "key-0001-aaaa") == 1

    with pytest.raises(
        InvalidExtension,
        match="sum's submission 1, made at 2026-09-26T13:30:00\\+00:00, would then be after "
        "the task closed",
    ):
        await entrants.extend(setup, manager, SPRING, bob, timedelta(minutes=20), tasks=["sum"])
    longer = await entrants.extend(setup, manager, SPRING, bob, timedelta(hours=2), tasks=["sum"])

    assert longer.time_extension == timedelta(hours=2)


async def test_a_removed_entrants_extension_holds_no_reveal_back(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, TIMED.format(times="closes: 2026-09-26T13:00:00Z"))
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await entrants.extend(
        setup, manager, SPRING, entered.entrant, timedelta(hours=1), tasks=["sum"]
    )

    async def reveal() -> datetime | None:
        async with setup.unit_of_work() as ctx:
            settings = await published.contest(ctx, SPRING)
            return await timelines.reveal(ctx, SPRING, settings, "sum")

    assert await reveal() == datetime(2026, 9, 26, 14, tzinfo=UTC)
    await entrants.remove(setup, manager, SPRING, entered.entrant)
    assert await reveal() == datetime(2026, 9, 26, 13, tzinfo=UTC)


async def test_a_decision_on_an_entrant_that_is_not_there_is_not_found(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    with pytest.raises(NotFound):
        await entrants.approve(setup, manager, SPRING, uuid.uuid4())


async def test_observers_list_the_entrants_and_only_managers_decide(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    bob = await _register(setup, people, 8)
    cyd = await _register(setup, people, 20)
    await entrants.approve(setup, manager, SPRING, cyd.id)
    await roles.grant(setup, manager, Scope("acme", "spring"), "dee", Role.OBSERVER)
    observer = await organiser(setup, people.fake, 21, Scope("acme", "spring"))

    listed = await entrants.list(setup, observer, SPRING)

    assert [(entry.display_name, entry.status) for entry in listed] == [
        ("bob", Status.PENDING),
        ("cyd", Status.APPROVED),
    ]
    first = listed[1].members[0]
    assert first.user is not None and first.user.email == "cyd@u.nus.edu"
    assert (listed[1].complete, listed[1].locked, listed[1].submitted) == (True, True, False)
    with pytest.raises(Forbidden):
        await entrants.approve(setup, observer, SPRING, bob.id)


async def test_an_organiser_of_another_contest_decides_nothing_here(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    bob = await _register(setup, people, 8)
    await contests.create(setup, people.ada, ACME, "autumn")
    await roles.grant(setup, manager, Scope("acme"), "dee", Role.OBSERVER)
    await roles.revoke(setup, manager, Scope("acme"), 21)
    await roles.grant(setup, people.ada, Scope("acme", "autumn"), "dee", Role.MANAGER)
    elsewhere = await organiser(setup, people.fake, 21, Scope("acme", "autumn"), Role.MANAGER)

    with pytest.raises(Forbidden):
        await entrants.list(setup, elsewhere, SPRING)
    with pytest.raises(Forbidden):
        await entrants.approve(setup, elsewhere, SPRING, bob.id)
    autumn = await organiser(setup, people.fake, 7, Scope("acme", "autumn"), Role.MANAGER)
    with pytest.raises(NotFound):
        await entrants.approve(setup, autumn, ContestId("acme/autumn"), bob.id)


async def test_a_person_in_no_entrant_is_told_they_may_register(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings())
    session: Session = await signed_in(setup, people.fake, 8)

    panel = await entrants.mine(setup, session, SPRING)

    assert (panel.entrant, panel.invitations, panel.refused) == (None, (), None)
    assert (panel.can_register, panel.asks_code, panel.registration_open) == (True, False, True)
    assert (panel.most, panel.fewest) == (1, 1)


@pytest.mark.parametrize(
    ("contest", "user_id", "code"),
    [
        (settings(opens="2026-09-27T00:00:00Z"), 8, "registration_closed"),
        (settings(ways="[]"), 8, "registration_closed"),
        (settings(ways="[{invite: {}}]"), 8, "invite_required"),
        (settings(ways="[{signup: {domains: [u.nus.edu]}}]"), 21, "domain_not_allowed"),
        (settings(), 7, "is_staff"),
    ],
    ids=["window", "no-way", "invite-only", "address", "staff"],
)
async def test_the_panel_says_why_the_person_cannot_register(
    setup: Setup, people: Acme, spring: str, contest: str, user_id: int, code: str
) -> None:
    await write_contest(people.fake, contest)
    session = await signed_in(setup, people.fake, user_id)

    panel = await entrants.mine(setup, session, SPRING)

    assert (panel.can_register, panel.asks_code) == (False, False)
    assert panel.refused is not None and panel.refused.code == code


async def test_the_panel_asks_for_a_code_only_when_a_code_would_let_the_person_in(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(
        people.fake,
        settings(ways="[{signup: {domains: [u.nus.edu], approval: auto}}, {signup: {code: s}}]"),
    )

    student = await entrants.mine(setup, await signed_in(setup, people.fake, 20), SPRING)
    guest = await entrants.mine(setup, await signed_in(setup, people.fake, 21), SPRING)

    assert (student.can_register, student.asks_code) == (True, False)
    assert (guest.can_register, guest.asks_code, guest.refused) == (True, True, None)


async def test_capacity_counts_entrants_and_not_their_members(
    setup: Setup, people: Acme, spring: str
) -> None:
    await write_contest(people.fake, settings(capacity="1", entrants_block="{most: 3}"))
    await _register(setup, people, 8, name="Adders")

    with pytest.raises(ContestFull):
        await _register(setup, people, 20, name="Takers")
    full = await entrants.mine(setup, await signed_in(setup, people.fake, 20), SPRING)

    assert full.refused is not None and full.refused.code == "contest_full"


async def test_a_deleted_users_member_row_keeps_their_id_and_shows_them_as_deleted(
    setup: Setup, people: Acme, spring: str, manager: Organiser
) -> None:
    await write_contest(people.fake, settings())
    await _register(setup, people, 20)
    cyd = await signed_in(setup, people.fake, 20)

    await account.delete(setup, cyd)
    listed = await entrants.list(setup, manager, SPRING)

    (gone,) = listed
    assert gone.display_name == "Deleted user"
    assert [(member.user_id, member.user) for member in gone.members] == [(20, None)]
    assert await _of(setup, 20) == gone.id
