"""The first `submissions.max` of an entrant's submissions to get a result
are the ones that count (#445, TASK-FORMAT.md sections 1.2 and 3.5). A
delayed submission uses up none while it waits, so the entrant may submit
meanwhile; but it is graded again, by its own retry, by staff's or by a
rejudge, only while the entrant's submissions with a result and those
waiting that are not delayed, with the delayed ones made before it, are
fewer than the most. Otherwise its new attempt ends over the limit at once,
run by nothing: its contestant reads "over_limit" with the most they had
reached, and it counts on no leaderboard and against nothing. The organisers'
list says beforehand when a retry would end so, a staff retry is held to the
same rule, and room that opens, the most raised, lets a staff retry grade
it.
"""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.errors import ConfirmationRequired, SubmissionLimit
from forge.domain.grading import LOST_CHECK_AFTER, GradingStatus, RunState
from forge.domain.identity import PLATFORM
from forge.domain.ids import RunId
from forge.domain.roles import Role, Scope
from forge.domain.submission_state import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import gradings, submissions, task_save
from forge.settings import Settings
from forge.testing import APP_URL, FORGE_URL, FakeClock
from tests.services.conftest import SPRING, Acme, Entered, organiser, upload

SOURCE = b"print(sum(map(int, input().split())))\n"
SUM = Scope("acme", "spring", "sum")
FAILED: dict[str, Any] = {"status": "system_error", "error": "The checker crashed."}
GRADED: dict[str, Any] = {
    "status": "done",
    "result": {
        "schema_version": 5,
        "stopped": None,
        "stopped_by": None,
        "tests": [{"test": "main/1", "outcome": "accepted", "values": {}}],
        "values": {},
        "run_log": "http://machines.test/unicon-results/logs/x/1.log",
        "error": None,
    },
}


async def _submit(setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, key: str) -> None:
    clock.advance(timedelta(seconds=31))
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    await setup.settle()


async def _most(setup: Setup, acme: Acme, entered: Entered, most: int) -> None:
    """The task's `submissions.max` set to `most` by ada's save, which she
    confirms, as every publishing save asks while the contest runs.
    """
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    content = current.content.split(b"submissions:")[0] + f"submissions: {{max: {most}}}\n".encode()
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    saved = await task_save.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(content, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, task_save.Published), saved


async def _rows(setup: Setup) -> list[Grading]:
    async with setup.unit_of_work() as ctx:
        return list(
            (
                await ctx.db.execute(
                    select(Grading).order_by(Grading.submission_number, Grading.attempt)
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


def _latest(rows: list[Grading], number: int) -> Grading:
    return max(
        (row for row in rows if row.submission_number == number), key=lambda row: row.attempt
    )


async def _told(setup: Setup, entered: Entered, number: int) -> submissions.Result:
    found = (await submissions.one(setup, entered.session, entered.task, number)).grading
    await setup.settle()
    assert found is not None
    return found


class TestItsOwnRetry:
    @pytest.fixture
    def settings(self, migrated_database_url: str) -> Settings:
        return Settings.for_tests(
            database_url=migrated_database_url,
            public_url=APP_URL,
            forge_public_url=FORGE_URL,
            retry_failed=True,
        )

    async def test_with_room_a_delayed_submission_is_graded_again(
        self, setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
    ) -> None:
        await _most(setup, acme, entered, 2)
        await _submit(setup, acme, entered, clock, "key-0001-aaaa")
        await _stands(setup, 1, **FAILED)

        await _told(setup, entered, 1)

        retried = _latest(await _rows(setup), 1)
        assert (retried.attempt, retried.status, retried.automatic) == (
            2,
            GradingStatus.DISPATCHED,
            True,
        )

    async def test_without_room_it_ends_over_the_limit_and_counts_for_nothing(
        self, setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
    ) -> None:
        await _most(setup, acme, entered, 1)
        await _submit(setup, acme, entered, clock, "key-0001-aaaa")
        await _stands(setup, 1, **FAILED)
        # Delayed, the first uses up nothing, so a second is taken, and waits.
        await _submit(setup, acme, entered, clock, "key-0002-bbbb")

        # The read that finds it delayed starts its retry, which finds no room.
        assert (await _told(setup, entered, 1)).status is SubmissionState.DELAYED
        told = await _told(setup, entered, 1)

        assert (told.status, told.limit, told.groups) == (SubmissionState.OVER_LIMIT, 1, ())
        over = _latest(await _rows(setup), 1)
        assert (over.attempt, over.status, over.automatic, over.submission_limit) == (
            2,
            GradingStatus.OVER_LIMIT,
            True,
            1,
        )
        assert over.run_id is None and over.finished_at is not None
        assert len(acme.fake.calls_to("start_run")) == 2
        # Nothing more is started by reading it again, and it waits for no one.
        await _told(setup, entered, 1)
        assert len(await _rows(setup)) == 3
        observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)
        assert (await gradings.queue_depth(setup, observer, SPRING)).failed == 0
        # The second holds the one place, graded or not.
        with pytest.raises(SubmissionLimit):
            await _submit(setup, acme, entered, clock, "key-0003-cccc")
        await _stands(setup, 2, **GRADED)
        with pytest.raises(SubmissionLimit):
            await _submit(setup, acme, entered, clock, "key-0003-cccc")


async def test_a_staff_retry_is_told_beforehand_and_held_to_the_same_rule(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _most(setup, acme, entered, 1)
    await _submit(setup, acme, entered, clock, "key-0001-aaaa")
    await _stands(setup, 1, **FAILED)
    await _submit(setup, acme, entered, clock, "key-0002-bbbb")

    listed = {
        entry.grading.submission_number: entry.grading
        for entry in await gradings.list_gradings(setup, manager, entered.task)
    }
    assert (listed[1].retry_limit, listed[2].retry_limit) == (1, None)
    first = listed[1].id

    retried = await gradings.retry(setup, manager, first)

    assert (retried.attempt, retried.status, retried.limit, retried.latest) == (
        2,
        GradingStatus.OVER_LIMIT,
        1,
        True,
    )
    assert len(acme.fake.calls_to("start_run")) == 2
    told = await _told(setup, entered, 1)
    assert (told.status, told.limit) == (SubmissionState.OVER_LIMIT, 1)
    # Retried again with no room, it ends over the limit again.
    again = await gradings.retry(setup, manager, retried.id)
    assert (again.attempt, again.status) == (3, GradingStatus.OVER_LIMIT)

    # The most raised, there is room, and the list says a retry grades it.
    await _most(setup, acme, entered, 2)
    (latest,) = [
        entry.grading
        for entry in await gradings.list_gradings(setup, manager, entered.task)
        if entry.grading.submission_number == 1 and entry.grading.latest
    ]
    assert (latest.status, latest.limit, latest.retry_limit) == (GradingStatus.OVER_LIMIT, 1, None)
    graded = await gradings.retry(setup, manager, again.id)
    assert (graded.attempt, graded.status, graded.limit) == (4, GradingStatus.QUEUED, None)


async def test_the_earliest_delayed_submissions_are_the_ones_graded(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _most(setup, acme, entered, 2)
    await _submit(setup, acme, entered, clock, "key-0001-aaaa")
    await _stands(setup, 1, **GRADED)
    await _submit(setup, acme, entered, clock, "key-0002-bbbb")
    await _stands(setup, 2, **FAILED)
    await _submit(setup, acme, entered, clock, "key-0003-cccc")
    await _stands(setup, 3, **FAILED)
    rows = await _rows(setup)

    # One place is left: the later one, retried first, leaves it to the earlier.
    later = await gradings.retry(setup, manager, _latest(rows, 3).id)
    earlier = await gradings.retry(setup, manager, _latest(rows, 2).id)

    assert (later.status, earlier.status) == (GradingStatus.OVER_LIMIT, GradingStatus.QUEUED)


async def test_a_save_that_lowers_the_most_regrades_under_the_most_it_saves(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    # Two graded and one delayed under a most of 3; a save that changes a
    # test and lowers the most to 2 grades the two again and ends the
    # delayed one over the limit, as its question said.
    await _most(setup, acme, entered, 3)
    for number, key in enumerate(("key-0001-aaaa", "key-0002-bbbb", "key-0003-cccc"), start=1):
        await _submit(setup, acme, entered, clock, key)
        await _stands(setup, number, **(FAILED if number == 3 else GRADED))
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    lowered = current.content.split(b"submissions:")[0] + b"submissions: {max: 2}\n"
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    edits = {
        "task.yaml": Edit(lowered, head.tokens["task.yaml"]),
        "tests/main/1/answer": Edit(b"4\n", head.tokens["tests/main/1/answer"]),
    }

    with pytest.raises(ConfirmationRequired) as asked:
        await task_save.save(setup, acme.ada, entered.task, edits)
    saved = await task_save.save(setup, acme.ada, entered.task, edits, confirm=True)

    assert isinstance(saved, task_save.Published) and saved.regraded == 2
    assert asked.value.extra["regrades"] == saved.regraded
    over = [
        (row.submission_number, row.status == GradingStatus.OVER_LIMIT)
        for row in await _rows(setup)
        if row.attempt == 2
    ]
    assert over == [(1, False), (2, False), (3, True)]


async def test_a_saves_question_counts_a_run_the_ci_lost_as_its_regrade_does(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    # One place, taken by the first's result; the second's run is lost, so
    # it is delayed: the regrade grades the first alone, and the question
    # says so.
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _most(setup, acme, entered, 2)
    await _submit(setup, acme, entered, clock, "key-0001-aaaa")
    await _stands(setup, 1, **GRADED)
    await _submit(setup, acme, entered, clock, "key-0002-bbbb")
    await _most(setup, acme, entered, 1)
    second = _latest(await _rows(setup), 2)
    assert second.run_id is not None
    acme.fake.ci.runs[RunId(second.run_id)].ci_state = RunState.LOST
    clock.advance(LOST_CHECK_AFTER)

    async with setup.unit_of_work() as ctx:
        asked = await gradings.to_regrade(ctx, entered.task)
    rejudged = await gradings.rejudge(setup, manager, entered.task)

    assert (rejudged.queued, rejudged.over_limit) == (1, 1)
    assert asked == rejudged.queued


async def test_a_rejudge_grades_the_earliest_delayed_and_ends_the_rest_over_the_limit(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _most(setup, acme, entered, 2)
    await _submit(setup, acme, entered, clock, "key-0001-aaaa")
    await _stands(setup, 1, **GRADED)
    await _submit(setup, acme, entered, clock, "key-0002-bbbb")
    await _stands(setup, 2, **FAILED)
    await _submit(setup, acme, entered, clock, "key-0003-cccc")
    await _stands(setup, 3, **FAILED)
    async with setup.unit_of_work() as ctx:
        # What a save's confirmation would say runs again: not the third.
        assert await gradings.to_regrade(ctx, entered.task) == 2
        assert await gradings.to_regrade(ctx, entered.task, limit=3) == 3

    rejudged = await gradings.rejudge(setup, manager, entered.task)

    assert (rejudged.queued, rejudged.over_limit) == (2, 1)
    rows = await _rows(setup)
    assert [(row.submission_number, row.attempt, row.status) for row in rows] == [
        (1, 1, GradingStatus.DONE),
        (1, 2, GradingStatus.DISPATCHED),
        (2, 1, GradingStatus.SYSTEM_ERROR),
        (2, 2, GradingStatus.DISPATCHED),
        (3, 1, GradingStatus.SYSTEM_ERROR),
        (3, 2, GradingStatus.OVER_LIMIT),
    ]
    # Again: the one over the limit, still without room, is left as it is.
    again = await gradings.rejudge(setup, manager, entered.task)
    assert (again.queued, again.left_running, again.over_limit) == (0, 2, 0)
    assert len(await _rows(setup)) == 6
