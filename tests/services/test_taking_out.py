"""Taking a task out of its contest (#450): a manager of the contest removes
a task nobody has submitted to, its entry, its place at the forge with its
roles, its activation and its name going, the letters after it moving up;
and withdraws one with submissions, which keeps its entry and its letter
but leaves every entrant's read of the contest, a submit and every
leaderboard, its submissions staying for the organisers, until it is put
back and scores as before. The request names what the organiser
confirmed, and is refused when it would now do the other; a removal waits
for a submit in flight, so it never deletes a submission. A leaderboard
reading a task's results keeps it from being removed. A save that writes `withdrawn`
by hand reads the same way.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update

from forge.db.tables import Entrant, Grading, Upload
from forge.domain.errors import (
    Forbidden,
    NotFound,
    TakeOutChanged,
    TakeOutRefused,
    TaskWithdrawn,
    Unavailable,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import (
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
STANDINGS = "leaderboards:\n  standings: {title: Standings, audience: {who: everyone}}\n"
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
    assert [call.operation for call in acme.fake.calls if call.operation != "read_file"][-4:] == [
        "write_file",
        "deactivate",
        "delete_submission_place",
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


async def test_a_removal_that_fails_puts_the_entry_back_naming_the_same_manager(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    before = await _settings(acme)

    async def broken(*_: Any, **__: Any) -> None:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(acme.fake.content, "delete_place", broken)
    with pytest.raises(Unavailable):
        await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    assert await _settings(acme) == before
    history = acme.fake.state.repos[("acme", "spring.contest")].history
    assert [(change.message, change.author_id) for change in history[-2:]] == [
        ("Remove product from the contest", 7),
        ("Put the removed task back", 7),
    ]


def _places(acme: Acme, task: str) -> set[str]:
    return {
        name
        for _, name in acme.fake.state.repos
        if name.startswith(f"spring.{task}.") and name.endswith(".sub")
    }


async def test_a_removal_takes_each_entrants_place_to_submit_with_the_uploads_waiting_there(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await upload(setup, acme.fake, entered.session, PRODUCT, b"print(6)\n")
    assert _places(acme, "product")

    await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    assert _places(acme, "product") == set()
    async with setup.unit_of_work() as ctx:
        assert (await ctx.db.scalars(select(Upload).where(Upload.task_id == PRODUCT))).all() == []


async def test_a_place_holding_a_submission_the_platform_has_no_row_for_refuses_a_removal(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, PRODUCT, "key-0001-aaaa")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(delete(Grading).where(Grading.task_id == PRODUCT))
    settings = await _settings(acme)

    with pytest.raises(TakeOutRefused) as refused:
        await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    assert refused.value.detail == (
        "product has a submission the platform has not recorded yet, so it cannot be removed. "
        "Ask the platform's operator to reconcile, then withdraw it."
    )
    assert await _settings(acme) == settings
    assert ("acme", "spring.product.task") in acme.fake.state.repos


async def test_a_task_with_a_submission_is_withdrawn_and_hidden_from_entrants_until_put_back(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    await _passed(setup, entered.task)

    async def seen() -> list[str]:
        home = await contest_home.home(setup, entered.session, SPRING)
        return [task.label for task in home.tasks]

    assert await seen() == ["A", "B", "C"]
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
    # B and C keep their letters.
    assert await seen() == ["B", "C"]
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
    # Withdrawing again changes nothing.
    assert await take_out(setup, manager, entered.task, TakeOut.WITHDRAW) is TakeOut.WITHDRAW

    returned = await put_back(setup, manager, entered.task)

    assert returned.notes == ()
    assert b"withdrawn" not in await _settings(acme)
    assert await seen() == ["A", "B", "C"]
    assert len(await submissions.mine(setup, entered.session, entered.task)) == 1


async def test_a_task_is_put_back_in_a_contest_stored_with_old_leaderboards(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    """A read ignores boards in the old list form, which a save refuses, so
    withdrawing and putting back each leave them as they are."""
    old = "leaderboards:\n  - {title: Standings, who: everyone}\n"
    await three(setup, acme, old)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    await take_out(setup, manager, entered.task, TakeOut.WITHDRAW)

    returned = await put_back(setup, manager, entered.task)

    assert returned.notes == ()
    settings = await _settings(acme)
    assert b"withdrawn" not in settings
    assert settings.endswith(old.encode())


async def test_a_removal_waits_for_a_submit_in_flight_and_then_withdraws_nothing(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removal holds the task as a save does, so it counts the submission
    the submit was recording, and is refused, deleting nothing."""
    manager = await _manager(setup, acme)
    record = submissions._record
    removing: list[asyncio.Task[TakeOut]] = []

    async def waiting_for_a_lock() -> bool:
        async with setup.unit_of_work() as ctx:
            return bool(
                await ctx.db.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                        " AND database = (SELECT oid FROM pg_database"
                        " WHERE datname = current_database())"
                    )
                )
            )

    async def record_then_remove(*args: Any, **kwargs: Any) -> Any:
        recorded = await record(*args, **kwargs)
        removing.append(asyncio.create_task(take_out(setup, manager, entered.task, TakeOut.REMOVE)))
        while not removing[0].done() and not await waiting_for_a_lock():
            await asyncio.sleep(0.01)
        return recorded

    monkeypatch.setattr(submissions, "_record", record_then_remove)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")

    with pytest.raises(TakeOutChanged):
        await removing[0]
    assert ("acme", "spring.sum.task") in acme.fake.state.repos
    assert b"withdrawn" not in await _settings(acme)
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


async def test_a_task_a_leaderboard_reads_is_not_removed_but_may_be_withdrawn(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    named = (
        "leaderboards:\n  practice:\n    title: Practice\n    columns:\n"
        "      p: {value: [{block: latest, from: {results: product}}, total]}\n"
    )
    await three(setup, acme, named)
    manager = await _manager(setup, acme)
    before = await _settings(acme)

    with pytest.raises(TakeOutRefused) as refused:
        await take_out(setup, manager, PRODUCT, TakeOut.REMOVE)

    assert refused.value.detail == (
        "The leaderboard Practice reads the results of product. Take product out of that "
        "leaderboard's columns first, then remove the task."
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
    """A task withdrawn by hand leaves the entrants' read of the contest as
    the action does, and is put back the same way."""
    await three(setup, acme)
    manager = await _manager(setup, acme)
    await _submit(setup, acme, entered.session, entered.task, "key-0001-aaaa")
    withdrawn = contest().replace("  - id: sum\n", "  - id: sum\n    withdrawn: true\n")
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")

    written = await files.write(
        setup, manager, SPRING, "contest.yaml", withdrawn.encode(), current.token
    )

    assert isinstance(written, files.Written) and written.notes == ()
    home = await contest_home.home(setup, entered.session, SPRING)
    assert [task.name for task in home.tasks] == ["product", "max"]
    assert (await put_back(setup, manager, entered.task)).notes == ()
    assert b"withdrawn" not in await _settings(acme)
