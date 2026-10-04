"""A grading from its queued row to its verdict, over a real Postgres and the
fake. The submit that makes a grading starts its run once it commits, once,
as the org account, pinned to the platform pool, with the variables the
extension checks; a start that fails ends the grading in a system error
saying why. The extension answers the three steps only for a signed request
naming a grading being started with those variables. The envelope is served
once, with its key, to a run the CI holds that has not begun, and that fetch
starts the run's clock. The callback takes reports only under the grading's
own token, keeps a verdict that matches the schema and turns any other into
a system error. A run that has not reported by its deadline reads as a
system error. The organiser cancels, retries and rejudges, and the
operator's reconcile gives a submission without gradings its rows.
"""

import asyncio
import json
import uuid
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from forge.db.tables import Grading
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
    MACHINE_WAIT,
    NEVER_BEGAN,
    NEVER_STARTED,
    OVERDUE,
    REPORT_ALLOWANCE,
    RUN_LOG_MAX,
    START_WAIT,
    GradingRun,
    GradingStatus,
    callback_token,
    envelope_key,
    wall_seconds,
)
from forge.domain.identity import PLATFORM, AsOrgAccount, User
from forge.domain.ids import OrgId, RunId
from forge.domain.plans import Plan
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import gradings, org_accounts, reconcile, runs, submissions
from forge.services.access import Organiser
from forge.testing import FakeClock
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
    return envelope_key(setup.settings.token_encryption_key_bytes, row.id)


def _token(setup: Setup, row: Grading) -> str:
    return callback_token(setup.settings.token_encryption_key_bytes, row.id)


async def _running(setup: Setup, acme: Acme, entered: Entered) -> tuple[Grading, dict[str, Any]]:
    """A grading dispatched and its envelope fetched, and the envelope."""
    row = await _submit(setup, acme, entered)
    document = await runs.envelope(setup, row.id, _key(setup, row))
    return await _row(setup, row.id), document


Start = Callable[[Setup, Grading], Coroutine[Any, Any, None]]


@pytest.fixture
def unstarted(monkeypatch: pytest.MonkeyPatch) -> Start:
    """Gradings left `queued` when the submit commits, for a test of what the
    CI asks while a start is under way, and the start itself, run when the
    test says.
    """
    real = gradings.start

    async def later(ctx: Any, grading: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(gradings, "start", later)

    async def start(setup: Setup, row: Grading) -> None:
        async with setup.unit_of_work() as ctx:
            await real(ctx, row.id)

    return start


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
        "schema_version": 4,
        "outcome": "accepted",
        "metrics": {"points": 1},
        "tests": [
            {"id": "1", "outcome": "accepted", "time_ms": 12, "memory_kb": 900, "metrics": {}}
        ],
        "summary": "1 of 1 tests accepted.",
        "log": envelope["log_put"].split("?", 1)[0],
    }
    verdict.update(changes)
    return verdict


async def _report(setup: Setup, row: Grading, document: dict[str, Any]) -> GradingStatus:
    return await runs.callback(
        setup, row.id, f"Bearer {_token(setup, row)}", json.dumps(document).encode()
    )


async def test_a_submit_starts_one_run_as_the_org_account_on_the_platform_pool(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert after.run_id is not None
    assert (after.dispatched_at, after.deadline_at) == (clock.now(), None)
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


async def test_two_starts_at_once_leave_one_run_and_cancel_the_other(
    setup: Setup,
    acme: Acme,
    entered: Entered,
    monkeypatch: pytest.MonkeyPatch,
    unstarted: Start,
) -> None:
    """The first start is held inside its call to the CI, which holds no
    lock, so a second starts a run too; whichever records its run second
    finds the grading dispatched and cancels its own.
    """
    row = await _submit(setup, acme, entered)
    starting, release = asyncio.Event(), asyncio.Event()
    start = acme.fake.grading.start_run

    async def held_start(as_: AsOrgAccount, run: GradingRun) -> RunId:
        starting.set()
        await release.wait()
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_run", held_start)
    first = asyncio.create_task(unstarted(setup, row))
    await asyncio.wait_for(starting.wait(), timeout=10)
    second = asyncio.create_task(unstarted(setup, row))
    release.set()
    await asyncio.gather(first, second)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_run")) == 2
    assert [made for made, run in acme.fake.state.runs.items() if not run.cancelled] == [
        RunId(str(after.run_id))
    ]


@pytest.mark.parametrize(
    ("trouble", "reason"),
    [
        ("refused", gradings.NO_RUN),
        ("answer_lost", gradings.NO_ANSWER),
        ("down", gradings.NO_ANSWER),
    ],
)
async def test_a_start_that_fails_is_a_system_error_saying_why_and_a_retry_starts_again(
    setup: Setup,
    acme: Acme,
    entered: Entered,
    monkeypatch: pytest.MonkeyPatch,
    trouble: str,
    reason: str,
) -> None:
    original = acme.fake.grading.start_run

    async def down(*args: Any, **kwargs: Any) -> RunId:
        raise Unavailable("the CI went away")

    match trouble:
        case "refused":
            acme.fake.state.refuse_starts = 1
        case "answer_lost":
            acme.fake.state.lose_start_answer = True
        case _:
            monkeypatch.setattr(acme.fake.grading, "start_run", down)
    row = await _submit(setup, acme, entered)
    monkeypatch.setattr(acme.fake.grading, "start_run", original)

    assert (row.status, row.error, row.run_id) == (GradingStatus.SYSTEM_ERROR, reason, None)
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    retried = await gradings.retry(setup, manager, row.id)
    assert (await _row(setup, retried.id)).status == GradingStatus.DISPATCHED


async def test_the_extension_answers_the_three_steps_for_a_grading_being_started(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
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
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start, change: str
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
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
) -> None:
    row = await _submit(setup, acme, entered)
    variables = await _variables(setup, row)
    await unstarted(setup, row)

    with pytest.raises(CiRequestRefused):
        await runs.config(
            setup, acme.fake.grading.config_request(entered.task, variables, now=clock.now())
        )


async def test_the_envelope_is_the_runs_served_once_and_its_fetch_starts_the_clock(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
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
    assert document["submission"]["repo"] == "spring.sum.u8.sub"
    assert (document["stage"], document["attempt"]) == ("default", 1)
    acme.fake.objects.put(document["log_put"], b"the log")
    assert acme.fake.objects.objects[f"logs/{row.id}/1.log"] == b"the log"

    clock.advance(timedelta(seconds=30))
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))
    assert (await _row(setup, row.id)).deadline_at == after.deadline_at


async def test_the_envelope_needs_its_key_and_a_run_the_ci_holds(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
) -> None:
    row = await _submit(setup, acme, entered)
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))
    await unstarted(setup, row)

    with pytest.raises(NotFound):
        await runs.envelope(setup, row.id, "wrong")
    with pytest.raises(NotFound):
        await runs.envelope(setup, uuid.uuid4(), _key(setup, row))

    await runs.envelope(setup, row.id, _key(setup, row))
    clock.advance(timedelta(hours=1))
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))


async def test_a_run_that_does_not_report_by_its_deadline_reads_as_a_system_error(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row, envelope = await _running(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    assert row.deadline_at is not None
    (before,) = await gradings.list(setup, manager, entered.task)
    assert before.status == GradingStatus.RUNNING

    clock.set(row.deadline_at)

    (after,) = await gradings.list(setup, manager, entered.task)
    assert (after.status, after.error) == (GradingStatus.SYSTEM_ERROR, OVERDUE)
    [result] = (await submissions.one(setup, entered.session, entered.task, 1)).gradings
    assert result.status == GradingStatus.SYSTEM_ERROR
    with pytest.raises(GradingClosed):
        await _report(setup, row, {"event": "finished", "verdict": _verdict(envelope)})
    with pytest.raises(WrongStatus):
        await gradings.cancel(setup, manager, row.id)
    retried = await gradings.retry(setup, manager, row.id)
    assert retried.attempt == 2


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
    assert failed.value.detail == submissions.LOG_STORE_UNAVAILABLE


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
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
) -> None:
    row = await _submit(setup, acme, entered)
    with pytest.raises(GradingClosed):
        await _report(setup, row, {"event": "started"})
    await unstarted(setup, row)
    envelope = await runs.envelope(setup, row.id, _key(setup, row))
    running = await _row(setup, row.id)
    assert running.deadline_at is not None

    clock.set(running.deadline_at)
    with pytest.raises(GradingClosed):
        await _report(setup, row, {"event": "finished", "verdict": _verdict(envelope)})


async def test_an_organiser_cancels_a_grading_at_the_ci_too(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)

    cancelled = await gradings.cancel(setup, manager, row.id)

    assert cancelled.status == GradingStatus.CANCELLED
    assert acme.fake.state.runs[run].cancelled is True
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
    assert (await _row(setup, retried.id)).status == GradingStatus.DISPATCHED
    old, new = await _rows(setup)
    assert (old.id, old.status, old.verdict) == (row.id, GradingStatus.DONE, _verdict(envelope))
    assert new.id == retried.id and new.idempotency_key is None
    with pytest.raises(Conflict):
        await gradings.retry(setup, manager, row.id)
    [result] = (await submissions.one(setup, entered.session, entered.task, 1)).gradings
    assert (result.attempt, result.status) == (2, GradingStatus.DISPATCHED)


async def test_a_rejudge_grades_every_submission_again_against_the_current_publication(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    first, envelope = await _running(setup, acme, entered)
    await _report(setup, first, {"event": "finished", "verdict": _verdict(envelope)})
    clock.advance(timedelta(seconds=31))
    second = await _submit(setup, acme, entered, key="key-0002-bbbb")
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
        (1, 2, GradingStatus.DISPATCHED),
        (2, 1, GradingStatus.CANCELLED),
        (2, 2, GradingStatus.DISPATCHED),
    ]
    assert {row.publication_id for row in rows if row.attempt == 2} == {republished.publication}
    assert rows[0].verdict == _verdict(envelope)
    again = await gradings.rejudge(setup, manager, entered.task)
    assert (again.queued, again.left_running) == (0, 2)
    with pytest.raises(Forbidden):
        await gradings.rejudge(setup, _held(acme, SUM, Role.OBSERVER), entered.task)
    assert second.submission_number == 2


async def test_the_reconcile_gives_a_submission_without_gradings_its_rows_and_starts_them(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(delete(Grading))
    clock.advance(timedelta(days=3))

    async with setup.unit_of_work() as ctx:
        done = await reconcile.reconcile(ctx)

    assert (done.contests, done.submissions, done.inserted) == (1, 1, 1)
    [made] = await _rows(setup)
    assert (made.submission_id, made.idempotency_key, made.status, made.attempt) == (
        row.submission_id,
        KEY,
        GradingStatus.DISPATCHED,
        1,
    )
    async with setup.unit_of_work() as ctx:
        again = await reconcile.reconcile(ctx)
    assert again.inserted == 0
    assert len(await _rows(setup)) == 1


async def test_a_grading_whose_run_was_never_started_reads_as_a_system_error(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    clock.advance(START_WAIT)

    (late,) = await gradings.list(setup, manager, entered.task)
    retried = await gradings.retry(setup, manager, row.id)
    await unstarted(setup, row)

    assert (late.status, late.error) == (GradingStatus.SYSTEM_ERROR, NEVER_STARTED)
    assert retried.attempt == 2
    assert acme.fake.calls_to("start_run") == []


async def test_a_grading_whose_turn_comes_late_in_a_long_batch_is_started_all_the_same(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
) -> None:
    row = await _submit(setup, acme, entered)
    clock.advance(START_WAIT)

    await unstarted(setup, row)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_run")) == 1


async def test_a_run_no_machine_took_reads_as_a_system_error_and_gets_no_envelope(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    clock.advance(MACHINE_WAIT)

    (late,) = await gradings.list(setup, manager, entered.task)

    assert (late.status, late.error) == (GradingStatus.SYSTEM_ERROR, NEVER_BEGAN)
    with pytest.raises(GradingClosed):
        await runs.envelope(setup, row.id, _key(setup, row))


async def test_a_start_the_ci_refuses_signs_the_org_account_in_again_and_starts(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    async with setup.unit_of_work() as ctx:
        lost = await org_accounts.identity(ctx, OrgId("acme"))
    acme.fake.state.revoked_ci_tokens.add(lost.ci_token)

    row = await _submit(setup, acme, entered)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_run")) == 2
    assert len(acme.fake.calls_to("mint_ci_token")) == 1
