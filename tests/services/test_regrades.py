"""After a regrade of many, the organisers see what it changed (#448,
TASK-FORMAT.md section 3.5). A rejudge and a save's regrade are each kept
as one regrade, naming what started it, who and when, and every attempt it
makes, and each retry of one of those, belongs to it. Each submission's
latest attempt of it is compared with the result that counted when it
began: it was graded again, is newly graded, still running, failed, or
ended over the limit. The contest's page reads each task's latest regrade
summed up until a newer one replaces it or a manager dismisses it, and the
regrade's gradings read with each change, the earlier and new attempts side
by side, narrowed to one change; what it changed, run by run, is
`run_summary`'s.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading, Regrade
from forge.domain.content import Edit
from forge.domain.errors import Forbidden, NotFound
from forge.domain.identity import PLATFORM, User
from forge.domain.ids import TaskId
from forge.domain.regrades import ChangeKind, RegradeCause
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import gradings, regrades, submissions, task_save
from forge.services.access import Organiser
from forge.services.task_save import Published
from forge.testing import FakeClock
from tests.services.conftest import SPRING, Acme, Entered, checked, organiser, upload

SOURCE = b"print(sum(map(int, input().split())))\n"
SUM = Scope("acme", "spring", "sum")
ONE_RUN = b"      - [tests/1.in, tests/1.ans]\n"
SECOND_RUN = b"      - [tests/2.in, tests/2.ans]\n"
TOTAL = b"  values:\n    total: {block: count, over: {runs: all}}\n"


def _result(verdict: str) -> dict[str, Any]:
    return {
        "schema_version": 6,
        "runs": [
            {
                "id": "r1",
                "key": {"input": "tests/1.in", "answer": "tests/1.ans"},
                "verdict": verdict,
                "passed": 1 if verdict == "accepted" else 0,
                "reports": {},
                "texts": {},
            }
        ],
    }


ACCEPTED: dict[str, Any] = {"status": "done", "result": _result("accepted")}
WRONG: dict[str, Any] = {"status": "done", "result": _result("wrong_answer")}
FAILED: dict[str, Any] = {"status": "system_error", "error": "The checker crashed."}


async def _submit(setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, n: int) -> None:
    clock.advance(timedelta(seconds=31))
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "code": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=f"key-000{n}-aaaa",
    )


async def _rows(setup: Setup) -> list[Grading]:
    async with setup.unit_of_work() as ctx:
        return list(
            (
                await ctx.db.execute(
                    select(Grading)
                    .where(Grading.pipeline != "save")
                    .order_by(Grading.submission_number, Grading.attempt)
                )
            ).scalars()
        )


async def _stands(setup: Setup, number: int, **values: Any) -> None:
    """Submission `number`'s latest attempt written as `values`."""
    latest = max(
        (row for row in await _rows(setup) if row.submission_number == number),
        key=lambda row: row.attempt,
    )
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).where(Grading.id == latest.id).values(**values))


async def _five(setup: Setup, acme: Acme, entered: Entered, clock: FakeClock) -> None:
    """Five submissions: four graded, accepted, wrong, accepted, accepted,
    and the fifth delayed.
    """
    for number, stands in enumerate((ACCEPTED, WRONG, ACCEPTED, ACCEPTED, FAILED), start=1):
        await _submit(setup, acme, entered, clock, number)
        await _stands(setup, number, **stands)


def _held(acme: Acme, scope: Scope, role: Role) -> Organiser:
    """Someone checked as holding `role` at `scope` and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, role),),
        scope=scope,
        role=role,
        identity=acme.ada.identity,
    )


def _counts(summary: regrades.Summary) -> dict[ChangeKind, int]:
    return {change: count for change, count in summary.counts.items() if count}


async def test_a_rejudge_is_one_regrade_and_says_what_it_changed(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _five(setup, acme, entered, clock)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    observer = _held(acme, Scope("acme", "spring"), Role.OBSERVER)

    rejudged = await gradings.rejudge(setup, manager, entered.task)

    assert rejudged.queued == 5 and rejudged.regrade is not None
    async with setup.unit_of_work() as ctx:
        kept = await ctx.db.get(Regrade, rejudged.regrade)
    assert kept is not None
    assert (kept.task_id, kept.cause, kept.started_by, kept.publication_id) == (
        entered.task,
        RegradeCause.REJUDGE,
        7,
        rejudged.publication,
    )
    assert {row.regrade_id for row in await _rows(setup) if row.attempt == 2} == {kept.id}
    assert {row.regrade_id for row in await _rows(setup) if row.attempt == 1} == {None}
    # Four graded again, whatever their verdicts now, and one still running.
    for number, stands in ((1, WRONG), (2, ACCEPTED), (3, WRONG), (4, ACCEPTED)):
        await _stands(setup, number, **stands)

    (summary,) = await regrades.latest(setup, observer, SPRING)

    assert (summary.id, summary.task, summary.label, summary.cause) == (
        kept.id,
        entered.task,
        "A",
        RegradeCause.REJUDGE,
    )
    assert (summary.started_by, summary.started_by_name) == (7, "ada")
    assert (summary.total, summary.graded, summary.running) == (5, 4, True)
    assert _counts(summary) == {ChangeKind.REGRADED: 4, ChangeKind.RUNNING: 1}

    graded = await regrades.gradings_of(
        setup, observer, SPRING, kept.id, change=ChangeKind.REGRADED
    )
    first = next(entry for entry in graded if entry.grading.submission_number == 1)
    change = first.grading.change
    assert change is not None and change.earlier is not None and change.now is not None
    assert (first.grading.attempt, first.label) == (2, "A")
    assert (change.change, change.earlier, change.now) == (
        ChangeKind.REGRADED,
        1,
        2,
    )
    assert sorted(entry.grading.submission_number for entry in graded) == [1, 2, 3, 4]
    every = await regrades.gradings_of(setup, observer, SPRING, kept.id)
    assert [entry.grading.submission_number for entry in every] == [5, 4, 3, 2, 1]
    assert all(entry.grading.regrade == kept.id for entry in every)

    # The delayed one graded, it is newly graded: there was nothing to compare.
    await _stands(setup, 5, **ACCEPTED)
    (summary,) = await regrades.latest(setup, observer, SPRING)
    assert (summary.graded, summary.running, summary.counts[ChangeKind.NEW]) == (5, False, 1)


async def test_a_retry_of_a_regrades_attempt_belongs_to_it(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    rejudged = await gradings.rejudge(setup, manager, entered.task)
    await _stands(setup, 1, **FAILED)
    (failed,) = [row for row in await _rows(setup) if row.attempt == 2]
    (summary,) = await regrades.latest(setup, manager, SPRING)
    assert _counts(summary) == {ChangeKind.FAILED: 1}

    retried = await gradings.retry(setup, manager, failed.id)

    assert retried.regrade == rejudged.regrade
    (summary,) = await regrades.latest(setup, manager, SPRING)
    assert _counts(summary) == {ChangeKind.RUNNING: 1}
    await _stands(setup, 1, **WRONG)
    (summary,) = await regrades.latest(setup, manager, SPRING)
    assert _counts(summary) == {ChangeKind.REGRADED: 1}
    # Retried again, it is still the regrade's; a retry of an attempt of no
    # regrade belongs to none.
    again = await gradings.retry(setup, manager, retried.id)
    assert again.regrade == rejudged.regrade
    await _submit(setup, acme, entered, clock, 2)
    (second,) = [row for row in await _rows(setup) if row.submission_number == 2]
    await _stands(setup, 2, **ACCEPTED)
    assert (await gradings.retry(setup, manager, second.id)).regrade is None


async def test_a_saves_regrade_is_one_too_and_replaces_the_one_before(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await gradings.rejudge(setup, manager, entered.task)
    await _stands(setup, 1, **ACCEPTED)

    task_file = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    saved = await task_save.save(
        setup,
        acme.ada,
        entered.task,
        {
            "task.yaml": Edit(
                task_file.content.replace(ONE_RUN, ONE_RUN + SECOND_RUN), task_file.token
            ),
            "tests/2.in": Edit(b"2 2\n", None),
            "tests/2.ans": Edit(b"4\n", None),
        },
        confirm=True,
    )
    await checked(setup, entered.task)

    assert isinstance(saved, task_save.Published)
    (summary,) = await regrades.latest(setup, manager, SPRING)
    assert (summary.cause, summary.publication, summary.started_by) == (
        RegradeCause.SAVE,
        saved.publication,
        7,
    )
    assert _counts(summary) == {ChangeKind.RUNNING: 1}


async def _save(setup: Setup, acme: Acme, task: TaskId, files: dict[str, bytes]) -> Published:
    """ada's save of the task's `files` over what they hold, confirmed."""
    head = await acme.fake.content.list_files(PLATFORM, task)
    saved = await task_save.save(
        setup,
        acme.ada,
        task,
        {path: Edit(content, head.tokens[path]) for path, content in files.items()},
        confirm=True,
    )
    assert isinstance(saved, Published), saved
    await checked(setup, task)
    return saved


async def test_each_side_of_a_saves_regrade_is_an_attempt_and_its_verdicts_are_compared(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    # A save that breaks the test's answer regrades bob's result: graded
    # again either way, its verdict told as changed once it is not accepted.
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    await _save(setup, acme, entered.task, {"tests/1.ans": b"4\n"})
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    await _stands(setup, 1, **ACCEPTED)
    (summary,) = await regrades.latest(setup, manager, SPRING)
    (entry,) = await regrades.gradings_of(setup, manager, SPRING, summary.id)

    change = entry.grading.change
    assert change is not None and change.earlier is not None and change.now is not None
    assert (change.earlier, change.now) == (1, 2)
    assert _counts(summary) == {ChangeKind.REGRADED: 1}
    assert (await regrades.run_summary(setup, manager, entered.task, summary.id)).changed == ()
    await _stands(setup, 1, **WRONG)
    (changed,) = (await regrades.run_summary(setup, manager, entered.task, summary.id)).changed
    assert [(each.run, each.before, each.after) for each in changed.verdicts] == [
        ("r1", "accepted", "wrong_answer")
    ]


async def test_a_finished_regrade_reads_the_same_after_a_later_save(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    # The rejudge changed no verdict; a save that changes only how the task
    # scores afterwards regrades nothing, and the rejudge's summary still
    # reads what the rejudge did.
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    rejudged = await gradings.rejudge(setup, manager, entered.task)
    assert rejudged.regrade is not None
    await _stands(setup, 1, **ACCEPTED)
    task_file = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")

    raised = await _save(
        setup,
        acme,
        entered.task,
        {"task.yaml": task_file.content.replace(b"  values:\n", TOTAL)},
    )
    await checked(setup, entered.task)

    assert not raised.grading_changed
    (summary,) = await regrades.latest(setup, manager, SPRING)
    assert (summary.id, _counts(summary)) == (rejudged.regrade, {ChangeKind.REGRADED: 1})
    (entry,) = await regrades.gradings_of(setup, manager, SPRING, rejudged.regrade)
    change = entry.grading.change
    assert change is not None and change.earlier is not None and change.now is not None
    assert (change.earlier, change.now) == (1, 2)


async def test_a_manager_dismisses_a_regrade_for_everyone(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    observer = _held(acme, SUM, Role.OBSERVER)
    rejudged = await gradings.rejudge(setup, manager, entered.task)
    assert rejudged.regrade is not None

    with pytest.raises(Forbidden):
        await regrades.dismiss(setup, observer, entered.task, rejudged.regrade)
    with pytest.raises(NotFound):
        await regrades.dismiss(setup, manager, entered.task, uuid.uuid4())
    await regrades.dismiss(setup, manager, entered.task, rejudged.regrade)
    await regrades.dismiss(setup, manager, entered.task, rejudged.regrade)

    assert await regrades.latest(setup, observer, SPRING) == ()
    # Its gradings still read by the regrade.
    assert len(await regrades.gradings_of(setup, observer, SPRING, rejudged.regrade)) == 1
    # A newer regrade shows again.
    await _stands(setup, 1, **ACCEPTED)
    again = await gradings.rejudge(setup, manager, entered.task)
    (summary,) = await regrades.latest(setup, observer, SPRING)
    assert summary.id == again.regrade


async def test_a_rejudge_that_makes_nothing_is_no_regrade(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    rejudged = await gradings.rejudge(setup, manager, entered.task)

    assert (rejudged.queued, rejudged.regrade) == (0, None)
    assert await regrades.latest(setup, manager, SPRING) == ()


async def test_a_regrade_of_a_task_not_observed_reads_as_none(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered, clock, 1)
    await _stands(setup, 1, **ACCEPTED)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    rejudged = await gradings.rejudge(setup, manager, entered.task)
    assert rejudged.regrade is not None
    other = _held(acme, Scope("acme", "spring", "other"), Role.MANAGER)

    assert await regrades.latest(setup, other, SPRING) == ()
    assert await regrades.gradings_of(setup, other, SPRING, rejudged.regrade) == ()
    assert await regrades.gradings_of(setup, manager, SPRING, uuid.uuid4()) == ()
