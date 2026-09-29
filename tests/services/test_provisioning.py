"""Making something at the forge is a pending row the poller takes: each
kind's steps are the ones the step table names, in its order, and its
record carries them; a failure leaves the row naming the step it stopped
at, the reason and when it is tried again, the next tick starts
at the step after the last one that completed, a row that keeps failing
waits longer each time before it is taken again, and a row of a kind nothing
makes is recorded rather than lost and waits the same way. Two requests for
one thing at once leave one row and a conflict.
"""

import asyncio
from collections.abc import Sequence
from datetime import timedelta

import pytest
from sqlalchemy import text

from forge.db.tables import Provisioning
from forge.domain.errors import Conflict, Unavailable
from forge.domain.provisioning import STEPS
from forge.runtime.context import Context
from forge.runtime.setup import MAKERS, Setup
from forge.services import provisioning
from forge.services.provisioning import Attempt, Record, Step, retry_wait
from forge.testing import FakeClock


class Steps:
    """Three steps that record their runs, the second of which fails while
    `broken` is set.
    """

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.broken = True

    def all(self) -> list[Step]:
        return [Step("one", self._one), Step("two", self._two), Step("three", self._three)]

    async def _one(self, attempt: Attempt) -> None:
        self.ran.append("one")

    async def _two(self, attempt: Attempt) -> None:
        self.ran.append("two")
        if self.broken:
            raise Unavailable("the forge went away")

    async def _three(self, attempt: Attempt) -> None:
        self.ran.append(f"three@{attempt.number}")


TARGETS = {
    "org": "acme",
    "contest": "acme/spring",
    "task": "acme/spring/sum",
    "workspace": "0190f0e8-0000-7000-8000-000000000000",
    "submission_place": "0190f0e8-0000-7000-8000-000000000000/acme/spring/sum",
    "activation": "acme/spring/sum",
}


async def _record(setup: Setup, kind: str, target: str) -> Record:
    async with setup.unit_of_work() as ctx:
        record = await provisioning.record_of(ctx, kind, target)
    assert record is not None
    return record


async def test_a_failure_names_the_step_and_the_error_and_a_rerun_starts_after_the_last_done(
    ctx: Context,
) -> None:
    steps = Steps()
    row = await provisioning.request(ctx, "contest", "acme/spring", {"description": "Spring"})
    assert (row.status, row.attempts, row.payload) == ("pending", 0, {"description": "Spring"})

    failed = await provisioning.run(ctx, row, steps.all())

    assert failed.status == "failed"
    assert (failed.last_step, failed.failed_step) == ("one", "two")
    assert failed.error == "the forge or the CI did not answer"
    assert failed.retry_at == ctx.now
    assert failed.attempts == 1
    assert (failed.kind, failed.target_id) == ("contest", "acme/spring")

    steps.broken = False
    done = await provisioning.run(ctx, row, steps.all())

    assert steps.ran == ["one", "two", "two", "three@2"]
    assert done.status == "ready"
    assert done.last_step == "three"
    assert (done.failed_step, done.error, done.retry_at) == (None, None, None)
    assert done.attempts == 2
    assert done.ready_at == ctx.now


async def test_a_thing_already_made_is_not_made_again(ctx: Context) -> None:
    steps = Steps()
    steps.broken = False
    row = await provisioning.request(ctx, "task", "acme/spring/sum", {})
    await provisioning.run(ctx, row, steps.all())

    again = await provisioning.run(ctx, row, steps.all())

    assert steps.ran == ["one", "two", "three@1"]
    assert again.status == "ready"
    assert again.attempts == 1


async def test_a_second_request_for_the_same_thing_is_a_conflict(ctx: Context) -> None:
    steps = Steps()
    steps.broken = False
    row = await provisioning.request(ctx, "task", "acme/spring/sum", {})

    with pytest.raises(Conflict, match="is being made"):
        await provisioning.request(ctx, "task", "acme/spring/sum", {})
    await provisioning.run(ctx, row, steps.all())
    with pytest.raises(Conflict, match="already exists"):
        await provisioning.request(ctx, "task", "acme/spring/sum", {})


async def test_a_step_that_fails_inside_the_database_leaves_the_unit_of_work_usable(
    ctx: Context,
) -> None:
    async def bad(attempt: Attempt) -> None:
        await ctx.db.execute(text("select * from no_such_table"))

    row = await provisioning.request(ctx, "workspace", "acme/spring/@bob", {})
    failed = await provisioning.run(ctx, row, [Step("bad", bad)])

    assert failed.status == "failed"
    assert (failed.failed_step, failed.error) == ("bad", "the step failed unexpectedly")
    assert (await ctx.db.execute(text("select 1"))).scalar() == 1


async def test_the_poller_takes_pending_and_failed_rows_and_dispatches_on_kind(
    setup: Setup,
) -> None:
    steps = Steps()

    async def make(ctx: Context, row: Provisioning) -> None:
        await provisioning.run(ctx, row, steps.all())

    poller = provisioning.poller({"workspace": make})
    async with setup.unit_of_work() as ctx:
        await provisioning.request(ctx, "workspace", "acme/spring/@bob", {})

    assert await poller.tick(setup.unit_of_work) == 1
    failed = await _record(setup, "workspace", "acme/spring/@bob")
    assert (failed.status, failed.last_step, failed.attempts) == ("failed", "one", 1)

    steps.broken = False
    assert await poller.tick(setup.unit_of_work) == 1
    done = await _record(setup, "workspace", "acme/spring/@bob")
    assert (done.status, done.last_step, done.attempts) == ("ready", "three", 2)
    assert steps.ran == ["one", "two", "two", "three@2"]
    assert await poller.tick(setup.unit_of_work) == 0


async def test_a_row_of_a_kind_nothing_makes_is_recorded_failed(setup: Setup) -> None:
    poller = provisioning.poller({})
    async with setup.unit_of_work() as ctx:
        await provisioning.request(ctx, "task", "acme/spring/sum", {})

    await poller.tick(setup.unit_of_work)

    failed = await _record(setup, "task", "acme/spring/sum")
    assert failed.status == "failed"
    assert (failed.failed_step, failed.error) == (None, "the step failed unexpectedly")
    assert failed.retry_at is not None
    assert failed.attempts == 1


async def test_a_row_whose_work_fails_outside_its_steps_waits_like_any_other(
    setup: Setup, clock: FakeClock
) -> None:
    poller = provisioning.poller({})
    async with setup.unit_of_work() as ctx:
        await provisioning.request(ctx, "task", "acme/spring/sum", {})

    assert await poller.tick(setup.unit_of_work) == 1
    assert await poller.tick(setup.unit_of_work) == 1
    assert await poller.tick(setup.unit_of_work) == 0
    clock.advance(timedelta(seconds=2))
    assert await poller.tick(setup.unit_of_work) == 1
    assert (await _record(setup, "task", "acme/spring/sum")).attempts == 3


async def test_two_requests_for_one_thing_at_once_make_one_row_and_a_conflict(
    setup: Setup,
) -> None:
    async def second() -> None:
        async with setup.unit_of_work() as ctx:
            await provisioning.request(ctx, "task", "acme/spring/sum", {})

    async with setup.unit_of_work() as ctx:
        await provisioning.request(ctx, "task", "acme/spring/sum", {})
        racing = asyncio.create_task(second())
        await asyncio.sleep(0.3)

    with pytest.raises(Conflict, match="is being made"):
        await racing
    assert (await _record(setup, "task", "acme/spring/sum")).status == "pending"


def test_the_wait_before_a_retry_doubles_up_to_an_hour() -> None:
    waits = [retry_wait(attempts) for attempts in (1, 2, 3, 4, 13, 50)]

    assert waits == [
        timedelta(0),
        timedelta(seconds=2),
        timedelta(seconds=4),
        timedelta(seconds=8),
        timedelta(hours=1),
        timedelta(hours=1),
    ]


async def test_a_row_that_keeps_failing_waits_before_the_poller_takes_it_again(
    setup: Setup, clock: FakeClock
) -> None:
    steps = Steps()

    async def make(ctx: Context, row: Provisioning) -> None:
        await provisioning.run(ctx, row, steps.all())

    poller = provisioning.poller({"workspace": make})
    async with setup.unit_of_work() as ctx:
        await provisioning.request(ctx, "workspace", "acme/spring/@bob", {})

    assert await poller.tick(setup.unit_of_work) == 1
    assert await poller.tick(setup.unit_of_work) == 1
    assert (await _record(setup, "workspace", "acme/spring/@bob")).attempts == 2
    assert await poller.tick(setup.unit_of_work) == 0

    clock.advance(timedelta(seconds=1))
    assert await poller.tick(setup.unit_of_work) == 0
    clock.advance(timedelta(seconds=1))
    assert await poller.tick(setup.unit_of_work) == 1
    steps.broken = False
    clock.advance(timedelta(seconds=4))
    assert await poller.tick(setup.unit_of_work) == 1
    done = await _record(setup, "workspace", "acme/spring/@bob")
    assert (done.status, done.attempts) == ("ready", 4)


def test_every_kind_the_poller_makes_has_its_steps_in_the_table() -> None:
    assert set(MAKERS) == set(STEPS)


@pytest.mark.parametrize("kind", sorted(STEPS))
async def test_each_service_runs_the_steps_the_table_names_in_its_order(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ran: list[tuple[str, ...]] = []

    async def spy(ctx: Context, row: Provisioning, steps: Sequence[Step]) -> Record:
        ran.append(tuple(step.name for step in steps))
        return provisioning.record_from(row)

    monkeypatch.setattr(provisioning, "run", spy)
    await MAKERS[kind](ctx, Provisioning(kind=kind, target_id=TARGETS[kind], payload={}))

    assert ran == [STEPS[kind]]


async def test_a_record_carries_the_steps_of_its_kind(ctx: Context) -> None:
    row = await provisioning.request(ctx, "task", "acme/spring/sum", {})

    record = provisioning.record_from(row)

    assert record.steps == ("repo", "roles")
    assert (record.last_step, record.failed_step, record.retry_at) == (None, None, None)


def test_the_steps_of_a_kind_are_refused_when_the_work_does_not_match_them() -> None:
    async def work(attempt: Attempt) -> None:
        return None

    assert [step.name for step in provisioning.steps("task", {"roles": work, "repo": work})] == [
        "repo",
        "roles",
    ]
    with pytest.raises(ValueError, match="does not match"):
        provisioning.steps("task", {"repo": work})
    with pytest.raises(ValueError, match="does not match"):
        provisioning.steps("task", {"repo": work, "roles": work, "extra": work})
