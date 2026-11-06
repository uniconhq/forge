"""What a page is told the reader may do agrees with what the action then
allows, for an admin, a manager, an observer and an entrant, who holds no
role: the org's, the contest's and the task's flags, each holder's and
invite's, each grading's, regrade's and question's, each listed task's,
and the owners one may make a workflow under. The list of contests needs
the observer role at the org, one contest's read and the list of tasks at
the contest, and who one is names the contests and tasks one holds a role
at.
"""

import uuid
from collections.abc import Awaitable
from typing import Any

import pytest

from forge.domain.clock import FakeClock
from forge.domain.errors import AdminOnly, Forbidden, InvalidName, NotFound
from forge.domain.identity import PLATFORM, User
from forge.domain.ids import ContestId, TaskId
from forge.domain.invites import Grant
from forge.domain.names import ScopeNames
from forge.domain.roles import Role, RoleGrant, Scope
from forge.runtime.setup import Setup
from forge.services import (
    clarifications,
    contests,
    files,
    gradings,
    identity,
    invites,
    may,
    orgs,
    regrades,
    roles,
    tasks,
    workflows,
)
from forge.services.access import Organiser
from tests.services.conftest import (
    ACME,
    SPRING,
    SUM,
    Acme,
    Entered,
    make_task,
    organiser,
    signed_in,
    write_contest,
)
from tests.services.test_regrades import ACCEPTED, FAILED, _rows, _stands, _submit

PEOPLE = {
    "admin": Role.ADMIN,
    "manager": Role.MANAGER,
    "observer": Role.OBSERVER,
    "entrant": None,
}
ORG = Scope("acme")
CONTEST = Scope("acme", "spring")
TASK = Scope("acme", "spring", "sum")


def _holding(acme: Acme, scope: Scope, held: Role | None) -> Organiser:
    """Someone checked as holding `held` at `scope` and nothing else, or
    nothing at all, their calls made as ada."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=() if held is None else (RoleGrant(scope, held),),
        scope=scope,
        role=held or Role.OBSERVER,
        identity=acme.ada.identity,
    )


async def _person(setup: Setup, acme: Acme, scope: Scope, held: Role | None) -> Organiser:
    """erin (60), signed in, holding `held` at `scope` and nothing else; an
    entrant, holding nothing, is `_holding` nothing, every check refusing
    them before the forge is asked."""
    if held is None:
        return _holding(acme, scope, None)
    acme.fake.add_user(60, "erin")
    await acme.fake.orgs.grant_role(60, scope, held)
    return await organiser(setup, acme.fake, 60, scope, held)


async def _allowed(call: Awaitable[Any]) -> bool:
    """Whether the action let the person do it; any other refusal fails."""
    try:
        await call
    except Forbidden, AdminOnly:
        return False
    return True


def _agree(seen: dict[str, tuple[bool, bool]]) -> None:
    """Each flag, by name, with whether its action allowed it."""
    assert {key: told for key, (told, _) in seen.items()} == {
        key: did for key, (_, did) in seen.items()
    }


@pytest.mark.parametrize("who", PEOPLE)
async def test_the_orgs_flags_agree_with_its_actions(setup: Setup, acme: Acme, who: str) -> None:
    acme.fake.add_user(50, "carol")
    acme.fake.add_user(51, "dave")
    person = await _person(setup, acme, ORG, PEOPLE[who])
    told = may.at_org(person.grants, ORG)

    seen = {
        "edit": (told.edit, await _allowed(orgs.update(setup, person, ACME, description="New"))),
        "create_contest": (
            told.create_contest,
            await _allowed(contests.create(setup, person, ACME, "autumn")),
        ),
        "see_people": (told.see_people, await _allowed(roles.holders(setup, person, ORG))),
        "manage_people": (
            told.manage_people,
            await _allowed(roles.grant(setup, person, ORG, "carol", Role.OBSERVER)),
        ),
        "give_admin": (
            told.give_admin,
            await _allowed(roles.grant(setup, person, ORG, "dave", Role.ADMIN)),
        ),
    }

    _agree(seen)
    if who != "entrant":
        assert (await orgs.read(setup, person, ACME)).may == told


@pytest.mark.parametrize("who", PEOPLE)
async def test_the_contests_flags_agree_with_its_actions(
    setup: Setup, acme: Acme, spring: ContestId, who: str
) -> None:
    acme.fake.add_user(50, "carol")
    acme.fake.add_user(51, "dave")
    person = await _person(setup, acme, CONTEST, PEOPLE[who])
    told = may.at_place(person.grants, CONTEST)
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    changed = current.content.replace(b"visibility: signed-in", b"visibility: everyone")
    assert changed != current.content

    seen = {
        "manage": (
            told.manage,
            await _allowed(files.write(setup, person, SPRING, "notes.txt", b"new", None)),
        ),
        "edit_admin_settings": (
            told.edit_admin_settings,
            await _allowed(
                files.write(setup, person, SPRING, "contest.yaml", changed, current.token)
            ),
        ),
        "manage, a task": (told.manage, await _allowed(tasks.create(setup, person, SPRING, "b"))),
        "see_people": (told.see_people, await _allowed(roles.holders(setup, person, CONTEST))),
        "manage_people": (
            told.manage_people,
            await _allowed(roles.grant(setup, person, CONTEST, "carol", Role.OBSERVER)),
        ),
        "give_admin": (
            told.give_admin,
            await _allowed(roles.grant(setup, person, CONTEST, "dave", Role.ADMIN)),
        ),
    }

    _agree(seen)
    assert ("visibility" in told.locked_keys) is not told.edit_admin_settings


@pytest.mark.parametrize("who", PEOPLE)
async def test_the_tasks_flags_agree_with_its_actions_held_at_the_contest(
    setup: Setup, acme: Acme, sum_task: TaskId, who: str
) -> None:
    person = await _person(setup, acme, CONTEST, PEOPLE[who])
    told = may.at_place(person.grants, TASK)
    task_yaml = await acme.fake.content.read_file(PLATFORM, SUM, "task.yaml")
    statement = await acme.fake.content.read_file(PLATFORM, SUM, "statement.md")
    limited = task_yaml.content.replace(b"scoring:", b"submissions: {max: 3}\nscoring:")
    assert limited != task_yaml.content

    seen = {
        "manage": (
            told.manage,
            await _allowed(files.write(setup, person, SUM, "notes.txt", b"new", None)),
        ),
        "edit_admin_settings, task.yaml": (
            told.edit_admin_settings,
            await _allowed(files.write(setup, person, SUM, "task.yaml", limited, task_yaml.token)),
        ),
    }
    statement_changed = await _allowed(
        files.write(setup, person, SUM, "statement.md", b"Add two numbers.\n", statement.token)
    )
    seen["edit_admin_settings, statement.md"] = (told.edit_admin_settings, statement_changed)

    _agree(seen)
    assert ("submissions" in told.locked_keys) is not told.edit_admin_settings
    if who != "entrant":
        assert (await tasks.state(setup, person, SUM)).may == told


@pytest.mark.parametrize("who", PEOPLE)
async def test_the_tasks_flags_agree_with_its_actions_held_at_the_task(
    setup: Setup, acme: Acme, sum_task: TaskId, who: str
) -> None:
    acme.fake.add_user(50, "carol")
    acme.fake.add_user(51, "dave")
    person = await _person(setup, acme, TASK, PEOPLE[who])
    told = may.at_place(person.grants, TASK)
    task_yaml = await acme.fake.content.read_file(PLATFORM, SUM, "task.yaml")
    limited = task_yaml.content.replace(b"scoring:", b"submissions: {max: 3}\nscoring:")

    seen = {
        "manage": (
            told.manage,
            await _allowed(files.write(setup, person, SUM, "notes.txt", b"new", None)),
        ),
        "edit_admin_settings": (
            told.edit_admin_settings,
            await _allowed(files.write(setup, person, SUM, "task.yaml", limited, task_yaml.token)),
        ),
        "see_people": (told.see_people, await _allowed(roles.holders(setup, person, TASK))),
        "manage_people": (
            told.manage_people,
            await _allowed(roles.grant(setup, person, TASK, "carol", Role.OBSERVER)),
        ),
        "give_admin": (
            told.give_admin,
            await _allowed(roles.grant(setup, person, TASK, "dave", Role.ADMIN)),
        ),
    }

    _agree(seen)
    assert set(told.locked_keys) == (set() if told.edit_admin_settings else {"name", "submissions"})


@pytest.mark.parametrize("who", PEOPLE)
async def test_each_holders_flag_agrees_with_taking_their_role(
    setup: Setup, acme: Acme, spring: ContestId, who: str
) -> None:
    acme.fake.add_user(50, "carol")
    acme.fake.add_user(51, "dave")
    await roles.grant(setup, acme.ada, CONTEST, "carol", Role.MANAGER)
    await roles.grant(setup, acme.ada, CONTEST, "dave", Role.ADMIN)
    person = await _person(setup, acme, CONTEST, PEOPLE[who])

    if PEOPLE[who] is None:
        assert not await _allowed(roles.holders(setup, person, CONTEST))
        assert not await _allowed(roles.revoke(setup, person, CONTEST, 50))
        assert not await _allowed(roles.revoke(setup, person, CONTEST, 51))
        return
    held = {each.user.id: each for each in await roles.holders(setup, person, CONTEST)}
    # ada is admin at the org, so no one changes her role here.
    assert held[7].may_change is False
    _agree(
        {
            "carol": (
                held[50].may_change,
                await _allowed(roles.revoke(setup, person, CONTEST, 50)),
            ),
            "dave": (held[51].may_change, await _allowed(roles.revoke(setup, person, CONTEST, 51))),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_each_holders_flag_agrees_with_granting_their_role(
    setup: Setup, acme: Acme, spring: ContestId, who: str
) -> None:
    acme.fake.add_user(50, "carol")
    acme.fake.add_user(51, "dave")
    acme.fake.add_user(52, "frank")
    acme.fake.add_user(53, "gina")
    await roles.grant(setup, acme.ada, CONTEST, "carol", Role.MANAGER)
    await roles.grant(setup, acme.ada, CONTEST, "dave", Role.ADMIN)
    person = await _person(setup, acme, CONTEST, PEOPLE[who])

    if PEOPLE[who] is None:
        assert not await _allowed(roles.grant(setup, person, CONTEST, "frank", Role.MANAGER))
        assert not await _allowed(roles.grant(setup, person, CONTEST, "carol", Role.OBSERVER))
        return
    held = {each.user.id: each for each in await roles.holders(setup, person, CONTEST)}
    # Their role to someone new, then each moved down from it.
    _agree(
        {
            "carol's role": (
                held[50].may_change,
                await _allowed(roles.grant(setup, person, CONTEST, "frank", Role.MANAGER)),
            ),
            "dave's role": (
                held[51].may_change,
                await _allowed(roles.grant(setup, person, CONTEST, "gina", Role.ADMIN)),
            ),
            "carol": (
                held[50].may_change,
                await _allowed(roles.grant(setup, person, CONTEST, "carol", Role.OBSERVER)),
            ),
            "dave": (
                held[51].may_change,
                await _allowed(roles.grant(setup, person, CONTEST, "dave", Role.MANAGER)),
            ),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_each_invites_flag_agrees_with_withdrawing_it(
    setup: Setup, acme: Acme, spring: ContestId, who: str
) -> None:
    manager = await invites.create(setup, acme.ada, CONTEST, Grant.MANAGER, email="m@uni.test")
    admin = await invites.create(setup, acme.ada, CONTEST, Grant.ADMIN, email="a@uni.test")
    assert manager.may_change and admin.may_change
    person = await _person(setup, acme, CONTEST, PEOPLE[who])

    if PEOPLE[who] is None:
        assert not await _allowed(invites.at(setup, person, CONTEST))
        assert not await _allowed(invites.withdraw(setup, person, CONTEST, manager.id))
        return
    listed = {each.id: each.may_change for each in await invites.at(setup, person, CONTEST)}
    _agree(
        {
            "manager": (
                listed[manager.id],
                await _allowed(invites.withdraw(setup, person, CONTEST, manager.id)),
            ),
            "admin": (
                listed[admin.id],
                await _allowed(invites.withdraw(setup, person, CONTEST, admin.id)),
            ),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_each_invites_flag_agrees_with_sending_it_again(
    setup: Setup, acme: Acme, spring: ContestId, clock: FakeClock, who: str
) -> None:
    manager = await invites.create(setup, acme.ada, CONTEST, Grant.MANAGER, email="m@uni.test")
    admin = await invites.create(setup, acme.ada, CONTEST, Grant.ADMIN, email="a@uni.test")
    await setup.settle()
    clock.advance(invites.SEND_AGAIN_AFTER)
    person = await _person(setup, acme, CONTEST, PEOPLE[who])

    if PEOPLE[who] is None:
        assert not await _allowed(invites.send_again(setup, person, CONTEST, manager.id))
        return
    listed = {each.id: each.may_change for each in await invites.at(setup, person, CONTEST)}
    _agree(
        {
            "manager": (
                listed[manager.id],
                await _allowed(invites.send_again(setup, person, CONTEST, manager.id)),
            ),
            "admin": (
                listed[admin.id],
                await _allowed(invites.send_again(setup, person, CONTEST, admin.id)),
            ),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_a_gradings_and_a_regrades_flags_agree_with_retrying_and_dismissing(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, who: str
) -> None:
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    rejudged = await gradings.rejudge(setup, acme.ada, entered.task)
    assert rejudged.regrade is not None
    await _stands(setup, 1, **FAILED)
    [failed] = [row for row in await _rows(setup) if row.attempt == 2]
    person = await _person(setup, acme, TASK, PEOPLE[who])

    if PEOPLE[who] is None:
        assert not await _allowed(gradings.feed(setup, person, SPRING))
        assert not await _allowed(regrades.latest(setup, person, SPRING))
        with pytest.raises(NotFound):
            await gradings.retry(setup, person, failed.id)
        assert not await _allowed(regrades.dismiss(setup, person, entered.task, rejudged.regrade))
        return
    [entry] = await gradings.feed(setup, person, SPRING, latest=True)
    [summary] = await regrades.latest(setup, person, SPRING)
    _agree(
        {
            "grading": (entry.may_manage, await _allowed(gradings.retry(setup, person, failed.id))),
            "regrade": (
                summary.may_manage,
                await _allowed(regrades.dismiss(setup, person, entered.task, rejudged.regrade)),
            ),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_a_gradings_flag_agrees_with_cancelling_it_and_rejudging_its_task(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, who: str
) -> None:
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **FAILED)
    [failed] = await _rows(setup)
    person = await _person(setup, acme, TASK, PEOPLE[who])
    reason = "The checker crashed on this one."

    if PEOPLE[who] is None:
        with pytest.raises(NotFound):
            await gradings.cancel(setup, person, failed.id, reason)
        assert not await _allowed(gradings.rejudge(setup, person, entered.task))
        return
    [entry] = await gradings.feed(setup, person, SPRING, latest=True)
    _agree(
        {
            "cancel": (
                entry.may_manage,
                await _allowed(gradings.cancel(setup, person, failed.id, reason)),
            ),
            "rejudge": (
                entry.may_manage,
                await _allowed(gradings.rejudge(setup, person, entered.task)),
            ),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_a_questions_flag_agrees_with_marking_it_answered(
    setup: Setup, acme: Acme, entered: Entered, who: str
) -> None:
    asked = await clarifications.ask(
        setup, entered.session, SPRING, title="Input size?", body="How big is n?", task="sum"
    )
    assert asked.may_answer is False
    person = await _person(setup, acme, CONTEST, PEOPLE[who])
    marked = clarifications.mark(setup, person, SPRING, asked.asker, asked.number)

    if PEOPLE[who] is None:
        assert not await _allowed(clarifications.of_contest(setup, person, SPRING))
        assert not await _allowed(marked)
        return
    [read] = await clarifications.of_contest(setup, person, SPRING)
    _agree({"answer": (read.may_answer, await _allowed(marked))})


@pytest.mark.parametrize("who", PEOPLE)
async def test_an_inbox_questions_flag_agrees_with_replying_unmarking_and_answering_publicly(
    setup: Setup, acme: Acme, entered: Entered, who: str
) -> None:
    asked = await clarifications.ask(
        setup, entered.session, SPRING, title="Input size?", body="How big is n?", task="sum"
    )
    person = await _person(setup, acme, CONTEST, PEOPLE[who])
    asker, number = asked.asker, asked.number
    reply = clarifications.reply(setup, person, SPRING, asker, number, body="Up to 10^5.")
    unmark = clarifications.unmark(setup, person, SPRING, asker, number)
    public = clarifications.answer_publicly(
        setup, person, SPRING, asker, number, title="On n", body="n is at most 10^5."
    )

    if PEOPLE[who] is None:
        assert not await _allowed(clarifications.inbox(setup, entered.session, "acme"))
        assert not await _allowed(reply)
        assert not await _allowed(unmark)
        assert not await _allowed(public)
        return
    [read] = await clarifications.inbox(setup, await signed_in(setup, acme.fake, 60), "acme")
    _agree(
        {
            "reply": (read.may_answer, await _allowed(reply)),
            "unmark": (read.may_answer, await _allowed(unmark)),
            "answer publicly": (read.may_answer, await _allowed(public)),
        }
    )


@pytest.mark.parametrize("who", PEOPLE)
async def test_each_listed_tasks_flag_agrees_with_changing_it(
    setup: Setup, acme: Acme, sum_task: TaskId, who: str
) -> None:
    person = await _person(setup, acme, CONTEST, PEOPLE[who])

    if PEOPLE[who] is None:
        assert not await _allowed(tasks.list_tasks(setup, person, SPRING))
        return
    [listed] = await tasks.list_tasks(setup, person, SPRING)
    allowed = await _allowed(files.write(setup, person, SUM, "notes.txt", b"new", None))
    _agree({"sum": (listed.may_manage, allowed)})


async def test_the_tasks_list_needs_the_observer_role_at_the_contest(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    other = await make_task(setup, acme, "other")

    at_sum = _holding(acme, TASK, Role.ADMIN)
    at_contest = _holding(acme, CONTEST, Role.OBSERVER)

    with pytest.raises(Forbidden):
        await tasks.list_tasks(setup, at_sum, SPRING)
    assert [
        (each.id, each.may_manage) for each in await tasks.list_tasks(setup, at_contest, SPRING)
    ] == [(other, False), (SUM, False)]


async def test_the_contests_list_needs_the_observer_role_at_the_org(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await contests.create(setup, acme.ada, ACME, "autumn")

    at_task = _holding(acme, TASK, Role.ADMIN)
    at_contest = _holding(acme, CONTEST, Role.ADMIN)
    at_org = _holding(acme, ORG, Role.OBSERVER)
    elsewhere = _holding(acme, Scope("other"), Role.ADMIN)

    for refused in (at_task, at_contest, elsewhere):
        with pytest.raises(Forbidden):
            await contests.list_contests(setup, refused, ACME)
    assert [each.id for each in await contests.list_contests(setup, at_org, ACME)] == [
        ContestId("acme/autumn"),
        SPRING,
    ]


@pytest.mark.parametrize("who", PEOPLE)
async def test_each_listed_contest_tells_its_flags_for_the_reader(
    setup: Setup, acme: Acme, sum_task: TaskId, who: str
) -> None:
    held = PEOPLE[who]
    reader = _holding(acme, ORG, held)
    if held is None:
        with pytest.raises(Forbidden):
            await contests.list_contests(setup, reader, ACME)
        return

    (listed,) = await contests.list_contests(setup, reader, ACME)

    assert listed.may == may.at_place(reader.grants, CONTEST)


@pytest.mark.parametrize("who", PEOPLE)
async def test_a_read_contest_tells_its_flags_for_the_reader(
    setup: Setup, acme: Acme, sum_task: TaskId, who: str
) -> None:
    held = PEOPLE[who]
    reader = _holding(acme, CONTEST, held)
    if held is None:
        with pytest.raises(Forbidden):
            await contests.read(setup, reader, SPRING)
        return

    read = await contests.read(setup, reader, SPRING)

    assert read.may == may.at_place(reader.grants, CONTEST)


async def test_a_contest_only_manager_reads_the_contest_with_a_managers_flags(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    at_contest = _holding(acme, CONTEST, Role.MANAGER)

    with pytest.raises(Forbidden):
        await contests.list_contests(setup, at_contest, ACME)
    read = await contests.read(setup, at_contest, SPRING)

    assert read.may.manage and read.may.see_people and read.may.manage_people
    assert not (read.may.edit_admin_settings or read.may.give_admin)


async def test_a_task_only_holder_is_refused_the_contest(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    with pytest.raises(Forbidden):
        await contests.read(setup, _holding(acme, TASK, Role.ADMIN), SPRING)


async def test_who_one_is_names_the_contests_and_tasks_one_holds_a_role_at(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    acme.fake.add_user(50, "carol")
    await acme.fake.orgs.grant_role(50, TASK, Role.OBSERVER)
    await acme.fake.orgs.grant_role(50, CONTEST, Role.MANAGER)
    session = await signed_in(setup, acme.fake, 50)

    me = await identity.whoami(setup, session)

    assert me.contests == (ScopeNames("acme", "spring"),)
    assert me.tasks == (ScopeNames("acme", "spring", "sum"),)


async def test_who_one_is_names_no_contest_or_task_for_a_role_at_the_org(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    acme.fake.add_user(50, "carol")
    await acme.fake.orgs.grant_role(50, ORG, Role.ADMIN)
    session = await signed_in(setup, acme.fake, 50)

    me = await identity.whoami(setup, session)

    assert (me.orgs, me.contests, me.tasks) == (("acme",), (), ())


async def test_a_contests_flags_are_told_while_its_settings_do_not_read(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await write_contest(acme.fake, "name: Spring\nstart: not a time\n")

    (listed,) = await contests.list_contests(setup, acme.ada, ACME)
    read = await contests.read(setup, acme.ada, SPRING)

    assert listed.state is None and read.state is None
    assert listed.may.manage and listed.may.edit_admin_settings
    assert read.may == listed.may


@pytest.mark.parametrize("who", PEOPLE)
async def test_the_owners_one_is_told_agree_with_making_a_workflow(
    setup: Setup, acme: Acme, spring: ContestId, who: str
) -> None:
    acme.fake.add_user(50, "Carol")
    held = PEOPLE[who]
    if held is not None:
        await acme.fake.orgs.grant_role(50, ORG, held)
    session = await signed_in(setup, acme.fake, 50)

    me = await identity.whoami(setup, session)

    assert me.orgs == (() if held is None else ("acme",))
    made = {
        owner: await _allowed(workflows.create(setup, session, owner, f"w{uuid.uuid4().hex[:6]}"))
        for owner in ("carol", "acme")
    }
    assert [owner for owner, allowed in made.items() if allowed] == list(me.workflow_owners)


async def test_a_username_that_cannot_name_a_workflow_is_no_owner_one_is_told(
    setup: Setup, acme: Acme
) -> None:
    acme.fake.add_user(50, "ann.lee")
    session = await signed_in(setup, acme.fake, 50)

    me = await identity.whoami(setup, session)

    assert me.workflow_owners == ()
    with pytest.raises(InvalidName):
        await workflows.create(setup, session, "ann.lee", "tuned")


async def test_a_role_held_only_inside_an_org_lists_it_but_makes_no_workflow_there(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    acme.fake.add_user(50, "carol")
    await acme.fake.orgs.grant_role(50, CONTEST, Role.ADMIN)
    session = await signed_in(setup, acme.fake, 50)

    me = await identity.whoami(setup, session)

    assert (me.orgs, me.workflow_owners) == (("acme",), ("carol",))
    assert not await _allowed(workflows.create(setup, session, "acme", "tuned"))
