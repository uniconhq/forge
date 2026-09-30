"""A grading from its queued row to its run at the CI, over a real Postgres
and the fake. The dispatcher starts each run once, as the org account,
pinned to the platform pool, with the run's variables, and puts a failed
start back in the queue with a reason and a wait; a start whose answer was
lost is found rather than sent again. A run past its deadline that the CI
has not handed to a machine is looked at again, and one that died is a
system error.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading
from forge.domain.grading import (
    RUN_TIMEOUT,
    GradingRun,
    GradingStatus,
    RunStatus,
    envelope_key,
)
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import RunId
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import dispatch, gradings, submissions
from forge.testing import FakeClock, tick
from tests.services.conftest import Acme, Entered, upload

KEY = "key-0001-aaaa"
SOURCE = b"print(sum(map(int, input().split())))\n"


async def _submit(setup: Setup, acme: Acme, entered: Entered, key: str = KEY) -> Grading:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {"submission": SubmittedInput(uploads=(made.id,), language="python")},
        idempotency_key=key,
    )
    return (await _rows(setup))[-1]


async def _rows(setup: Setup) -> list[Grading]:
    async with setup.unit_of_work() as ctx:
        return list(
            (
                await ctx.db.execute(
                    select(Grading).order_by(Grading.submission_number, Grading.attempt)
                )
            ).scalars()
        )


async def _row(setup: Setup, grading: uuid.UUID) -> Grading:
    async with setup.unit_of_work() as ctx:
        found = await gradings.find(ctx, grading)
        assert found is not None
        return found


async def _set(setup: Setup, grading: uuid.UUID, **values: Any) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).where(Grading.id == grading).values(**values))


def _key(setup: Setup, row: Grading) -> str:
    return envelope_key(setup.settings.token_encryption_key_bytes, row.id, run=row.requeues)


async def test_a_queued_grading_becomes_one_run_as_the_org_account_on_the_platform_pool(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)

    await tick(setup, "gradings.dispatch")

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert after.run_id is not None
    assert (after.dispatched_at, after.deadline_at) == (clock.now(), clock.now() + RUN_TIMEOUT)
    assert after.wait_reason == dispatch.WAITING_FOR_MACHINE
    [started] = acme.fake.calls_to("start_run")
    assert isinstance(started.identity, AsOrgAccount)
    assert started.identity.org == "acme"
    [publication] = await acme.fake.workspaces.list_publications(entered.task)
    variables = started.arguments["variables"]
    assert variables["UNICON_GRADING_ID"] == str(row.id)
    assert variables["UNICON_COMPUTE"] == "pool:platform"
    assert variables["UNICON_PUBLICATION_COMMIT"] == publication.version
    assert variables["UNICON_SUBMISSION_COMMIT"] == row.submission_version
    assert variables["UNICON_ENVELOPE_URL"] == (
        f"http://app.test/api/v1/gradings/{row.id}/envelope?key={_key(setup, row)}"
    )


async def test_two_pollers_at_once_start_the_run_once(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first poller is held inside its start, its row taken on its own
    connection, while a second one ticks on another; the second finds nothing
    to take, and once the first commits nothing is left for a third.
    """
    row = await _submit(setup, acme, entered)
    starting, release = asyncio.Event(), asyncio.Event()
    start = acme.fake.grading.start_run

    async def held_start(as_: AsOrgAccount, run: GradingRun) -> RunId:
        starting.set()
        await release.wait()
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_run", held_start)
    first = asyncio.create_task(dispatch.poller().tick(setup.unit_of_work))
    await asyncio.wait_for(starting.wait(), timeout=10)

    second = await dispatch.poller().tick(setup.unit_of_work)
    release.set()

    assert (await first, second) == (1, 0)
    assert await dispatch.poller().tick(setup.unit_of_work) == 0
    assert len(acme.fake.calls_to("start_run")) == 1
    assert (await _row(setup, row.id)).status == GradingStatus.DISPATCHED


async def test_a_start_without_a_run_waits_with_a_reason_and_a_later_try_starts_it(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    acme.fake.state.refuse_starts = 2

    await tick(setup, "gradings.dispatch")
    first = await _row(setup, row.id)
    assert (first.status, first.wait_reason, first.start_failures) == (
        GradingStatus.QUEUED,
        dispatch.NO_RUN,
        1,
    )
    assert first.retry_at == clock.now() + timedelta(seconds=5)
    await tick(setup, "gradings.dispatch")
    assert len(acme.fake.calls_to("start_run")) == 1

    clock.advance(timedelta(seconds=5))
    await tick(setup, "gradings.dispatch")
    second = await _row(setup, row.id)
    assert (second.start_failures, second.retry_at) == (2, clock.now() + timedelta(seconds=10))

    clock.advance(timedelta(seconds=10))
    await tick(setup, "gradings.dispatch")
    third = await _row(setup, row.id)
    assert (third.status, third.start_failures, third.retry_at) == (
        GradingStatus.DISPATCHED,
        0,
        None,
    )


async def test_a_start_whose_answer_was_lost_is_found_and_not_sent_again(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    acme.fake.state.lose_start_answer = True

    await tick(setup, "gradings.dispatch")
    lost = await _row(setup, row.id)
    assert (lost.status, lost.wait_reason) == (GradingStatus.DISPATCHING, dispatch.NO_ANSWER)

    clock.advance(timedelta(seconds=5))
    await tick(setup, "gradings.dispatch")

    found = await _row(setup, row.id)
    assert found.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_run")) == 1
    assert found.run_id == next(iter(acme.fake.state.started))


async def test_a_ci_that_does_not_answer_leaves_the_grading_queued(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    acme.fake.unavailable = True

    await tick(setup, "gradings.dispatch")

    after = await _row(setup, row.id)
    assert (after.status, after.wait_reason) == (GradingStatus.QUEUED, dispatch.NO_ANSWER)


async def test_a_run_waiting_for_a_machine_past_its_deadline_is_looked_at_again(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    clock.advance(RUN_TIMEOUT)

    await tick(setup, "gradings.overdue")

    after = await _row(setup, row.id)
    assert (after.status, after.wait_reason) == (
        GradingStatus.DISPATCHED,
        dispatch.WAITING_FOR_MACHINE,
    )
    assert after.deadline_at == clock.now() + timedelta(minutes=5)


async def test_a_run_that_died_is_a_system_error(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    run = RunId(str((await _row(setup, row.id)).run_id))
    acme.fake.grading.finish(run, RunStatus.FAILED)
    clock.advance(RUN_TIMEOUT)

    await tick(setup, "gradings.overdue")

    dead = await _row(setup, row.id)
    assert (dead.status, dead.error) == (GradingStatus.SYSTEM_ERROR, dispatch.DIED)
    assert dead.finished_at == clock.now()


async def test_a_grading_whose_start_is_refused_twenty_times_is_a_system_error(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    acme.fake.state.refuse_starts = dispatch.REFUSALS

    for _ in range(dispatch.REFUSALS):
        await tick(setup, "gradings.dispatch")
        clock.advance(timedelta(minutes=5))

    after = await _row(setup, row.id)
    assert (after.status, after.error) == (GradingStatus.SYSTEM_ERROR, dispatch.GAVE_UP)
    assert len(acme.fake.calls_to("start_run")) == dispatch.REFUSALS
