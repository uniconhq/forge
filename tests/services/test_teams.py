"""Teams in a contest whose `team_size: N` turns them on. An approved
contestant makes a team and leads it, or asks to join one; the leader asks
people in, lets requests in and removes members, up to the contest's
`team_size`; a member leaves. Organisers make, delete and mend teams, and
extend one on the tasks they name. The team is the contestant: one
workspace its members reach as the membership changes, in the request that
changes it, one count of submissions, one extension, and one desk of
questions.
"""

import re
from datetime import timedelta
from typing import Any

import pytest

from forge.db.tables import Team as TeamRow
from forge.domain.errors import (
    Forbidden,
    InTeam,
    InvalidExtension,
    NotApproved,
    NotFound,
    SubmittedAlone,
    TaskClosed,
    TeamFull,
    TeamHasSubmissions,
    TeamNameTaken,
    TeamsOff,
)
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import TeamOwner, UserOwner
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.forges.fake import FakeForge
from forge.forges.ids import parse_task, parse_workspace
from forge.runtime.setup import Setup
from forge.services import clarifications, contestants, live, submissions, teams
from forge.testing import FakeClock
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    organiser,
    signed_in,
    upload,
    write_contest,
)

TEAMS = RUNNING + "team_size: {size}\n"


def code(*uploads: Any) -> dict[str, SubmittedInput]:
    return {
        "submission": SubmittedInput(uploads=tuple(uploads)),
        "language": SubmittedInput(value="python"),
    }


async def _enter(setup: Setup, acme: Acme, user_id: int, name: str) -> Session:
    acme.fake.add_user(user_id, name, email=f"{name}@example.test")
    person = await signed_in(setup, acme.fake, user_id)
    await contestants.register(setup, person, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, user_id)
    return person


@pytest.fixture
async def crowd(setup: Setup, acme: Acme, entered: Entered) -> dict[str, Session]:
    """bob (8), carol (20) and dan (21), approved in acme/spring with teams of
    at most two.
    """
    await write_contest(acme.fake, TEAMS.format(visibility="everyone", size=2))
    return {
        "bob": entered.session,
        "carol": await _enter(setup, acme, 20, "carol"),
        "dan": await _enter(setup, acme, 21, "dan"),
    }


def _repo(fake: FakeForge, owner: TeamOwner | UserOwner, task: TaskId | None = None) -> Any:
    ref = parse_workspace(fake.workspaces.workspace_of(SPRING, owner))
    name = ref.desk_repo if task is None else ref.submission_repo(parse_task(task).task)
    return fake.state.repos.get((ref.org, name))


async def _submit(setup: Setup, acme: Acme, person: Session, task: TaskId, key: str) -> int:
    made = await upload(setup, acme.fake, person, task, f"print({key!r})\n".encode())
    submitted = await submissions.submit(setup, person, task, code(made.id), idempotency_key=key)
    return submitted.number


async def test_a_team_submits_as_one_and_every_member_sees_every_submission(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session], clock: FakeClock
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    assert (team.name, team.leader, [m.user_id for m in team.members]) == ("Adders", 8, [8])

    await teams.request(setup, carol, SPRING, team.id)
    mine = await teams.mine(setup, bob, SPRING)
    assert mine.team is not None and [m.user_id for m in mine.team.pending] == [20]
    joined = await teams.approve(setup, bob, SPRING, team.id, 20)
    assert sorted(m.user_id for m in joined.members) == [8, 20]

    first = await _submit(setup, acme, bob, entered.task, "key-0001-aaaa")
    clock.advance(timedelta(seconds=31))
    second = await _submit(setup, acme, carol, entered.task, "key-0002-bbbb")

    assert (first, second) == (1, 2)
    owner = TeamOwner(team.id)
    assert _repo(acme.fake, owner, entered.task).writers == {8, 20}
    for person in (bob, carol):
        seen = await submissions.mine(setup, person, entered.task)
        assert [made.number for made in seen] == [2, 1]
    assert _repo(acme.fake, UserOwner(8), entered.task) is None


async def test_the_task_limits_count_once_for_the_whole_team(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, TEAMS.format(visibility="everyone", size=2))
    bob, carol = crowd["bob"], crowd["carol"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await teams.invite(setup, bob, SPRING, team.id, "carol")
    await teams.request(setup, carol, SPRING, team.id)
    await _submit(setup, acme, bob, entered.task, "key-0001-aaaa")

    # The task takes one submission every 30 seconds, from the team.
    from forge.domain.errors import RateLimited

    with pytest.raises(RateLimited):
        await _submit(setup, acme, carol, entered.task, "key-0002-bbbb")


async def test_a_member_joining_later_reaches_what_the_team_made_at_once(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await _submit(setup, acme, bob, entered.task, "key-0001-aaaa")
    await clarifications.ask(setup, bob, SPRING, title="n?", body="How big is n?")

    await teams.invite(setup, bob, SPRING, team.id, "carol")
    await teams.request(setup, carol, SPRING, team.id)

    owner = TeamOwner(team.id)
    assert _repo(acme.fake, owner, entered.task).writers == {8, 20}
    assert _repo(acme.fake, owner).readers == {8, 20}
    [question] = await clarifications.mine(setup, carol, SPRING)
    assert question.asker == f"team.{team.id}"
    replied = await clarifications.reply(
        setup, acme.ada, SPRING, f"team.{team.id}", question.number, body="Up to 10^5."
    )
    assert [m.from_asker for m in replied.messages] == [False]
    followed = await clarifications.follow_up(setup, carol, SPRING, question.number, body="Thanks")
    assert [m.from_asker for m in followed.messages] == [False, True]


async def test_leaving_takes_the_access_away_and_hands_the_lead_on(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await teams.request(setup, carol, SPRING, team.id)
    await teams.approve(setup, bob, SPRING, team.id, 20)
    await _submit(setup, acme, bob, entered.task, "key-0001-aaaa")

    await teams.leave(setup, bob, SPRING)

    owner = TeamOwner(team.id)
    assert _repo(acme.fake, owner, entered.task).writers == {20}
    mine = await teams.mine(setup, carol, SPRING)
    assert mine.team is not None and mine.team.leader == 20
    assert (await teams.mine(setup, bob, SPRING)).team is None
    # bob submits on his own from now, and the team keeps what it made.
    assert await submissions.mine(setup, bob, entered.task) == ()

    # The last one out of a team that submitted leaves it standing, empty.
    await teams.leave(setup, carol, SPRING)
    [kept] = await teams.listed(setup, carol, SPRING)
    assert (kept.id, kept.size, kept.leader) == (team.id, 0, None)


async def test_an_empty_team_that_never_submitted_goes(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    team = await teams.create(setup, crowd["bob"], SPRING, "Adders")
    await teams.leave(setup, crowd["bob"], SPRING)
    assert await teams.listed(setup, crowd["carol"], SPRING) == ()
    async with setup.unit_of_work() as ctx:
        assert await ctx.db.get(TeamRow, team.id) is None


async def test_a_team_holds_no_more_than_the_contest_allows(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await teams.request(setup, carol, SPRING, team.id)
    await teams.request(setup, dan, SPRING, team.id)
    await teams.approve(setup, bob, SPRING, team.id, 20)

    with pytest.raises(TeamFull) as refused:
        await teams.approve(setup, bob, SPRING, team.id, 21)
    assert refused.value.extra == {"limit": 2}
    with pytest.raises(TeamFull):
        await teams.invite(setup, bob, SPRING, team.id, "dan")
    assert (await teams.mine(setup, bob, SPRING)).max_size == 2

    # The cap is the contest's team_size, read at each change.
    await write_contest(acme.fake, TEAMS.format(visibility="everyone", size=3))
    joined = await teams.approve(setup, bob, SPRING, team.id, 21)
    assert sorted(m.user_id for m in joined.members) == [8, 20, 21]
    assert (await teams.mine(setup, bob, SPRING)).max_size == 3


async def test_who_may_join_and_who_may_run_a_team(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    await _submit(setup, acme, dan, entered.task, "key-0009-dddd")
    team = await teams.create(setup, bob, SPRING, "Adders")

    with pytest.raises(SubmittedAlone):
        await teams.request(setup, dan, SPRING, team.id)
    with pytest.raises(TeamNameTaken):
        await teams.create(setup, carol, SPRING, " adders ")
    with pytest.raises(InTeam):
        await teams.create(setup, bob, SPRING, "Second")
    await teams.request(setup, carol, SPRING, team.id)
    with pytest.raises(InTeam):
        await teams.request(setup, carol, SPRING, team.id)
    with pytest.raises(Forbidden):
        await teams.approve(setup, carol, SPRING, team.id, 20)
    with pytest.raises(Forbidden):
        await teams.remove(setup, bob, SPRING, team.id, 8)
    outsider = await signed_in(setup, acme.fake, 7)
    with pytest.raises(NotApproved):
        await teams.create(setup, outsider, SPRING, "Staff")

    # A refused request is gone, and nothing was given.
    await teams.remove(setup, bob, SPRING, team.id, 20)
    assert (await teams.mine(setup, carol, SPRING)).requested == ()


async def test_a_contest_without_teams_has_none(setup: Setup, acme: Acme, entered: Entered) -> None:
    with pytest.raises(TeamsOff):
        await teams.create(setup, entered.session, SPRING, "Adders")


async def test_organisers_make_delete_move_and_lead(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    carol = crowd["carol"]
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    first = await teams.organise_create(setup, manager, SPRING, "First", leader="bob")
    second = await teams.organise_create(setup, manager, SPRING, "Second")
    await teams.organise_move(setup, manager, SPRING, 20, first.id)
    await _submit(setup, acme, carol, entered.task, "key-0001-aaaa")

    moved = await teams.organise_move(setup, manager, SPRING, 20, second.id)
    assert [m.user_id for m in moved.members] == [20]
    assert moved.leader == 20
    assert _repo(acme.fake, TeamOwner(first.id), entered.task).writers == {8}
    led = await teams.organise_lead(setup, manager, SPRING, first.id, 8)
    assert led.leader == 8

    with pytest.raises(TeamHasSubmissions):
        await teams.organise_delete(setup, manager, SPRING, first.id)
    empty = await teams.organise_create(setup, manager, SPRING, "Third")
    await teams.organise_delete(setup, manager, SPRING, empty.id)
    names = [team.name for team in await teams.every(setup, manager, SPRING)]
    assert names == ["First", "Second"]

    # Taking the last member out of a team that never submitted deletes it.
    await teams.organise_remove(setup, manager, SPRING, second.id, 20)
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)
    assert [team.name for team in await teams.every(setup, observer, SPRING)] == ["First"]


TIMED = (
    RUNNING.format(visibility="everyone").replace(
        "  - id: sum\n", "  - {id: sum, closes: 2026-09-26T13:00:00Z}\n"
    )
    + "  - {id: other, closes: 2026-09-26T13:00:00Z}\n"
    + "  - {id: third, closes: 2026-09-26T13:00:00Z}\n"
    + "team_size: 2\n"
)


async def test_an_organiser_extends_a_team_on_the_named_tasks_alone(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session], clock: FakeClock
) -> None:
    await write_contest(acme.fake, TIMED)
    carol, dan = crowd["carol"], crowd["dan"]
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    adders = await teams.organise_create(setup, manager, SPRING, "Adders", leader="bob")
    await teams.organise_move(setup, manager, SPRING, 20, adders.id)
    takers = await teams.organise_create(setup, manager, SPRING, "Takers", leader="dan")

    with pytest.raises(InvalidExtension):
        await teams.organise_extend(
            setup, manager, SPRING, adders.id, timedelta(hours=1), tasks=["nope"]
        )
    extended = await teams.organise_extend(
        setup, manager, SPRING, adders.id, timedelta(hours=1), tasks=["sum"]
    )
    await teams.organise_extend(
        setup, manager, SPRING, takers.id, timedelta(hours=1), tasks=["other"]
    )
    clock.advance(timedelta(hours=1, minutes=30))

    assert (extended.time_extension, extended.extension_tasks) == (timedelta(hours=1), ("sum",))
    assert await _submit(setup, acme, carol, entered.task, "key-0001-aaaa") == 1
    with pytest.raises(TaskClosed) as refused:
        await _submit(setup, acme, dan, entered.task, "key-0002-bbbb")
    assert refused.value.extra == {"reason": "closed"}
    with pytest.raises(InvalidExtension, match="third's hidden results were shown"):
        await teams.organise_extend(setup, manager, SPRING, takers.id, timedelta(hours=1))
    made = "sum's submission 1, made 2026-09-26T13:30:00+00:00 would be after the task closed"
    with pytest.raises(InvalidExtension, match=re.escape(made)):
        await teams.organise_extend(
            setup, manager, SPRING, adders.id, timedelta(minutes=20), tasks=["sum"]
        )


async def test_a_contestant_removed_from_the_contest_leaves_their_team(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    bob, carol = crowd["bob"], crowd["carol"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await teams.request(setup, carol, SPRING, team.id)
    await teams.approve(setup, bob, SPRING, team.id, 20)
    await _submit(setup, acme, bob, entered.task, "key-0001-aaaa")
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)

    await contestants.remove(setup, manager, SPRING, 20)

    assert _repo(acme.fake, TeamOwner(team.id), entered.task).writers == {8}
    with pytest.raises(NotFound):
        await teams.leave(setup, carol, SPRING)


async def test_a_place_made_with_old_members_is_put_in_step_with_the_team(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    bob = crowd["bob"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await teams.organise_move(
        setup,
        await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER),
        SPRING,
        20,
        team.id,
    )
    workspace = acme.fake.workspaces.workspace_of(SPRING, TeamOwner(team.id))

    # Made as if carol had not joined yet and dan were still in.
    await acme.fake.workspaces.open_submission_place(workspace, entered.task, [8])
    _repo(acme.fake, TeamOwner(team.id), entered.task).writers.add(21)
    async with setup.unit_of_work() as ctx:
        current = await teams.settle(ctx, team.id, workspace, (8, 21))

    assert sorted(current) == [8, 20]
    assert _repo(acme.fake, TeamOwner(team.id), entered.task).writers == {8, 20}


async def test_a_members_stream_hears_of_the_teams_gradings(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    team = await teams.create(setup, crowd["bob"], SPRING, "Adders")
    heard = await live.audience(setup, crowd["bob"])
    assert heard.teams == frozenset({str(team.id)})
    assert ContestId("acme/spring") in heard.contests


async def test_someone_out_of_a_deleted_team_uploads_on_their_own(
    setup: Setup, acme: Acme, entered: Entered, crowd: dict[str, Session]
) -> None:
    bob = crowd["bob"]
    await teams.create(setup, bob, SPRING, "Adders")
    await upload(setup, acme.fake, bob, entered.task, b"print(1)\n")
    await teams.leave(setup, bob, SPRING)

    await upload(setup, acme.fake, bob, entered.task, b"print(2)\n", filename="b.py")

    assert _repo(acme.fake, UserOwner(8), entered.task).writers == {8}


async def test_teams_stand_once_the_contest_ends(
    setup: Setup, acme: Acme, crowd: dict[str, Session], clock: FakeClock
) -> None:
    team = await teams.create(setup, crowd["bob"], SPRING, "Adders")
    clock.advance(timedelta(hours=4))
    with pytest.raises(Forbidden):
        await teams.request(setup, crowd["carol"], SPRING, team.id)
    with pytest.raises(Forbidden):
        await teams.leave(setup, crowd["bob"], SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    moved = await teams.organise_move(setup, manager, SPRING, 20, team.id)
    assert sorted(m.user_id for m in moved.members) == [8, 20]


async def test_a_requester_sees_the_team_and_not_who_else_asked(
    setup: Setup, acme: Acme, crowd: dict[str, Session]
) -> None:
    bob, carol, dan = crowd["bob"], crowd["carol"], crowd["dan"]
    team = await teams.create(setup, bob, SPRING, "Adders")
    await teams.invite(setup, bob, SPRING, team.id, "dan")
    asked = await teams.request(setup, carol, SPRING, team.id)
    assert asked.pending == ()
    assert dan is not None
