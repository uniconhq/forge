"""Taking a task out of its contest (#450): a manager of the contest removes
a task nobody has submitted to, its entry, its place at the forge with its
roles, its activation and its name going, the letters after it moving up;
and withdraws one with submissions, which keeps its entry and its letter
but leaves every entrant's read of the contest, a submit and every
leaderboard, its submissions staying for the organisers, until it is put
back and scores as before. The request names what the organiser
confirmed, and is refused when it would now do the other; a removal waits
for a submit in flight, so it never deletes a submission. A leaderboard
naming a task keeps it from being removed. A save that writes `withdrawn`
by hand reads the same way.
"""

import asyncio
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Entrant, Grading
from forge.domain.errors import (
    Forbidden,
    NotFound,
    TakeOutChanged,
    TakeOutRefused,
    TaskWithdrawn,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import (
    boards,
    contest_home,
    files,
    names,
    submissions,
    tasks,
)
from forge.services.taking_out import TakeOut, put_back, take_out
from forge.testing import FakeClock
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    make_task,
    organiser,
    publish,
    upload,
    write_contest,
)

PRODUCT = TaskId("acme/spring/product")
MAX = TaskId("acme/spring/max")
STANDINGS = "leaderboards:\n  - {name: Standings, who: everyone}\n"
PASSED: dict[str, Any] = {
    "schema_version": 5,
    "stopped": None,
    "stopped_by": None,
    "tests": [{"test": "main/1", "outcome": "accepted", "values": {}}],
    "values": {},
    "run_log": None,
    "error": None,
}


def contest(extra: str = STANDINGS) -> str:
    """The running contest, its tasks sum, product and max, and `extra`."""
    return RUNNING.format(visibility="everyone") + "  - id: product\n  - id: max\n" + extra


async def three(setup: Setup, acme: Acme, extra: str = STANDINGS) -> None:
    """sum, product and max published in the running contest, in that order."""
    for task in ("product", "max"):
        made = await make_task(setup, acme, task)
        await publish(setup, acme, made)
    await write_contest(acme.fake, contest(extra))


async def _submit(setup: Setup, acme: Acme, session: Session, task: TaskId, key: str) -> int:
    made = await upload(setup, acme.fake, session, task, key.encode())
    submission = await submissions.submit(
        setup,
        session,
        task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    return submission.number


async def _passed(setup: Setup, task: TaskId) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading).where(Grading.task_id == task).values(status="done", result=PASSED)
        )


async def _settings(acme: Acme) -> bytes:
    return (await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")).content


async def _manager(setup: Setup, acme: Acme) -> Any:
    """ada, as a manager of the contest."""
    return await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)


async def test_a_task_nobody_submitted_to_is_removed_and_the_letters_after_it_move_up(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    acme.fake.reset_calls()

    done = await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    assert done is TakeOut.REMOVE
    assert b"product" not in await _settings(acme)
    assert ("acme", "spring.product.task") not in acme.fake.state.repos
    assert PRODUCT not in acme.fake.ci.activated
    assert [call.operation for call in acme.fake.calls if call.operation != "read_file"][-3:] == [
        "write_file",
        "deactivate",
        "delete_place",
    ]
    async with setup.unit_of_work() as ctx:
        assert await names.task_id(ctx, SPRING, "product") is None
    standing = await tasks.standing(setup, manager, SPRING)
    assert [(each.task.name, each.label) for each in standing] == [("sum", "A"), ("max", "B")]
    # The name is free again.
    await make_task(setup, acme, "product")


async def test_the_change_to_the_settings_names_the_manager_who_took_the_task_out(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)

    await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    latest, *_ = await files.history(setup, acme.ada, SPRING, "contest.yaml")
    assert latest.author_id == 7


async def test_a_task_with_a_submission_is_withdrawn_and_hidden_from_entrants_until_put_back(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    await _passed(setup, entered.task)

    async def seen() -> tuple[list[str], Fraction | None]:
        home = await contest_home.home(setup, entered.session, SPRING)
        (board,) = await boards.seen(setup, entered.session, SPRING)
        return [task.label for task in home.tasks], board.rows[0].keys[0]

    assert await seen() == (["A", "B", "C"], Fraction(100))
    with pytest.raises(TakeOutChanged) as changed:
        await take_out(setup, manager, entered.task, TakeOut.REMOVE)
    assert changed.value.extra == {"submissions": 1, "take_out": "withdraw"}
    assert changed.value.detail == (
        "sum has 1 submission now, so it can no longer be removed, only withdrawn: hidden from "
        "entrants and counted on no leaderboard, its submissions kept."
    )

    done = await take_out(setup, manager, entered.task, TakeOut.WITHDRAW)

    assert done is TakeOut.WITHDRAW
    assert b"  - id: sum\n    withdrawn: true\n  - id: product\n" in await _settings(acme)
    # B and C keep their letters; sum's points leave the board.
    assert await seen() == (["B", "C"], Fraction(0))
    with pytest.raises(NotFound):
        await contest_home.task(setup, entered.session, entered.task)
    with pytest.raises(NotFound):
        await submissions.mine(setup, entered.session, entered.task)
    with pytest.raises(TaskWithdrawn) as refused:
        await _submit(setup, acme, entered.session, entered.task, "key-0002-bbbb")
    assert refused.value.detail == (
        "This task was withdrawn from the contest, so it takes no submissions."
    )
    standing = await tasks.standing(setup, manager, SPRING)
    assert [
        (each.task.name, each.label, each.withdrawn, each.submissions, each.take_out)
        for each in standing
    ] == [
        ("sum", "A", True, 1, None),
        ("product", "B", False, 0, TakeOut.REMOVE),
        ("max", "C", False, 0, TakeOut.REMOVE),
    ]
    organised = await boards.organised(setup, manager, SPRING)
    assert [task.name for task in organised[0].final.tasks] == ["product", "max"]
    # Withdrawing again changes nothing.
    assert await take_out(setup, manager, entered.task, TakeOut.WITHDRAW) is TakeOut.WITHDRAW

    returned = await put_back(setup, manager, entered.task)

    assert returned.notes == ()
    assert b"withdrawn" not in await _settings(acme)
    assert await seen() == (["A", "B", "C"], Fraction(100))
    assert len(await submissions.mine(setup, entered.session, entered.task)) == 1


async def test_a_removal_waits_for_a_submit_in_flight_and_then_withdraws_nothing(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removal holds the task as a save does, so it counts the submission
    the submit was recording, and is refused, deleting nothing."""
    manager = await _manager(setup, acme)
    record = submissions._record
    removing: list[asyncio.Task[TakeOut]] = []

    async def record_then_remove(*args: Any, **kwargs: Any) -> Any:
        recorded = await record(*args, **kwargs)
        removing.append(asyncio.create_task(take_out(setup, manager, entered.task, TakeOut.REMOVE)))
        await asyncio.wait(removing, timeout=1)
        return recorded

    monkeypatch.setattr(submissions, "_record", record_then_remove)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")

    with pytest.raises(TakeOutChanged):
        await removing[0]
    assert ("acme", "spring.sum.task") in acme.fake.state.repos
    assert len(await submissions.mine(setup, entered.session, entered.task)) == 1


async def test_a_withdrawal_confirmed_for_a_task_nobody_submitted_to_is_refused(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    manager = await _manager(setup, acme)

    with pytest.raises(TakeOutChanged) as changed:
        await take_out(setup, manager, entered.task, TakeOut.WITHDRAW)

    assert changed.value.extra == {"submissions": 0, "take_out": "remove"}
    assert ("acme", "spring.sum.task") in acme.fake.state.repos


async def test_a_task_the_settings_do_not_list_is_not_withdrawn_and_says_why(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    await write_contest(acme.fake, contest().replace("  - id: sum\n", ""))

    with pytest.raises(TakeOutRefused) as refused:
        await take_out(setup, manager, entered.task, TakeOut.WITHDRAW)

    assert refused.value.detail == (
        "The contest's settings do not list sum. Add an entry for it to their tasks by hand first."
    )


async def test_a_task_a_leaderboard_names_is_not_removed_but_may_be_withdrawn(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    named = "leaderboards:\n  - {name: Practice, who: everyone, tasks: [product]}\n"
    await three(setup, acme, named)
    manager = await _manager(setup, acme)
    before = await _settings(acme)

    with pytest.raises(TakeOutRefused) as refused:
        await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    assert refused.value.detail == (
        "The leaderboard Practice counts product by name. Take product out of that leaderboard "
        "first, then remove the task."
    )
    assert await _settings(acme) == before
    assert ("acme", "spring.product.task") in acme.fake.state.repos


async def test_taking_a_task_out_needs_the_manager_role_at_the_contest(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.add_user(10, "otto")
    acme.fake.add_user(11, "tess")
    await acme.fake.orgs.grant_role(10, Scope("acme", "spring"), Role.OBSERVER)
    await acme.fake.orgs.grant_role(11, Scope("acme", "spring", "sum"), Role.MANAGER)
    observer = await organiser(setup, acme.fake, 10, Scope("acme", "spring"), Role.OBSERVER)
    task_manager = await organiser(
        setup, acme.fake, 11, Scope("acme", "spring", "sum"), Role.MANAGER
    )

    for who in (observer, task_manager):
        with pytest.raises(Forbidden):
            await take_out(setup, who, entered.task, TakeOut.REMOVE)
        with pytest.raises(Forbidden):
            await put_back(setup, who, entered.task)


async def test_a_removed_tasks_name_leaves_every_extension_that_named_it(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Entrant, entered.entrant)
        assert row is not None
        row.time_extension_seconds = 600
        row.extension_tasks = ["product", "max"]

    await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    async with setup.unit_of_work() as ctx:
        found = await ctx.db.scalar(
            select(Entrant.extension_tasks).where(Entrant.id == entered.entrant)
        )
    assert found == ["max"]


async def test_a_save_that_writes_withdrawn_by_hand_reads_as_the_action_does(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    """A withdrawn task is in no leaderboard's numbers and asks nothing of
    one: a board ranking a value sum does not report saves while sum is
    withdrawn, and putting sum back by hand is refused there."""
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    by_time = "leaderboards:\n  - {name: Fastest, who: everyone, tasks: [sum], order: [speed]}\n"
    withdrawn = contest(by_time).replace("  - id: sum\n", "  - id: sum\n    withdrawn: true\n")
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")

    written = await files.write(
        setup, manager, SPRING, "contest.yaml", withdrawn.encode(), current.token
    )

    assert isinstance(written, files.Written) and written.notes == ()
    home = await contest_home.home(setup, entered.session, SPRING)
    assert [task.name for task in home.tasks] == ["product", "max"]
    with pytest.raises(InvalidDefinition) as refused:
        await put_back(setup, manager, entered.task)
    assert [problem["path"] for problem in refused.value.errors] == ["leaderboards[0].order[0]"]
