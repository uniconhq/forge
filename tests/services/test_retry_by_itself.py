"""A grading that ends in `system_error` is tried again once by itself
(#442): the CI's report, a start that fails and a read that finds it overdue
each start the retry once their unit of work commits, in the background,
and nothing runs on a timer. The new attempt is marked `automatic`, a second
failure waits for staff, and staff retry as often as they like, each staff
attempt that fails getting its one retry by itself. Two retries by itself of
one grading at once make one attempt, and a staff cancel or retry before it
leaves it nothing to do. While the retry runs, a submission with no result
reads as queued or grading again, and once its second failure waits, as
delayed, counted among the contest's failed gradings.
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading
from forge.domain import exact_json
from forge.domain.errors import Unavailable
from forge.domain.grading import NEVER_BEGAN, OVERDUE, GradingStatus, callback_token, envelope_key
from forge.domain.ids import RunId
from forge.domain.roles import Role, Scope
from forge.domain.showing import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import gradings, runs, submissions
from forge.services.gradings import NO_ANSWER, NO_RUN
from forge.settings import Settings
from forge.testing import APP_URL, FORGE_URL, FakeClock
from tests.services.conftest import SPRING, Acme, Entered, organiser, upload

SOURCE = b"print(sum(map(int, input().split())))\n"
SUM = Scope("acme", "spring", "sum")
CRASHED = "The step check wrote no outcome."


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_URL,
        retry_failed=True,
    )


async def _submit(setup: Setup, acme: Acme, entered: Entered, key: str = "key-0001-aaaa") -> None:
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


async def _rows(setup: Setup) -> list[Grading]:
    async with setup.unit_of_work() as ctx:
        return list(
            (
                await ctx.db.execute(
                    select(Grading).order_by(Grading.submission_number, Grading.attempt)
                )
            ).scalars()
        )


async def _fail(setup: Setup, row: Grading) -> GradingStatus:
    """The grading's run fetched its envelope and reported a system error,
    and whatever that started in the background has finished.
    """
    document = await runs.envelope(
        setup, row.id, envelope_key(setup.settings.token_encryption_key_bytes, row.id)
    )
    result = {
        "schema_version": 5,
        "stopped": "system_error",
        "stopped_by": None,
        "tests": [],
        "values": {},
        "run_log": document["log_put"].split("?", 1)[0],
        "error": CRASHED,
    }
    token = callback_token(setup.settings.token_encryption_key_bytes, row.id)
    status = await runs.callback(
        setup,
        row.id,
        f"Bearer {token}",
        exact_json.dumps({"event": "finished", "result": result}).encode(),
    )
    await setup.settle()
    return status


async def _told(setup: Setup, entered: Entered) -> Any:
    found = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    await setup.settle()
    assert found is not None
    return found


async def test_a_failure_the_ci_reports_is_retried_once_by_itself(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)

    assert await _fail(setup, first) == GradingStatus.SYSTEM_ERROR

    old, new = await _rows(setup)
    assert (old.id, old.status, old.automatic) == (first.id, GradingStatus.SYSTEM_ERROR, False)
    assert (new.attempt, new.status, new.automatic) == (2, GradingStatus.DISPATCHED, True)
    assert new.publication_id == old.publication_id
    told = await _told(setup, entered)
    assert (told.attempt, told.status) == (2, SubmissionState.GRADING)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    listed = {
        entry.grading.attempt: entry.grading
        for entry in await gradings.list(setup, manager, entered.task)
    }
    assert (listed[2].automatic, listed[1].automatic) == (True, False)


async def test_a_second_failure_waits_for_staff_and_reads_as_delayed(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _fail(setup, first)
    _, second = await _rows(setup)

    await _fail(setup, second)

    assert [(row.attempt, row.status) for row in await _rows(setup)] == [
        (1, GradingStatus.SYSTEM_ERROR),
        (2, GradingStatus.SYSTEM_ERROR),
    ]
    told = await _told(setup, entered)
    assert (told.attempt, told.status, told.groups, told.reason) == (
        2,
        SubmissionState.DELAYED,
        (),
        None,
    )
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)
    assert (await gradings.queue_depth(setup, observer, SPRING)).failed == 1
    # Reading it again starts nothing more.
    await _told(setup, entered)
    assert len(await _rows(setup)) == 2


async def test_staff_retry_as_often_as_they_like_each_attempt_retried_once_by_itself(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _fail(setup, first)
    _, second = await _rows(setup)
    await _fail(setup, second)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    third = await gradings.retry(setup, manager, second.id)
    await setup.settle()
    assert (third.attempt, third.automatic) == (3, False)
    *_, made = await _rows(setup)
    await _fail(setup, made)

    *_, fourth = await _rows(setup)
    assert (fourth.attempt, fourth.status, fourth.automatic) == (
        4,
        GradingStatus.DISPATCHED,
        True,
    )
    await _fail(setup, fourth)
    again = await gradings.retry(setup, manager, fourth.id)
    await setup.settle()
    assert again.attempt == 5


async def test_a_grading_a_read_finds_overdue_is_retried_by_itself_and_ended_saying_why(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    run = RunId(str(first.run_id))
    clock.advance(timedelta(hours=3))

    told = await _told(setup, entered)

    # The read that found it overdue told it as delayed, and started the retry.
    assert (told.attempt, told.status) == (1, SubmissionState.DELAYED)
    old, new = await _rows(setup)
    assert (old.status, old.error) == (
        GradingStatus.SYSTEM_ERROR,
        NEVER_BEGAN,
    )
    assert acme.fake.ci.runs[run].cancelled is True
    assert (new.attempt, new.automatic, new.status) == (2, True, GradingStatus.DISPATCHED)
    assert (await _told(setup, entered)).status == SubmissionState.GRADING


async def test_a_run_past_its_deadline_is_retried_by_itself_once_found(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await runs.envelope(
        setup, first.id, envelope_key(setup.settings.token_encryption_key_bytes, first.id)
    )
    (running,) = await _rows(setup)
    assert running.deadline_at is not None
    clock.set(running.deadline_at)
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)

    await gradings.feed(setup, observer, SPRING)
    await setup.settle()

    old, new = await _rows(setup)
    assert (old.status, old.error) == (GradingStatus.SYSTEM_ERROR, OVERDUE)
    assert (new.attempt, new.automatic) == (2, True)


async def test_a_start_that_fails_is_retried_by_itself(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.ci.refuse_starts = 1

    await _submit(setup, acme, entered)

    old, new = await _rows(setup)
    assert (old.status, old.error) == (GradingStatus.SYSTEM_ERROR, NO_RUN)
    assert (new.attempt, new.automatic, new.status) == (2, True, GradingStatus.DISPATCHED)


async def test_a_retry_that_cannot_start_either_waits_for_staff(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: Any, **kwargs: Any) -> RunId:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(acme.fake.grading, "start_run", down)

    await _submit(setup, acme, entered)

    assert [(row.attempt, row.status, row.error, row.automatic) for row in await _rows(setup)] == [
        (1, GradingStatus.SYSTEM_ERROR, NO_ANSWER, False),
        (2, GradingStatus.SYSTEM_ERROR, NO_ANSWER, True),
    ]


async def _broken(setup: Setup, row: Grading) -> None:
    """The grading written as a system error straight into its row, which
    nothing notices until it is read.
    """
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.id == row.id)
            .values(status=GradingStatus.SYSTEM_ERROR, error=CRASHED)
        )


async def test_two_noticing_one_failure_at_once_make_one_retry(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _broken(setup, first)

    async def notice() -> Grading | None:
        async with setup.unit_of_work() as ctx:
            return await gradings.retry_by_itself(ctx, first.id)

    made = await asyncio.gather(notice(), notice(), notice())
    await setup.settle()

    assert len([found for found in made if found is not None]) == 1
    assert [(row.attempt, row.automatic) for row in await _rows(setup)] == [
        (1, False),
        (2, True),
    ]


async def test_many_readers_finding_one_failure_together_start_one_retry(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _broken(setup, first)

    await asyncio.gather(
        *(submissions.one(setup, entered.session, entered.task, 1) for _ in range(5))
    )
    await setup.settle()

    assert [(row.attempt, row.automatic) for row in await _rows(setup)] == [
        (1, False),
        (2, True),
    ]


async def test_a_staff_cancel_or_retry_first_leaves_the_retry_by_itself_nothing_to_do(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _broken(setup, first)
    await gradings.cancel(setup, manager, first.id, "It cannot be graded.")
    clock.advance(timedelta(seconds=31))
    await _submit(setup, acme, entered, key="key-0002-bbbb")
    *_, second = await _rows(setup)
    await _broken(setup, second)
    retried = await gradings.retry(setup, manager, second.id)
    await setup.settle()

    async with setup.unit_of_work() as ctx:
        cancelled = await gradings.retry_by_itself(ctx, first.id)
    async with setup.unit_of_work() as ctx:
        replaced = await gradings.retry_by_itself(ctx, second.id)

    assert (cancelled, replaced) == (None, None)
    rows = await _rows(setup)
    assert [(row.submission_number, row.attempt, row.automatic) for row in rows] == [
        (1, 1, False),
        (2, 1, False),
        (2, 2, False),
    ]
    assert rows[-1].id == retried.id
