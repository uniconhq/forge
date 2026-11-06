"""A grading that ends in `system_error` is tried again once by itself
(#442): the CI's report and a read that finds it overdue each start the
retry once their unit of work commits, in the background, and a start that
fails is retried by the first read a minute after; nothing runs on a
timer. The new attempt is marked `automatic`, a second
failure waits for staff, and staff retry as often as they like, each staff
attempt that fails getting its one retry by itself. Two retries by itself of
one grading at once make one attempt, and a staff cancel or retry before it
leaves it nothing to do. While the retry runs, a submission with no result
reads as queued or grading again, and once its second failure waits, as
delayed, counted among the contest's failed gradings. Each new attempt
restarts the run before it (#631), or starts a new one when there is none
or the CI refuses (#635).
"""

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading
from forge.domain.errors import CiRequestRefused, Conflict, Unavailable
from forge.domain.grading import NEVER_BEGAN, OVERDUE, GradingStatus
from forge.domain.ids import OrgId, RunId
from forge.domain.roles import Role, Scope
from forge.domain.submission_state import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import gradings, runs, submissions
from forge.services.gradings import NO_ANSWER, NO_RUN, START_RETRY_WAIT
from forge.settings import Settings
from forge.testing import APP_URL, FORGE_URL, FakeClock
from tests.services.conftest import RUNNING, SPRING, Acme, Entered, organiser, upload, write_contest
from tests.services.test_steps import _job, _mint, _report, _step, _to_run

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
            "code": SubmittedInput(uploads=(made.id,)),
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
                    select(Grading)
                    .where(Grading.pipeline != "save")
                    .order_by(Grading.submission_number, Grading.attempt)
                )
            ).scalars()
        )


async def _first_step(setup: Setup, row: Grading) -> str:
    """The name of the first step the grading's pipeline runs."""
    [first, *_] = await _to_run(setup, row.id, "grading")
    return first


async def _fail(setup: Setup, acme: Acme, row: Grading) -> GradingStatus:
    """The grading's first step wrote an `outputs.json` that does not read
    and its report said so, a system error, and whatever that started in
    the background has finished.
    """
    step = await _first_step(setup, row)
    await _step(setup, acme, row.id, "grading", step, lambda job: {"outputs.json": b"{"})
    status = await _report(setup, row.id, "grading")
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

    assert await _fail(setup, acme, first) == GradingStatus.SYSTEM_ERROR

    old, new = await _rows(setup)
    assert (old.id, old.status, old.automatic) == (first.id, GradingStatus.SYSTEM_ERROR, False)
    assert (new.attempt, new.status, new.automatic) == (2, GradingStatus.DISPATCHED, True)
    assert new.publication_id == old.publication_id
    told = await _told(setup, entered)
    assert (told.attempt, told.status) == (2, SubmissionState.GRADING)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    listed = {
        entry.grading.attempt: entry.grading
        for entry in await gradings.list_gradings(setup, manager, entered.task)
    }
    assert (listed[2].automatic, listed[1].automatic) == (True, False)


async def test_a_second_failure_waits_for_staff_and_reads_as_delayed(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _fail(setup, acme, first)
    _, second = await _rows(setup)

    await _fail(setup, acme, second)

    assert [(row.attempt, row.status) for row in await _rows(setup)] == [
        (1, GradingStatus.SYSTEM_ERROR),
        (2, GradingStatus.SYSTEM_ERROR),
    ]
    told = await _told(setup, entered)
    assert (told.attempt, told.status, told.reason) == (2, SubmissionState.DELAYED, None)
    assert [run["verdict"] for run in told.runs] == ["system_error"]
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
    await _fail(setup, acme, first)
    _, second = await _rows(setup)
    await _fail(setup, acme, second)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    third = await gradings.retry(setup, manager, second.id)
    await setup.settle()
    assert (third.attempt, third.automatic) == (3, False)
    *_, made = await _rows(setup)
    await _fail(setup, acme, made)

    *_, fourth = await _rows(setup)
    assert (fourth.attempt, fourth.status, fourth.automatic) == (
        4,
        GradingStatus.DISPATCHED,
        True,
    )
    await _fail(setup, acme, fourth)
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
    step = await _first_step(setup, first)
    await _job(setup, first.id, step, await _mint(setup, first.id, "grading", step))
    (running,) = await _rows(setup)
    assert running.deadline_at is not None
    clock.set(running.deadline_at)
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)

    await gradings.feed(setup, observer, SPRING)
    await setup.settle()

    old, new = await _rows(setup)
    assert (old.status, old.error) == (GradingStatus.SYSTEM_ERROR, OVERDUE)
    assert (new.attempt, new.automatic) == (2, True)


async def test_a_start_that_fails_is_retried_by_the_first_read_a_minute_after(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    acme.fake.ci.refuse_starts = 1

    await _submit(setup, acme, entered)
    clock.advance(START_RETRY_WAIT - timedelta(seconds=1))
    await _told(setup, entered)

    (only,) = await _rows(setup)
    assert (only.status, only.error) == (GradingStatus.SYSTEM_ERROR, NO_RUN)
    clock.advance(timedelta(seconds=1))
    await _told(setup, entered)
    old, new = await _rows(setup)
    assert (old.id, old.status) == (only.id, GradingStatus.SYSTEM_ERROR)
    assert (new.attempt, new.automatic, new.status) == (2, True, GradingStatus.DISPATCHED)
    assert (old.run_id, new.restarts) == (None, None)
    assert acme.fake.ci.runs[RunId(str(new.run_id))].variables["UNICON_GRADING_ID"] == str(new.id)


async def test_a_retry_that_cannot_start_either_waits_for_staff(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    async def down(*args: Any, **kwargs: Any) -> RunId:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", down)

    await _submit(setup, acme, entered)
    for _ in range(2):
        clock.advance(START_RETRY_WAIT)
        await _told(setup, entered)

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
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _broken(setup, first)

    started: list[object] = []
    real = gradings.watchdog.retry_by_itself

    async def counted(ctx: Any, grading: Any) -> Grading | None:
        started.append(grading)
        return await real(ctx, grading)

    monkeypatch.setattr(gradings.watchdog, "retry_by_itself", counted)

    await asyncio.gather(
        *(submissions.one(setup, entered.session, entered.task, 1) for _ in range(5))
    )
    await setup.settle()

    # One start among them, not one each that finds nothing left to do.
    assert started == [first.id]
    assert [(row.attempt, row.automatic) for row in await _rows(setup)] == [
        (1, False),
        (2, True),
    ]


READERS = ["task's gradings", "queue depth"]
"""A task's readers; a leaderboard reads them too, and starts the retry of
a failure it finds (`tests.services.test_pipelines`)."""


async def _read(setup: Setup, acme: Acme, entered: Entered, reader: str) -> None:
    """The contest read the way `reader` reads it."""
    observer = await organiser(setup, acme.fake, 7, SUM, Role.OBSERVER)
    match reader:
        case "task's gradings":
            assert await gradings.list_gradings(setup, observer, entered.task)
        case "queue depth":
            contest = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)
            await gradings.queue_depth(setup, contest, SPRING)


@pytest.mark.parametrize("reader", READERS)
async def test_each_reader_of_a_failure_starts_its_retry_by_itself(
    setup: Setup, acme: Acme, entered: Entered, reader: str
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _broken(setup, first)

    await _read(setup, acme, entered, reader)
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


async def test_a_staff_cancel_beside_the_retry_by_itself_waits_for_it_and_finds_the_new_attempt(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _broken(setup, first)
    made = asyncio.Event()
    go_on = asyncio.Event()
    real = gradings.attempt_history._next_attempt

    def next_attempt(*args: Any, **kwargs: Any) -> Grading:
        found = real(*args, **kwargs)
        made.set()
        return found

    monkeypatch.setattr(gradings.attempt_history, "_next_attempt", next_attempt)

    async def retry() -> Grading | None:
        async with setup.unit_of_work() as ctx:
            found = await gradings.retry_by_itself(ctx, first.id)
            await go_on.wait()
            return found

    async def cancel() -> None:
        await made.wait()
        cancelling = asyncio.create_task(gradings.cancel(setup, manager, first.id, "Ended."))
        await asyncio.sleep(0.2)
        go_on.set()
        with pytest.raises(Conflict):
            await cancelling

    retried, _ = await asyncio.gather(retry(), cancel())
    await setup.settle()

    assert retried is not None
    assert [(row.attempt, row.status, row.cancel_reason) for row in await _rows(setup)] == [
        (1, GradingStatus.SYSTEM_ERROR, None),
        (2, GradingStatus.DISPATCHED, None),
    ]


async def _asked(
    setup: Setup, acme: Acme, clock: FakeClock, run: RunId, *, restarting: RunId | None = None
) -> str:
    """Asked with `run`'s variables, for a restart of `restarting` when given."""
    request = acme.fake.grading.config_request(
        OrgId("acme"), acme.fake.ci.runs[run].variables, now=clock.now(), run=restarting
    )
    return json.dumps(json.loads((await runs.config(setup, request)).body))


async def test_a_retry_by_itself_restarts_the_failed_run_answered_with_its_own_steps(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    run = RunId(str(first.run_id))
    restart = acme.fake.grading.restart_pipeline
    answered: list[str] = []

    async def asking(*args: Any) -> RunId:
        made = await restart(*args)
        answered.append(await _asked(setup, acme, clock, made, restarting=args[-1]))
        return made

    monkeypatch.setattr(acme.fake.grading, "restart_pipeline", asking)
    await _fail(setup, acme, first)

    _, new = await _rows(setup)
    restarted = acme.fake.ci.runs[RunId(str(new.run_id))]
    assert (new.restarts, restarted.parent, new.status) == (run, run, GradingStatus.DISPATCHED)
    # The variables name the first attempt, but the new one is answered.
    assert restarted.variables == acme.fake.ci.runs[run].variables
    assert len(acme.fake.calls_to("start_pipeline")) == 1
    [answer] = answered
    assert str(new.id) in answer and str(first.id) not in answer
    with pytest.raises(CiRequestRefused):
        await _asked(setup, acme, clock, RunId(str(new.run_id)))


async def test_a_staff_retry_restarts_the_run_of_the_attempt_it_retries(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    await _fail(setup, acme, first)
    _, second = await _rows(setup)
    await _fail(setup, acme, second)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    await gradings.retry(setup, manager, second.id)
    await setup.settle()

    *_, third = await _rows(setup)
    assert (third.attempt, third.restarts) == (3, second.run_id)
    assert acme.fake.ci.runs[RunId(str(third.run_id))].parent == second.run_id
    assert len(acme.fake.calls_to("start_pipeline")) == 1


@pytest.mark.parametrize("gone", [False, True], ids=["refused", "not-held"])
async def test_a_restart_the_ci_refuses_or_no_longer_holds_starts_a_new_run(
    setup: Setup, acme: Acme, entered: Entered, gone: bool
) -> None:
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    if gone:
        del acme.fake.ci.runs[RunId(str(first.run_id))]
    else:
        acme.fake.ci.refuse_restarts = 1

    await _fail(setup, acme, first)

    _, new = await _rows(setup)
    started = acme.fake.ci.runs[RunId(str(new.run_id))]
    assert (new.status, new.restarts, started.parent) == (
        GradingStatus.DISPATCHED,
        first.run_id,
        None,
    )
    assert started.variables["UNICON_GRADING_ID"] == str(new.id)
    assert len(acme.fake.calls_to("start_pipeline")) == 2


async def test_a_restart_is_answered_only_for_the_latest_attempt_that_restarts_a_run(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    """So an old run restarted by hand isn't answered while the platform
    starts a new run of its own."""
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    old = RunId(str(first.run_id))
    await _fail(setup, acme, first)
    _, new = await _rows(setup)
    new_run = RunId(str(new.run_id))

    for restarts in (None, str(old)):
        async with setup.unit_of_work() as ctx:
            await ctx.db.execute(
                update(Grading)
                .where(Grading.id == new.id)
                .values(status=GradingStatus.QUEUED, restarts=restarts)
            )
        if restarts is None:
            with pytest.raises(CiRequestRefused):
                await _asked(setup, acme, clock, old, restarting=old)
        else:
            assert str(new.id) in await _asked(setup, acme, clock, old, restarting=old)
    assert str(new.id) in await _asked(setup, acme, clock, new_run, restarting=old)


async def test_a_restart_of_another_run_of_the_line_is_not_answered(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    """So a run restarted by hand while the platform restarts another can't
    run with the new attempt's steps too."""
    await _submit(setup, acme, entered)
    (first,) = await _rows(setup)
    old = RunId(str(first.run_id))
    await _fail(setup, acme, first)
    _, new = await _rows(setup)
    new_run = RunId(str(new.run_id))
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading).where(Grading.id == new.id).values(status=GradingStatus.QUEUED)
        )

    for restarting in (new_run, None):
        with pytest.raises(CiRequestRefused):
            await _asked(setup, acme, clock, new_run, restarting=restarting)
    assert str(new.id) in await _asked(setup, acme, clock, new_run, restarting=old)
