"""A grading from its queued row to its verdict, over a real Postgres and the
fake. The dispatcher starts each run once, as the org account, pinned to the
platform pool, with the variables the extension checks, and puts a failed
start back in the queue with a reason and a wait; a start whose answer was
lost is found rather than sent again. The extension answers the three steps
only for a signed request naming a grading being started with those
variables. The envelope is served once, with its key, to a run the CI holds
that has not begun, and that fetch starts the run's clock. The callback takes reports only
under the grading's own token, keeps a verdict that matches the schema and
turns any other into a system error. A run that died goes back to the queue
once, and the second time is a system error. The organiser cancels, retries
and rejudges, and the reconcile pass gives a submission without gradings its
rows.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from forge.db.tables import Contestant, Grading
from forge.domain.contracts import violation
from forge.domain.errors import (
    CiRequestRefused,
    Conflict,
    Forbidden,
    GradingClosed,
    InvalidCallback,
    InvalidToken,
    LogTooLarge,
    NotFound,
    Unavailable,
    WrongStatus,
)
from forge.domain.grading import (
    REPORT_ALLOWANCE,
    RUN_LOG_MAX,
    RUN_TIMEOUT,
    GradingRun,
    GradingStatus,
    RunStatus,
    callback_token,
    envelope_key,
    wall_seconds,
)
from forge.domain.identity import PLATFORM, AsOrgAccount, User
from forge.domain.ids import RunId
from forge.domain.plans import Plan
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.submissions import SubmittedInput
from forge.port.objects import Store
from forge.runtime.setup import Setup
from forge.services import dispatch, gradings, reconcile, runs, submissions, uploads
from forge.services.access import Organiser
from forge.testing import FakeClock, tick
from tests.services.conftest import Acme, Entered, organiser, publish, upload

KEY = "key-0001-aaaa"
SOURCE = b"print(sum(map(int, input().split())))\n"
SUM = Scope("acme", "spring", "sum")


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


async def _variables(setup: Setup, row: Grading) -> dict[str, str]:
    async with setup.unit_of_work() as ctx:
        return dict(ctx.forge.grading.run_variables(await gradings.run_of(ctx, row)))


def _key(setup: Setup, row: Grading) -> str:
    return envelope_key(setup.settings.token_encryption_key_bytes, row.id, run=row.requeues)


def _token(setup: Setup, row: Grading) -> str:
    return callback_token(setup.settings.token_encryption_key_bytes, row.id, run=row.requeues)


async def _running(setup: Setup, acme: Acme, entered: Entered) -> tuple[Grading, dict[str, Any]]:
    """A grading dispatched and its envelope fetched, and the envelope."""
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    document = await runs.envelope(setup, row.id, _key(setup, row))
    return await _row(setup, row.id), document


def _held(acme: Acme, scope: Scope, role: Role) -> Organiser:
    """Someone checked as holding `role` at `scope` and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, role),),
        scope=scope,
        role=role,
        identity=acme.ada.identity,
    )


def _verdict(envelope: dict[str, Any], **changes: Any) -> dict[str, Any]:
    verdict = {
        "schema_version": 3,
        "grading_id": envelope["grading_id"],
        "submission": envelope["submission"],
        "stage": envelope["stage"],
        "attempt": envelope["attempt"],
        "task": envelope["task"],
        "publication": envelope["publication"],
        "outcome": "accepted",
        "metrics": {"points": 1},
        "tests": [
            {"id": "1", "outcome": "accepted", "time_ms": 12, "memory_kb": 900, "metrics": {}}
        ],
        "summary": "1 of 1 tests accepted.",
        "resources": {"wall_ms": 3000, "cpu_ms": None, "peak_memory_kb": None},
        "log": envelope["log_put"].split("?", 1)[0],
        "started_at": "2026-09-26T12:00:00Z",
        "finished_at": "2026-09-26T12:00:03Z",
    }
    verdict.update(changes)
    return verdict


async def _report(setup: Setup, row: Grading, document: dict[str, Any]) -> GradingStatus:
    return await runs.callback(
        setup, row.id, f"Bearer {_token(setup, row)}", json.dumps(document).encode()
    )


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


async def test_the_extension_answers_the_three_steps_for_a_grading_being_started(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    variables = await _variables(setup, row)
    request = acme.fake.grading.config_request(entered.task, variables, now=clock.now())

    answer = await runs.config(setup, request)
    again = await runs.config(setup, request)

    assert answer == again
    document = json.loads(answer.body)
    [publication] = await acme.fake.workspaces.list_publications(entered.task)
    assert document["labels"] == "pool:platform"
    assert document["clone"][0]["commit"] == publication.version
    assert document["clone"][1]["commit"] == row.submission_version
    assert {step["image"] for step in document["clone"]} == {setup.settings.clone_image}
    assert document["steps"] == [
        {
            "name": "grade",
            "image": setup.settings.harness_image,
            "volumes": ["unicon-filter:/run/unicon:ro"],
        }
    ]
    assert [step["volumes"] for step in document["clone"]] == [["unicon-lfs-acme:/lfs-cache"]] * 2
    assert (await _row(setup, row.id)).status == GradingStatus.QUEUED


@pytest.mark.parametrize(
    "change",
    ["another_key", "body_changed", "stale", "unknown_grading", "variables_disagree"],
)
async def test_the_extension_refuses_what_it_should_not_answer(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, change: str
) -> None:
    row = await _submit(setup, acme, entered)
    variables = await _variables(setup, row)
    grading = acme.fake.grading
    match change:
        case "another_key":
            request = grading.config_request(entered.task, variables, now=clock.now(), key=b"x")
        case "body_changed":
            request = grading.config_request(entered.task, variables, now=clock.now(), body=b"{}")
        case "stale":
            request = grading.config_request(
                entered.task, variables, now=clock.now() - timedelta(minutes=6)
            )
        case "unknown_grading":
            request = grading.config_request(
                entered.task,
                {**variables, "UNICON_GRADING_ID": str(uuid.uuid4())},
                now=clock.now(),
            )
        case _:
            request = grading.config_request(
                entered.task,
                {**variables, "UNICON_SUBMISSION_COMMIT": "0" * 40},
                now=clock.now(),
            )

    with pytest.raises(CiRequestRefused) as refused:
        await runs.config(setup, request)
    assert refused.value.detail == runs.REFUSED


async def test_the_extension_refuses_a_grading_whose_run_was_started_already(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    variables = await _variables(setup, row)
    await tick(setup, "gradings.dispatch")

    with pytest.raises(CiRequestRefused):
        await runs.config(
            setup, acme.fake.grading.config_request(entered.task, variables, now=clock.now())
        )


async def test_the_envelope_is_the_runs_served_once_and_its_fetch_starts_the_clock(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    clock.advance(timedelta(minutes=40))

    document = await runs.envelope(setup, row.id, _key(setup, row))

    assert violation(document, "envelope") is None
    after = await _row(setup, row.id)
    async with setup.unit_of_work() as ctx:
        [publication] = await ctx.forge.workspaces.list_publications(entered.task)
        found = await ctx.forge.content.read_file(
            PLATFORM, entered.task, "plans/default.json", at=publication.version
        )
    wall = wall_seconds(Plan.from_bytes(found.content))
    assert document["limits"] == {"wall_seconds": wall}
    assert after.status == GradingStatus.RUNNING
    assert after.started_at == clock.now()
    assert after.deadline_at == clock.now() + timedelta(seconds=wall) + REPORT_ALLOWANCE
    assert document["deadline"] == after.deadline_at.isoformat().replace("+00:00", "Z")
    assert document["callback"] == {
        "url": f"http://app.test/api/v1/gradings/{row.id}/callback",
        "token": _token(setup, row),
    }
    assert document["checkouts"] == {
        "task": "/woodpecker/task",
        "submission": "/woodpecker/submission",
    }
    assert document["submission"]["repo"] == "spring.sum.bob.sub"
    assert document["publication"] == {"tag": "published/1", "commit": publication.version}
    acme.fake.objects.put(document["log_put"], b"the log")
    assert acme.fake.objects.objects[Store.RESULTS][f"logs/{row.id}/1.log"] == b"the log"

    clock.advance(timedelta(seconds=30))
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))
    assert (await _row(setup, row.id)).deadline_at == after.deadline_at


async def test_the_envelope_needs_its_key_and_a_run_the_ci_holds(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))
    await tick(setup, "gradings.dispatch")

    with pytest.raises(NotFound):
        await runs.envelope(setup, row.id, "wrong")
    with pytest.raises(NotFound):
        await runs.envelope(setup, uuid.uuid4(), _key(setup, row))

    await runs.envelope(setup, row.id, _key(setup, row))
    clock.advance(RUN_TIMEOUT)
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))


async def test_a_run_that_lost_its_envelope_is_requeued_and_its_next_run_fetches_its_own(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    await runs.envelope(setup, row.id, _key(setup, row))
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))
    running = await _row(setup, row.id)
    assert running.deadline_at is not None
    acme.fake.grading.finish(RunId(str(running.run_id)), RunStatus.FAILED)
    clock.set(running.deadline_at)

    await tick(setup, "gradings.overdue")
    await tick(setup, "gradings.dispatch")

    again = await _row(setup, row.id)
    assert (again.status, again.requeues) == (GradingStatus.DISPATCHED, 1)
    with pytest.raises(NotFound):
        await runs.envelope(setup, row.id, _key(setup, row))
    document = await runs.envelope(setup, row.id, _key(setup, again))
    assert document["callback"]["token"] == _token(setup, again)
    assert (await _row(setup, row.id)).status == GradingStatus.RUNNING
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, again))


async def test_a_valid_verdict_lands_on_the_row_with_its_log_and_the_contestant_reads_it(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row, envelope = await _running(setup, acme, entered)
    acme.fake.objects.put(envelope["log_put"], b"compile ok\n")

    assert await _report(setup, row, {"event": "started"}) == GradingStatus.RUNNING
    assert (
        await _report(setup, row, {"event": "progress", "step": "run", "done": 3, "total": 10})
        == GradingStatus.RUNNING
    )
    assert (await _row(setup, row.id)).progress == {"step": "run", "done": 3, "total": 10}
    verdict = _verdict(envelope)
    assert await _report(setup, row, {"event": "finished", "verdict": verdict}) == (
        GradingStatus.DONE
    )
    assert await _report(setup, row, {"event": "finished", "verdict": verdict}) == (
        GradingStatus.DONE
    )

    after = await _row(setup, row.id)
    assert (after.status, after.verdict, after.log_key) == (
        GradingStatus.DONE,
        verdict,
        f"logs/{row.id}/1.log",
    )
    mine = await submissions.one(setup, entered.session, entered.task, 1)
    [result] = mine.gradings
    assert (result.status, result.outcome, result.metrics, result.log) == (
        GradingStatus.DONE,
        "accepted",
        {"points": 1},
        True,
    )
    assert await submissions.run_log(setup, entered.session, entered.task, 1) == b"compile ok\n"


async def test_a_run_log_over_the_ceiling_is_refused_and_a_failing_store_is_not_named(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, envelope = await _running(setup, acme, entered)
    await _report(setup, row, {"event": "finished", "verdict": _verdict(envelope)})
    session, task = entered.session, entered.task
    acme.fake.objects.put(envelope["log_put"], b"x" * RUN_LOG_MAX)
    assert len(await submissions.run_log(setup, session, task, 1)) == RUN_LOG_MAX

    acme.fake.objects.put(envelope["log_put"], b"x" * (RUN_LOG_MAX + 1))
    with pytest.raises(LogTooLarge) as refused:
        await submissions.run_log(setup, session, task, 1)
    assert refused.value.extra == {"limit": RUN_LOG_MAX}

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the store answered 503 (SlowDown)")

    monkeypatch.setattr(acme.fake.objects, "read", down)
    with pytest.raises(Unavailable) as failed:
        await submissions.run_log(setup, session, task, 1)
    assert failed.value.detail == uploads.STORE_UNAVAILABLE


async def test_a_malformed_verdict_leaves_the_grading_in_system_error(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row, envelope = await _running(setup, acme, entered)

    status = await _report(
        setup, row, {"event": "finished", "verdict": _verdict(envelope, outcome="great")}
    )

    after = await _row(setup, row.id)
    assert status == GradingStatus.SYSTEM_ERROR
    assert (after.status, after.verdict) == (GradingStatus.SYSTEM_ERROR, None)
    assert after.error is not None and "verdict.schema.json" in after.error
    with pytest.raises(NotFound):
        await submissions.run_log(setup, entered.session, entered.task, 1)


async def test_a_verdict_for_another_grading_is_a_system_error(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row, envelope = await _running(setup, acme, entered)

    await _report(
        setup,
        row,
        {"event": "finished", "verdict": _verdict(envelope, grading_id=str(uuid.uuid4()))},
    )

    assert (await _row(setup, row.id)).status == GradingStatus.SYSTEM_ERROR


async def test_a_system_error_verdict_is_kept_as_one(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row, envelope = await _running(setup, acme, entered)
    verdict = _verdict(
        envelope, outcome="system_error", metrics={}, tests=[], summary="the plan broke"
    )

    assert await _report(setup, row, {"event": "finished", "verdict": verdict}) == (
        GradingStatus.SYSTEM_ERROR
    )
    after = await _row(setup, row.id)
    assert (after.verdict, after.error) == (verdict, "the plan broke")


async def test_a_report_needs_this_gradings_token(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row, _ = await _running(setup, acme, entered)
    clock.advance(timedelta(seconds=31))
    other = await _submit(setup, acme, entered, key="key-0002-bbbb")
    started = json.dumps({"event": "started"}).encode()

    for authorization in (None, "Bearer wrong", "Basic x", f"Bearer {_token(setup, other)}"):
        with pytest.raises(InvalidToken):
            await runs.callback(setup, row.id, authorization, started)
    with pytest.raises(InvalidToken):
        await runs.callback(setup, uuid.uuid4(), f"Bearer {_token(setup, row)}", started)
    with pytest.raises(InvalidCallback):
        await runs.callback(setup, row.id, f"Bearer {_token(setup, row)}", b"not json")
    with pytest.raises(InvalidCallback):
        await _report(setup, row, {"event": "progress", "step": "run", "done": 4, "total": 3})


async def test_a_report_is_refused_once_the_grading_takes_none(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    with pytest.raises(GradingClosed):
        await _report(setup, row, {"event": "started"})
    await tick(setup, "gradings.dispatch")
    envelope = await runs.envelope(setup, row.id, _key(setup, row))
    running = await _row(setup, row.id)
    assert running.deadline_at is not None

    clock.set(running.deadline_at)
    with pytest.raises(GradingClosed):
        await _report(setup, row, {"event": "finished", "verdict": _verdict(envelope)})


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


async def test_a_run_that_died_is_requeued_once_and_the_second_death_is_a_system_error(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    first_run = RunId(str((await _row(setup, row.id)).run_id))
    acme.fake.grading.finish(first_run, RunStatus.FAILED)
    clock.advance(RUN_TIMEOUT)

    await tick(setup, "gradings.overdue")

    requeued = await _row(setup, row.id)
    assert (requeued.status, requeued.requeues, requeued.wait_reason) == (
        GradingStatus.QUEUED,
        1,
        dispatch.REQUEUED,
    )
    await tick(setup, "gradings.dispatch")
    again = await _row(setup, row.id)
    assert again.status == GradingStatus.DISPATCHED
    assert again.run_id != first_run
    assert len(acme.fake.calls_to("start_run")) == 2
    assert again.callback_token_hash != row.callback_token_hash
    with pytest.raises(NotFound):
        await runs.envelope(setup, row.id, _key(setup, row))

    await runs.envelope(setup, row.id, _key(setup, again))
    running = await _row(setup, row.id)
    with pytest.raises(InvalidToken):
        await _report(setup, row, {"event": "started"})
    await _report(setup, running, {"event": "started"})
    assert running.deadline_at is not None
    acme.fake.grading.finish(RunId(str(running.run_id)), RunStatus.RUNNING)
    clock.set(running.deadline_at)
    await tick(setup, "gradings.overdue")

    dead = await _row(setup, row.id)
    assert (dead.status, dead.error) == (GradingStatus.SYSTEM_ERROR, dispatch.DIED_TWICE)
    assert acme.fake.state.runs[RunId(str(running.run_id))].status is RunStatus.CANCELLED


async def test_an_organiser_cancels_a_grading_at_the_ci_too(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    await tick(setup, "gradings.dispatch")
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    cancelled = await gradings.cancel(setup, manager, row.id)

    assert cancelled.status == GradingStatus.CANCELLED
    assert acme.fake.state.runs[run].status is RunStatus.CANCELLED
    with pytest.raises(WrongStatus) as refused:
        await gradings.cancel(setup, manager, row.id)
    assert refused.value.extra == {"current": "cancelled"}
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))


async def test_a_grading_is_no_such_grading_to_an_organiser_who_does_not_observe_its_task(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    stranger = _held(acme, Scope("other"), Role.ADMIN)
    observer = _held(acme, SUM, Role.OBSERVER)

    assert await gradings.task_of(setup, row.id) == entered.task
    with pytest.raises(NotFound):
        await gradings.task_of(setup, uuid.uuid4())
    with pytest.raises(NotFound):
        await gradings.cancel(setup, stranger, row.id)
    with pytest.raises(NotFound):
        await gradings.cancel(setup, acme.ada, uuid.uuid4())
    with pytest.raises(Forbidden):
        await gradings.cancel(setup, observer, row.id)
    assert [found.id for found in await gradings.list(setup, observer, entered.task)] == [row.id]


async def test_a_retry_makes_a_new_attempt_and_keeps_the_old(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row, envelope = await _running(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    with pytest.raises(WrongStatus):
        await gradings.retry(setup, manager, row.id)
    await _report(setup, row, {"event": "finished", "verdict": _verdict(envelope)})

    retried = await gradings.retry(setup, manager, row.id)

    assert (retried.attempt, retried.status, retried.publication) == (
        2,
        GradingStatus.QUEUED,
        row.publication_id,
    )
    old, new = await _rows(setup)
    assert (old.id, old.status, old.verdict) == (row.id, GradingStatus.DONE, _verdict(envelope))
    assert new.id == retried.id and new.idempotency_key is None
    with pytest.raises(Conflict):
        await gradings.retry(setup, manager, row.id)
    [result] = (await submissions.one(setup, entered.session, entered.task, 1)).gradings
    assert (result.attempt, result.status) == (2, GradingStatus.QUEUED)


async def test_a_rejudge_grades_every_submission_again_against_the_current_publication(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    first, envelope = await _running(setup, acme, entered)
    await _report(setup, first, {"event": "finished", "verdict": _verdict(envelope)})
    clock.advance(timedelta(seconds=31))
    second = await _submit(setup, acme, entered, key="key-0002-bbbb")
    await tick(setup, "gradings.dispatch")
    republished = await publish(setup, acme, entered.task, b"\n")
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    rejudged = await gradings.rejudge(setup, manager, entered.task)

    assert (rejudged.publication, rejudged.queued, rejudged.cancelled) == (
        republished.publication,
        2,
        1,
    )
    rows = await _rows(setup)
    assert [(row.submission_number, row.attempt, row.status) for row in rows] == [
        (1, 1, GradingStatus.DONE),
        (1, 2, GradingStatus.QUEUED),
        (2, 1, GradingStatus.CANCELLED),
        (2, 2, GradingStatus.QUEUED),
    ]
    assert {row.publication_id for row in rows if row.attempt == 2} == {republished.publication}
    assert rows[0].verdict == _verdict(envelope)
    again = await gradings.rejudge(setup, manager, entered.task)
    assert (again.queued, again.left_running) == (0, 2)
    with pytest.raises(Forbidden):
        await gradings.rejudge(setup, _held(acme, SUM, Role.OBSERVER), entered.task)
    assert second.submission_number == 2


async def test_the_reconcile_pass_gives_a_submission_without_gradings_its_rows(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(delete(Grading))

    await tick(setup, "gradings.reconcile")

    [made] = await _rows(setup)
    assert (made.submission_id, made.idempotency_key, made.status, made.attempt) == (
        row.submission_id,
        KEY,
        GradingStatus.QUEUED,
        1,
    )
    await tick(setup, "gradings.reconcile")
    assert len(await _rows(setup)) == 1


async def test_a_full_reconcile_rebuilds_the_gradings_of_a_contest_that_is_over(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(delete(Grading))
    clock.advance(timedelta(days=3))

    async with setup.unit_of_work() as ctx:
        timed = await reconcile.reconcile(ctx, every_contest=False)
    assert (timed.contests, timed.inserted) == (0, 0)
    async with setup.unit_of_work() as ctx:
        full = await reconcile.reconcile(ctx, every_contest=True)

    assert (full.contests, full.submissions, full.inserted) == (1, 1, 1)
    assert len(await _rows(setup)) == 1


async def test_the_reconcile_pass_waits_for_the_contestant_with_the_longest_extension(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(delete(Grading))
    clock.advance(timedelta(days=3))
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Contestant).values(time_extension_seconds=int(timedelta(days=3).total_seconds()))
        )

    async with setup.unit_of_work() as ctx:
        timed = await reconcile.reconcile(ctx, every_contest=False)

    assert (timed.contests, timed.inserted) == (1, 1)


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
