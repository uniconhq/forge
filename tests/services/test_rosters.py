"""Who is in an entrant, in a contest whose `entrants` block lets one hold
more than one person. A member invites people in, by username or address,
and they join by accepting, qualifying by one of the contest's ways as
themselves; up to `entrants.most` with the invites waiting counted, and
only until the roster locks, at the start or at the entrant's first
submission. A member leaves, or moves by accepting another entrant's invite
while the one they leave has submitted nothing. The entrant is approved on
its own once complete and every member came in by a way that approves
straight away, and a member who comes in by one an organiser approves sends
it back to pending. Organisers invite people straight into an entrant,
move, take out and rename members, delete an entrant that never submitted,
and extend one on the tasks they name, whatever the lock. The entrant is
the one row: one workspace its members reach as the membership changes, in
the request that changes it, one count of submissions, one extension, and
one desk of questions.
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forge.adapters.fakes import FakeForge
from forge.adapters.ids import parse_task, parse_workspace
from forge.db.tables import Entrant
from forge.domain.errors import (
    AlreadyInvited,
    EntrantChanged,
    EntrantFull,
    EntrantHasSubmissions,
    EntrantIncomplete,
    EntrantNameTaken,
    EntrantsAlone,
    Forbidden,
    InEntrant,
    InvalidExtension,
    InviteLimit,
    IsStaff,
    NoInviteWay,
    NotFound,
    RateLimited,
    RegistrationClosed,
    RosterLocked,
    TaskClosed,
    Unavailable,
    WrongStatus,
)
from forge.domain.ids import ContestId, TaskId
from forge.domain.invites import Grant, InviteStatus
from forge.domain.names import EntrantOwner
from forge.domain.registration import Status, WayKind
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import (
    account,
    clarifications,
    entrants,
    identity,
    invites,
    live,
    published,
    submissions,
    timelines,
)
from forge.services.access import Organiser
from forge.testing import FakeClock
from tests.services.conftest import (
    SPRING,
    Acme,
    organiser,
    publish,
    signed_in,
    upload,
    write_contest,
)

START = datetime(2026, 9, 26, 13, tzinfo=UTC)
SOON = """name: Spring 2026
start: 2026-09-26T13:00:00Z
end: 2026-09-26T18:00:00Z
state: published
visibility: everyone
tasks:
{tasks}registration:
  ways: {ways}
entrants: {entrants}
"""


def contest(
    *,
    most: int = 2,
    fewest: int = 1,
    locks: str = "start",
    ways: str = "[{invite: {}}, {signup: {approval: auto}}]",
    tasks: str = "  - id: sum\n",
) -> str:
    return SOON.format(
        tasks=tasks, ways=ways, entrants=f"{{most: {most}, fewest: {fewest}, locks: {locks}}}"
    )


def code(*uploads: Any) -> dict[str, SubmittedInput]:
    return {
        "submission": SubmittedInput(uploads=tuple(uploads)),
        "language": SubmittedInput(value="python"),
    }


@pytest.fixture
async def crowd(setup: Setup, acme: Acme, sum_task: TaskId) -> dict[str, Session]:
    """bob (8), carol (20) and dan (21), registered nowhere in acme/spring,
    which starts at 13:00 with the task sum published, takes entrants of up
    to two, its rosters locking at the start, and lets anyone sign up,
    approved straight away, and the people it invites.
    """
    await write_contest(acme.fake, contest())
    await publish(setup, acme, sum_task)
    acme.fake.add_user(20, "carol", email="carol@example.test")
    acme.fake.add_user(21, "dan", email="dan@example.test")
    acme.fake.reset_calls()
    return {
        "bob": await signed_in(setup, acme.fake, 8),
        "carol": await signed_in(setup, acme.fake, 20),
        "dan": await signed_in(setup, acme.fake, 21),
    }


@pytest.fixture
async def manager(setup: Setup, acme: Acme) -> Organiser:
    return await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)


async def _join(setup: Setup, inviter: Session, joiner: Session, username: str) -> invites.Accepted:
    """`inviter` asks `username` into their entrant, and `joiner` accepts."""
    made = await invites_of(setup, inviter, username)
    return await invites.accept(setup, joiner, made.id)


async def invites_of(setup: Setup, inviter: Session, username: str) -> invites.Invite:
    return await entrants.invite(setup, inviter, SPRING, username=username)


def _repo(fake: FakeForge, entrant: uuid.UUID, task: TaskId | None = None) -> Any:
    ref = parse_workspace(fake.workspaces.workspace_of(SPRING, EntrantOwner(entrant)))
    name = ref.desk_repo if task is None else ref.submission_repo(parse_task(task).task)
    return fake.state.repos.get((ref.org, name))


async def _submit(setup: Setup, acme: Acme, person: Session, task: TaskId, key: str) -> int:
    made = await upload(setup, acme.fake, person, task, f"print({key!r})\n".encode())
    submitted = await submissions.submit(setup, person, task, code(made.id), idempotency_key=key)
    return submitted.number


async def _mine(setup: Setup, person: Session) -> entrants.Entrant | None:
    return (await entrants.mine(setup, person, SPRING)).entrant


async def test_an_entrant_submits_as_one_and_every_member_sees_every_submission(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    assert (adders.name, adders.status, [m.user_id for m in adders.members]) == (
        "Adders",
        Status.APPROVED,
        [8],
    )
    joined = await _join(setup, bob, carol, "carol")
    assert joined.entered is not None
    assert (joined.entered.entrant.id, joined.entered.how) == (adders.id, "joined")
    clock.set(START + timedelta(minutes=30))

    first = await _submit(setup, acme, bob, sum_task, "key-0001-aaaa")
    clock.advance(timedelta(seconds=31))
    second = await _submit(setup, acme, carol, sum_task, "key-0002-bbbb")

    assert (first, second) == (1, 2)
    assert _repo(acme.fake, adders.id, sum_task).writers == {8, 20}
    for person in (bob, carol):
        seen = await submissions.mine(setup, person, sum_task)
        assert [made.number for made in seen] == [2, 1]


async def test_the_task_limits_count_once_for_the_whole_entrant(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    clock.set(START + timedelta(minutes=30))
    await _submit(setup, acme, bob, sum_task, "key-0001-aaaa")

    # The task takes one submission every 30 seconds, from the entrant.
    with pytest.raises(RateLimited):
        await _submit(setup, acme, carol, sum_task, "key-0002-bbbb")


async def test_a_member_joining_later_reaches_what_the_entrant_made_at_once(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(locks="first_submission"))
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    clock.set(START + timedelta(minutes=30))
    await upload(setup, acme.fake, bob, sum_task, b"print(1)\n")
    await clarifications.ask(setup, bob, SPRING, title="n?", body="How big is n?")

    await _join(setup, bob, carol, "carol")
    await identity.current(carol.id, setup=setup)

    assert _repo(acme.fake, adders.id, sum_task).writers == {8, 20}
    assert _repo(acme.fake, adders.id).readers == {8, 20}
    [question] = await clarifications.mine(setup, carol, SPRING)
    asker = f"entrant.{adders.id.hex}"
    assert question.asker == asker
    replied = await clarifications.reply(
        setup, acme.ada, SPRING, asker, question.number, body="Up to 10^5."
    )
    assert [m.from_asker for m in replied.messages] == [False]
    followed = await clarifications.follow_up(setup, carol, SPRING, question.number, body="Thanks")
    assert [m.from_asker for m in followed.messages] == [False, True]


async def test_leaving_takes_the_access_away_and_the_last_one_out_of_an_unsubmitted_entrant_ends_it(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(locks="first_submission"))
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    clock.set(START + timedelta(minutes=30))
    await upload(setup, acme.fake, carol, sum_task, b"print(1)\n")

    await entrants.leave(setup, bob, SPRING)

    assert _repo(acme.fake, adders.id, sum_task).writers == {20}
    kept = await _mine(setup, carol)
    assert kept is not None and [m.user_id for m in kept.members] == [20]
    assert await _mine(setup, bob) is None
    with pytest.raises(NotFound):
        await entrants.leave(setup, bob, SPRING)

    await entrants.leave(setup, carol, SPRING)

    assert _repo(acme.fake, adders.id, sum_task).writers == set()
    assert await _mine(setup, carol) is None
    async with setup.unit_of_work() as ctx:
        assert await ctx.db.get(Entrant, adders.id) is None


async def test_an_entrant_holds_no_more_than_the_contest_allows_invites_waiting_included(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    asked = await invites_of(setup, bob, "carol")
    assert (asked.grants, asked.entrant is not None) == (Grant.MEMBER, True)

    with pytest.raises(EntrantFull) as refused:
        await invites_of(setup, bob, "dan")
    assert refused.value.extra == {"limit": 2}

    # The most is the contest's, read at each change.
    await write_contest(acme.fake, contest(most=3))
    await invites_of(setup, bob, "dan")
    await invites.accept(setup, carol, asked.id)
    panel = await entrants.mine(setup, dan, SPRING)
    (invitation,) = panel.invitations
    await invites.accept(setup, dan, invitation.invite)
    full = await _mine(setup, bob)
    assert full is not None and sorted(m.user_id for m in full.members) == [8, 20, 21]
    assert (await entrants.mine(setup, bob, SPRING)).most == 3

    await write_contest(acme.fake, contest(most=2))
    acme.fake.add_user(22, "eve")
    eve = await signed_in(setup, acme.fake, 22)
    lone = await entrants.register(setup, eve, SPRING, name="Lone")
    await entrants.invite(setup, eve, SPRING, username="dan")
    await entrants.leave(setup, dan, SPRING)
    (late,) = (await entrants.mine(setup, dan, SPRING)).invitations
    assert late.entrant.id == lone.id


async def test_who_may_be_invited_in_and_by_whom(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    await entrants.register(setup, bob, SPRING, name="Adders")

    with pytest.raises(IsStaff):
        await invites_of(setup, bob, "ada")
    with pytest.raises(InEntrant):
        await invites_of(setup, bob, "bob")
    with pytest.raises(NotFound):
        await invites_of(setup, carol, "dan")
    await invites_of(setup, bob, "carol")
    # The person's own refusals come before the entrant's room.
    with pytest.raises(AlreadyInvited):
        await invites_of(setup, bob, "carol")
    with pytest.raises(IsStaff):
        await invites_of(setup, bob, "ada")
    with pytest.raises(EntrantFull):
        await invites_of(setup, bob, "dan")
    with pytest.raises(EntrantNameTaken):
        await entrants.register(setup, carol, SPRING, name=" adders ")
    assert dan is not None


async def test_everyone_entering_alone_invites_nobody(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    await write_contest(acme.fake, contest(most=1))
    bob = crowd["bob"]
    await entrants.register(setup, bob, SPRING)

    with pytest.raises(EntrantsAlone) as refused:
        await invites_of(setup, bob, "carol")
    assert refused.value.code == "entrants_alone"
    with pytest.raises(EntrantsAlone):
        await entrants.rename(setup, bob, SPRING, "Adders")


async def test_a_member_withdraws_the_entrants_invite_and_nobody_else_does(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    await entrants.register(setup, dan, SPRING, name="Takers")
    asked = await invites_of(setup, bob, "carol")

    with pytest.raises(NotFound):
        await entrants.withdraw_invite(setup, dan, SPRING, asked.id)
    withdrawn = await entrants.withdraw_invite(setup, bob, SPRING, asked.id)

    assert withdrawn.status is InviteStatus.WITHDRAWN
    assert (await entrants.mine(setup, carol, SPRING)).invitations == ()


async def test_the_roster_locks_at_the_start_and_then_only_organisers_change_it(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock, manager: Organiser
) -> None:
    await write_contest(acme.fake, contest(most=3))
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    asked = await invites_of(setup, bob, "carol")
    placed = await invites.create(
        setup, manager, Scope("acme", "spring"), Grant.CONTESTANT, username="dan", entrant=adders.id
    )
    assert placed.entrant is not None and placed.entrant.name == "Adders"
    clock.set(START)

    with pytest.raises(RosterLocked) as refused:
        await invites.accept(setup, carol, asked.id)
    assert refused.value.code == "roster_locked"
    for change in (
        entrants.invite(setup, bob, SPRING, username="carol"),
        entrants.leave(setup, bob, SPRING),
        entrants.rename(setup, bob, SPRING, "Subtractors"),
    ):
        with pytest.raises(RosterLocked):
            await change
    # An organiser's invite is the organisers' change.
    accepted = await invites.accept(setup, dan, placed.id)
    assert accepted.entered is not None and accepted.entered.entrant.id == adders.id
    renamed = await entrants.organise_rename(setup, manager, SPRING, adders.id, "Subtractors")
    assert (renamed.name, renamed.locked, [m.user_id for m in renamed.members]) == (
        "Subtractors",
        True,
        [8, 21],
    )
    assert [m.way_kind for m in renamed.members] == [WayKind.SIGNUP, WayKind.ORGANISER]


async def test_the_roster_locks_at_the_entrants_first_submission(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(locks="first_submission"))
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    await entrants.register(setup, dan, SPRING, name="Takers")
    clock.set(START + timedelta(minutes=30))
    await _join(setup, bob, carol, "carol")

    await _submit(setup, acme, bob, sum_task, "key-0001-aaaa")

    with pytest.raises(RosterLocked):
        await entrants.leave(setup, carol, SPRING)
    with pytest.raises(RosterLocked):
        await entrants.rename(setup, carol, SPRING, "Subtractors")
    # Another entrant's roster is its own.
    renamed = await entrants.rename(setup, dan, SPRING, "Others")
    assert (renamed.name, renamed.locked) == ("Others", False)


async def test_a_member_moves_by_accepting_another_entrants_invite_while_theirs_has_no_submissions(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(locks="first_submission"))
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    takers = await entrants.register(setup, dan, SPRING, name="Takers")
    clock.set(START + timedelta(minutes=30))
    await upload(setup, acme.fake, dan, sum_task, b"print(1)\n")

    await _join(setup, bob, dan, "dan")

    assert _repo(acme.fake, takers.id, sum_task).writers == set()
    moved = await _mine(setup, dan)
    assert moved is not None and moved.id == adders.id
    async with setup.unit_of_work() as ctx:
        assert await ctx.db.get(Entrant, takers.id) is None

    # carol's entrant has submitted, so she stays in it.
    lone = await entrants.register(setup, carol, SPRING, name="Lone")
    await _submit(setup, acme, carol, sum_task, "key-0003-cccc")
    await write_contest(acme.fake, contest(most=3, locks="first_submission"))
    asked = await invites_of(setup, bob, "carol")
    with pytest.raises(InEntrant) as refused:
        await invites.accept(setup, carol, asked.id)
    assert refused.value.code == "in_entrant"
    stayed = await _mine(setup, carol)
    assert stayed is not None and stayed.id == lone.id


async def test_an_entrant_complete_with_every_member_in_by_an_automatic_way_is_approved(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    await write_contest(acme.fake, contest(fewest=2))
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    assert (adders.status, adders.complete) == (Status.PENDING, False)

    with pytest.raises(EntrantIncomplete) as refused:
        await entrants.approve(setup, manager, SPRING, adders.id)
    assert refused.value.extra == {"fewest": 2, "members": 1}
    joined = await _join(setup, bob, carol, "carol")

    assert joined.entered is not None and joined.entered.status is Status.APPROVED


async def test_someone_joining_by_a_way_an_organiser_approves_sends_the_entrant_back_to_pending(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    acme.fake.add_user(22, "eve", email="eve@u.nus.edu")
    await write_contest(
        acme.fake,
        contest(
            ways=(
                "[{signup: {name: Students, domains: [u.nus.edu], approval: auto}}, "
                "{signup: {name: Guests}}]"
            )
        ),
    )
    eve, dan = await signed_in(setup, acme.fake, 22), crowd["dan"]
    adders = await entrants.register(setup, eve, SPRING, name="Adders")
    assert adders.status is Status.APPROVED

    joined = await _join(setup, eve, dan, "dan")

    assert joined.entered is not None and joined.entered.status is Status.PENDING
    pending = await _mine(setup, eve)
    assert pending is not None
    assert [(m.user_id, m.way_name) for m in pending.members] == [(22, "Students"), (21, "Guests")]
    approved = await entrants.approve(setup, manager, SPRING, adders.id)
    assert approved.status is Status.APPROVED


async def test_a_submit_is_refused_once_a_member_left_the_entrant_short(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(fewest=2, locks="first_submission"))
    bob, carol = crowd["bob"], crowd["carol"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    clock.set(START + timedelta(minutes=30))

    await entrants.leave(setup, carol, SPRING)

    with pytest.raises(EntrantIncomplete) as refused:
        await _submit(setup, acme, bob, sum_task, "key-0001-aaaa")
    assert refused.value.code == "entrant_incomplete"


async def test_organisers_move_take_out_rename_and_delete(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    crowd: dict[str, Session],
    clock: FakeClock,
    manager: Organiser,
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    takers = await entrants.register(setup, dan, SPRING, name="Takers")
    clock.set(START + timedelta(minutes=30))
    await _submit(setup, acme, carol, sum_task, "key-0001-aaaa")

    moved = await entrants.move_member(setup, manager, SPRING, adders.id, 20, takers.id)
    assert sorted(m.user_id for m in moved.members) == [20, 21]
    assert [m.way_kind for m in moved.members if m.user_id == 20] == [WayKind.ORGANISER]
    assert _repo(acme.fake, adders.id, sum_task).writers == {8}
    with pytest.raises(EntrantHasSubmissions):
        await entrants.move_member(setup, manager, SPRING, adders.id, 8, takers.id)
    acme.fake.add_user(22, "eve")
    eve = await signed_in(setup, acme.fake, 22)
    third = await entrants.register(setup, eve, SPRING, name="Third")
    with pytest.raises(EntrantFull):
        await entrants.move_member(setup, manager, SPRING, third.id, 22, takers.id)
    with pytest.raises(EntrantHasSubmissions):
        await entrants.delete(setup, manager, SPRING, adders.id)

    # The last member out of an entrant that submitted leaves it standing.
    standing = await entrants.remove_member(setup, manager, SPRING, adders.id, 8)
    assert standing is not None and standing.members == () and standing.submitted
    assert _repo(acme.fake, adders.id, sum_task).writers == set()
    await entrants.delete(setup, manager, SPRING, third.id)
    assert await _mine(setup, eve) is None
    names = [entrant.display_name for entrant in await entrants.list(setup, manager, SPRING)]
    assert names == ["Adders", "Takers"]


async def test_an_organiser_takes_the_last_member_out_of_an_entrant_that_never_submitted(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")

    assert await entrants.remove_member(setup, manager, SPRING, adders.id, 8) is None
    assert await entrants.list(setup, manager, SPRING) == ()
    with pytest.raises(NotFound):
        await entrants.remove_member(setup, manager, SPRING, adders.id, 8)


TIMED = contest(
    tasks=(
        "  - {id: sum, closes: 2026-09-26T16:00:00Z}\n"
        "  - {id: other, closes: 2026-09-26T16:00:00Z}\n"
        "  - {id: third, closes: 2026-09-26T16:00:00Z}\n"
    )
)


async def test_an_organiser_extends_an_entrant_on_the_named_tasks_alone(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    crowd: dict[str, Session],
    clock: FakeClock,
    manager: Organiser,
) -> None:
    await write_contest(acme.fake, TIMED)
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    takers = await entrants.register(setup, dan, SPRING, name="Takers")

    with pytest.raises(InvalidExtension):
        await entrants.extend(setup, manager, SPRING, adders.id, timedelta(hours=1), tasks=["nope"])
    extended = await entrants.extend(
        setup, manager, SPRING, adders.id, timedelta(hours=1), tasks=["sum"]
    )
    await entrants.extend(setup, manager, SPRING, takers.id, timedelta(hours=1), tasks=["other"])
    clock.set(START + timedelta(hours=3, minutes=30))

    assert (extended.time_extension, extended.extension_tasks) == (timedelta(hours=1), ("sum",))
    assert await _submit(setup, acme, carol, sum_task, "key-0001-aaaa") == 1
    with pytest.raises(TaskClosed) as refused:
        await _submit(setup, acme, dan, sum_task, "key-0002-bbbb")
    assert refused.value.extra == {"reason": "closed"}
    with pytest.raises(InvalidExtension, match="third's hidden results were shown"):
        await entrants.extend(setup, manager, SPRING, takers.id, timedelta(hours=1))
    made = (
        "sum's submission 1, made on 26 September 2026 at 16:30 UTC, would then be after the "
        "task closed"
    )
    with pytest.raises(InvalidExtension, match=re.escape(made)):
        await entrants.extend(
            setup, manager, SPRING, adders.id, timedelta(minutes=20), tasks=["sum"]
        )


async def _reveal(setup: Setup) -> datetime | None:
    """When sum reveals, by the extensions in force now."""
    async with setup.unit_of_work() as ctx:
        settings = await published.contest(ctx, SPRING)
        return await timelines.reveal(ctx, SPRING, settings, "sum")


async def test_only_an_approved_entrants_extension_holds_the_reveal_back(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    await write_contest(
        acme.fake, TIMED.replace("{signup: {approval: auto}}", "{signup: {approval: manual}}")
    )
    closes = datetime(2026, 9, 26, 16, tzinfo=UTC)
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    await entrants.extend(setup, manager, SPRING, adders.id, timedelta(hours=2), tasks=["sum"])
    assert await _reveal(setup) == closes

    await entrants.approve(setup, manager, SPRING, adders.id)
    assert await _reveal(setup) == closes + timedelta(hours=2)


async def test_a_place_made_with_old_members_is_put_in_step_with_the_entrant(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session]
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    workspace = acme.fake.workspaces.workspace_of(SPRING, EntrantOwner(adders.id))

    # Made as if carol had not joined yet and dan were still in.
    await acme.fake.workspaces.open_submission_place(workspace, sum_task, [8])
    _repo(acme.fake, adders.id, sum_task).writers.add(21)
    async with setup.unit_of_work() as ctx:
        current = await entrants.settle(ctx, adders.id, workspace, (8, 21))

    assert sorted(current) == [8, 20]
    assert _repo(acme.fake, adders.id, sum_task).writers == {8, 20}


async def test_a_members_stream_hears_of_the_entrants_gradings(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    heard = await live.audience(setup, crowd["bob"])
    assert heard.entrants == frozenset({str(adders.id)})
    assert ContestId("acme/spring") in heard.contests


async def test_someone_who_left_uploads_into_the_place_of_the_entrant_they_are_in_now(
    setup: Setup, acme: Acme, sum_task: TaskId, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(locks="first_submission"))
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    clock.set(START + timedelta(minutes=30))
    await upload(setup, acme.fake, bob, sum_task, b"print(1)\n")
    await entrants.leave(setup, bob, SPRING)
    alone = await entrants.register(setup, bob, SPRING, name="Alone")

    await upload(setup, acme.fake, bob, sum_task, b"print(2)\n", filename="b.py")

    assert _repo(acme.fake, alone.id, sum_task).writers == {8}
    assert _repo(acme.fake, adders.id, sum_task).writers == {20}


async def test_entrants_stand_once_the_contest_ends_but_organisers_still_mend_them(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock, manager: Organiser
) -> None:
    await write_contest(acme.fake, contest(locks="first_submission"))
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    takers = await entrants.register(setup, crowd["dan"], SPRING, name="Takers")
    clock.set(START + timedelta(hours=6))

    with pytest.raises(RosterLocked):
        await entrants.leave(setup, crowd["bob"], SPRING)
    moved = await entrants.move_member(setup, manager, SPRING, takers.id, 21, adders.id)
    assert sorted(m.user_id for m in moved.members) == [8, 21]


async def test_an_organisers_invite_into_an_entrant_needs_the_invite_way_and_room(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    scope = Scope("acme", "spring")
    await invites.create(
        setup, manager, scope, Grant.CONTESTANT, username="carol", entrant=adders.id
    )

    with pytest.raises(EntrantFull):
        await invites.create(
            setup, manager, scope, Grant.CONTESTANT, username="dan", entrant=adders.id
        )
    with pytest.raises(NotFound):
        await invites.create(
            setup, manager, scope, Grant.CONTESTANT, username="dan", entrant=uuid.uuid4()
        )
    await write_contest(acme.fake, contest(ways="[{signup: {}}]"))
    with pytest.raises(NoInviteWay) as refused:
        await invites.create(setup, manager, scope, Grant.CONTESTANT, username="dan")
    assert refused.value.code == "no_invite_way"


async def test_nobody_outside_the_entrant_acts_for_it(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    outsider = await signed_in(setup, acme.fake, 7)

    with pytest.raises(NotFound):
        await entrants.leave(setup, outsider, SPRING)
    with pytest.raises(NotFound):
        await entrants.rename(setup, crowd["carol"], SPRING, "Mine now")
    with pytest.raises(Forbidden):
        await organiser(setup, acme.fake, 8, Scope("acme", "spring"))


async def _invite_status(setup: Setup, manager: Organiser, invite: uuid.UUID) -> InviteStatus:
    listed = await invites.at(setup, manager, Scope("acme", "spring"))
    return next(found.status for found in listed if found.id == invite)


async def test_an_organisers_invite_into_an_entrant_is_an_organisers_change_and_never_reopens_it(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock, manager: Organiser
) -> None:
    await write_contest(
        acme.fake,
        contest(most=3, ways="[{invite: {approval: manual}}, {signup: {approval: auto}}]"),
    )
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    assert adders.status is Status.APPROVED
    placed = await invites.create(
        setup, manager, Scope("acme", "spring"), Grant.CONTESTANT, username="dan", entrant=adders.id
    )
    clock.set(START + timedelta(hours=1))

    accepted = await invites.accept(setup, crowd["dan"], placed.id)

    assert accepted.entered is not None and accepted.entered.status is Status.APPROVED
    mine = await _mine(setup, crowd["bob"])
    assert mine is not None
    assert [(m.user_id, m.way_kind) for m in mine.members] == [
        (8, WayKind.SIGNUP),
        (21, WayKind.ORGANISER),
    ]


async def test_an_organisers_invite_is_accepted_only_while_the_contest_is_open(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock, manager: Organiser
) -> None:
    await write_contest(acme.fake, contest(most=3))
    scope = Scope("acme", "spring")
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    alone = await invites.create(
        setup, manager, scope, Grant.CONTESTANT, username="dan", lifetime=timedelta(days=30)
    )
    into = await invites.create(
        setup,
        manager,
        scope,
        Grant.CONTESTANT,
        username="carol",
        entrant=adders.id,
        lifetime=timedelta(days=30),
    )

    # Past the end, and archived or a draft before it, nobody takes a place.
    clock.set(START + timedelta(days=2))
    for person, invite in ((crowd["dan"], alone), (crowd["carol"], into)):
        with pytest.raises(RegistrationClosed) as ended:
            await invites.accept(setup, person, invite.id)
        assert ended.value.code == "registration_closed"
        assert await _invite_status(setup, manager, invite.id) is InviteStatus.PENDING
    clock.set(START + timedelta(hours=1))
    for state in ("archived", "draft"):
        await write_contest(
            acme.fake, contest(most=3).replace("state: published", f"state: {state}")
        )
        for person, invite in ((crowd["dan"], alone), (crowd["carol"], into)):
            with pytest.raises(RegistrationClosed):
                await invites.accept(setup, person, invite.id)

    await write_contest(acme.fake, contest(most=3))
    placed = await invites.accept(setup, crowd["dan"], alone.id)
    assert placed.entered is not None and placed.entered.how == "made"
    joined = await invites.accept(setup, crowd["carol"], into.id)
    assert joined.entered is not None and joined.entered.entrant.id == adders.id


async def test_no_new_team_registers_once_teams_are_fixed_and_one_alone_still_may(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(most=3, fewest=2))
    clock.set(START + timedelta(minutes=5))

    panel = await entrants.mine(setup, crowd["bob"], SPRING)
    assert panel.can_register is False
    assert panel.refused is not None and panel.refused.code == "registration_closed"
    with pytest.raises(RegistrationClosed, match="Teams were fixed when the contest started"):
        await entrants.register(setup, crowd["bob"], SPRING, name="Late")

    # Teams fix at the first submission: a new one has none, so it is open.
    await write_contest(acme.fake, contest(most=3, fewest=2, locks="first_submission"))
    late = await entrants.register(setup, crowd["bob"], SPRING, name="Late")
    assert late.locked is False
    await write_contest(acme.fake, contest(most=3, fewest=1))
    alone = await entrants.register(setup, crowd["carol"], SPRING, name="Alone")
    assert alone.status is Status.APPROVED


async def test_a_submit_reads_its_entrant_again_once_it_holds_it(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    crowd: dict[str, Session],
    clock: FakeClock,
    manager: Organiser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bob = crowd["bob"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await entrants.extend(setup, manager, SPRING, adders.id, timedelta(hours=2))
    clock.set(START + timedelta(hours=5, minutes=30))
    made = await upload(setup, acme.fake, bob, sum_task, b"print(1)\n")
    holding = entrants.hold_for_reading

    async def taken_away_first(ctx: Any, entrant: uuid.UUID) -> Any:
        # The organiser's change commits while the submit waits for the row.
        await entrants.extend(setup, manager, SPRING, adders.id, timedelta(0))
        return await holding(ctx, entrant)

    monkeypatch.setattr(entrants, "hold_for_reading", taken_away_first)

    with pytest.raises(TaskClosed):
        await submissions.submit(
            setup, bob, sum_task, code(made.id), idempotency_key="key-0001-race"
        )


async def test_a_team_sends_ten_invites_a_day_of_its_own_and_never_the_orgs(
    setup: Setup,
    acme: Acme,
    crowd: dict[str, Session],
    clock: FakeClock,
    manager: Organiser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invites, "ORG_DAILY_MAX", 3)
    clock.set(START - timedelta(days=3))
    bob = crowd["bob"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    for number in range(10):
        made = await entrants.invite(setup, bob, SPRING, email=f"someone{number}@example.test")
        await entrants.withdraw_invite(setup, bob, SPRING, made.id)

    with pytest.raises(InviteLimit) as refused:
        await entrants.invite(setup, bob, SPRING, email="one-more@example.test")
    assert refused.value.code == "invite_limit"
    # The org's own limit counts only its organisers' invites.
    await invites.create(setup, manager, Scope("acme", "spring"), Grant.CONTESTANT, username="dan")
    clock.advance(timedelta(days=1, seconds=1))
    await entrants.invite(setup, bob, SPRING, email="one-more@example.test")


async def test_the_last_one_out_and_an_accept_of_the_entrants_invite_take_turns(
    setup: Setup, acme: Acme, crowd: dict[str, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    asked = await invites_of(setup, bob, "carol")
    closing = entrants._close
    accepting: list[asyncio.Future[Any]] = []

    async def slow_close(ctx: Any, workspace: Any, user_ids: Any) -> None:
        # bob's leave is under way; carol accepts the entrant's invite now.
        accepting.append(asyncio.ensure_future(invites.accept(setup, carol, asked.id)))
        await asyncio.sleep(1.0)
        await closing(ctx, workspace, user_ids)

    monkeypatch.setattr(entrants, "_close", slow_close)

    await entrants.leave(setup, bob, SPRING)
    (accepted,) = await asyncio.gather(*accepting, return_exceptions=True)

    assert isinstance(accepted, WrongStatus), accepted
    assert await _mine(setup, carol) is None


async def test_an_entrant_is_approved_on_its_own_once_the_member_holding_it_back_is_gone(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    acme.fake.add_user(22, "eve", email="eve@u.nus.edu")
    await write_contest(
        acme.fake,
        contest(
            most=3,
            ways=(
                "[{signup: {name: Students, domains: [u.nus.edu], approval: auto}}, "
                "{signup: {name: Guests}}]"
            ),
        ),
    )
    eve, dan, carol = await signed_in(setup, acme.fake, 22), crowd["dan"], crowd["carol"]
    adders = await entrants.register(setup, eve, SPRING, name="Adders")
    await _join(setup, eve, dan, "dan")
    await _join(setup, eve, carol, "carol")

    await entrants.leave(setup, dan, SPRING)
    still = await _mine(setup, eve)
    assert still is not None and still.status is Status.PENDING

    removed = await entrants.remove_member(setup, manager, SPRING, adders.id, 20)
    assert removed is not None and removed.status is Status.APPROVED


async def test_a_move_the_forge_fails_halfway_leaves_access_as_the_membership_stands(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    crowd: dict[str, Session],
    clock: FakeClock,
    manager: Organiser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    takers = await entrants.register(setup, dan, SPRING, name="Takers")
    clock.set(START + timedelta(minutes=30))
    await upload(setup, acme.fake, carol, sum_task, b"print(1)\n")
    await upload(setup, acme.fake, dan, sum_task, b"print(2)\n")
    workspaces = acme.fake.workspaces
    sharing = workspaces.share_workspace

    async def down_for_takers(workspace: Any, member_ids: Any) -> None:
        if str(takers.id.hex) in workspace:
            raise Unavailable("the forge did not answer")
        await sharing(workspace, member_ids)

    monkeypatch.setattr(workspaces, "share_workspace", down_for_takers)

    with pytest.raises(Unavailable):
        await entrants.move_member(setup, manager, SPRING, adders.id, 20, takers.id)

    assert _repo(acme.fake, adders.id, sum_task).writers == {8, 20}
    assert _repo(acme.fake, takers.id, sum_task).writers == {21}
    stayed = await _mine(setup, carol)
    assert stayed is not None and stayed.id == adders.id

    # The other way round: the source's access fails to go.
    monkeypatch.setattr(workspaces, "share_workspace", sharing)
    closing = workspaces.close_workspace

    async def down_for_adders(workspace: Any, member_ids: Any) -> None:
        if str(adders.id.hex) in workspace:
            raise Unavailable("the forge did not answer")
        await closing(workspace, member_ids)

    monkeypatch.setattr(workspaces, "close_workspace", down_for_adders)

    with pytest.raises(Unavailable):
        await entrants.move_member(setup, manager, SPRING, adders.id, 20, takers.id)

    assert _repo(acme.fake, adders.id, sum_task).writers == {8, 20}
    assert _repo(acme.fake, takers.id, sum_task).writers == {21}


async def test_a_rejected_entrant_loses_its_access_and_has_it_again_once_reopened(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    crowd: dict[str, Session],
    clock: FakeClock,
    manager: Organiser,
) -> None:
    acme.fake.add_user(22, "eve", email="eve@u.nus.edu")
    await write_contest(
        acme.fake,
        contest(
            locks="first_submission",
            ways=(
                "[{signup: {name: Students, domains: [u.nus.edu], approval: auto}}, "
                "{signup: {name: Guests}}]"
            ),
        ),
    )
    eve = await signed_in(setup, acme.fake, 22)
    adders = await entrants.register(setup, eve, SPRING, name="Adders")
    clock.set(START + timedelta(minutes=30))
    await upload(setup, acme.fake, eve, sum_task, b"print(1)\n")
    await _join(setup, eve, crowd["dan"], "dan")

    rejected = await entrants.reject(setup, manager, SPRING, adders.id, "Guests must be named.")

    assert rejected.status is Status.REJECTED
    assert _repo(acme.fake, adders.id, sum_task).writers == set()
    await entrants.reopen(setup, manager, SPRING, adders.id)
    assert _repo(acme.fake, adders.id, sum_task).writers == {21, 22}


async def test_an_organiser_moves_people_only_into_an_entrant_that_stands_and_never_staff(
    setup: Setup, acme: Acme, crowd: dict[str, Session], manager: Organiser
) -> None:
    await write_contest(
        acme.fake, TIMED.replace("{signup: {approval: auto}}", "{signup: {approval: manual}}")
    )
    adders = await entrants.register(setup, crowd["bob"], SPRING, name="Adders")
    lone = await entrants.register(setup, crowd["carol"], SPRING, name="Lone")
    takers = await entrants.register(setup, crowd["dan"], SPRING, name="Takers")
    await entrants.reject(setup, manager, SPRING, takers.id, "Not this time.")

    with pytest.raises(WrongStatus):
        await entrants.move_member(setup, manager, SPRING, adders.id, 8, takers.id)
    await entrants.reject(setup, manager, SPRING, lone.id, "Not this time.")
    await acme.fake.orgs.grant_role(20, Scope("acme", "spring"), Role.OBSERVER)
    with pytest.raises(IsStaff):
        await entrants.move_member(setup, manager, SPRING, lone.id, 20, adders.id)


async def test_deleting_an_entrant_someone_joined_meanwhile_is_asked_again(
    setup: Setup,
    acme: Acme,
    crowd: dict[str, Session],
    manager: Organiser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    adders = await entrants.register(setup, bob, SPRING, name="Adders")
    asked = await invites_of(setup, bob, "carol")
    holding = entrants._hold_people
    joined: list[bool] = []

    async def carol_joins_first(ctx: Any, contest: Any, user_ids: Any) -> None:
        if not joined:
            joined.append(True)
            await invites.accept(setup, carol, asked.id)
        await holding(ctx, contest, user_ids)

    monkeypatch.setattr(entrants, "_hold_people", carol_joins_first)

    with pytest.raises(EntrantChanged):
        await entrants.delete(setup, manager, SPRING, adders.id)
    kept = await _mine(setup, carol)
    assert kept is not None and kept.id == adders.id


async def test_a_deleted_account_leaves_every_team_still_open_and_stays_in_one_fixed(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(most=3))
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    await entrants.register(setup, bob, SPRING, name="Adders")
    await _join(setup, bob, carol, "carol")
    await entrants.invite(setup, carol, SPRING, email="friend@example.test")
    takers = await entrants.register(setup, dan, SPRING, name="Takers")

    await account.delete(setup, carol)

    adders = await _mine(setup, bob)
    assert adders is not None and [m.user_id for m in adders.members] == [8]
    assert adders.invites == ()
    clock.set(START + timedelta(hours=1))
    await account.delete(setup, await signed_in(setup, acme.fake, 21))
    async with setup.unit_of_work() as ctx:
        assert await entrants.member_ids(ctx, takers.id) == [21]


async def test_the_panel_says_the_addresses_could_not_be_checked_when_the_forge_did_not_answer(
    setup: Setup, acme: Acme, crowd: dict[str, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    await write_contest(acme.fake, contest(ways="[{signup: {domains: [u.nus.edu]}}]"))

    async def unanswered(user_id: int) -> tuple[str, ...]:
        raise Unavailable("the forge did not answer")

    monkeypatch.setattr(acme.fake.identity, "verified_emails", unanswered)

    panel = await entrants.mine(setup, crowd["bob"], SPRING)

    assert panel.can_register is False
    assert panel.refused is not None and panel.refused.code == "forge_unavailable"
    assert "addresses" in panel.refused.detail and "Try again" in panel.refused.detail
