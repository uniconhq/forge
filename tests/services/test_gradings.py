"""A grading from its queued row to its result, over a real Postgres and the
fake. The submit that makes a grading, one per submission, starts its
pipeline once it commits, once, as the org account, on the org's dispatch
place, made and activated first when the CI does not know it, pinned to the
platform pool; a start that fails ends the grading in a system error saying
why. A run no machine took reads as a system error, and so does one whose
run the CI has lost, found by asking the CI when the grading is read, a few
times a minute at most, while one still waiting in its queue is left alone;
its contestant is told it is delayed while it has no result, and is told
its last result while it has one. The organiser cancels, retries and
rejudges, a stuck grading included, whose old run is cancelled, and a
failed or cancelled attempt never takes a result away; and the operator's
reconcile gives a submission without gradings its rows.
"""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from forge.adapters.ci.fake import dispatch_of, token_in
from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.errors import (
    Conflict,
    Forbidden,
    InvalidReason,
    NotFound,
    SubmissionLimit,
    Unavailable,
    WrongStatus,
)
from forge.domain.grading import (
    CANCEL_REASON_MAX,
    LOST,
    LOST_CHECK_AFTER,
    MACHINE_WAIT,
    NEVER_BEGAN,
    NEVER_STARTED,
    OVERDUE,
    START_WAIT,
    GradingStatus,
    PipelineRun,
    RunState,
)
from forge.domain.identity import PLATFORM, AsOrgAccount, User
from forge.domain.ids import OrgId, RunId, TaskId
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.submission_state import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import gradings, org_accounts, reconcile, submissions, task_save
from forge.services.access import Organiser
from forge.testing import FakeClock
from tests.services.conftest import (
    Acme,
    Entered,
    checked,
    organiser,
    publish,
    upload,
    write_contest,
)

KEY = "key-0001-aaaa"
SOURCE = b"print(sum(map(int, input().split())))\n"
SUM = Scope("acme", "spring", "sum")
RESULT = {
    "schema_version": 6,
    "runs": [
        {
            "id": "r1",
            "key": {"input": "tests/1.in", "answer": "tests/1.ans"},
            "verdict": "accepted",
            "passed": 1,
            "reports": {},
            "texts": {},
        }
    ],
}
"""The result of sum's one run, accepted."""


async def _submit(setup: Setup, acme: Acme, entered: Entered, key: str = KEY) -> Grading:
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
    return (await _rows(setup))[-1]


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


async def _row(setup: Setup, grading: uuid.UUID) -> Grading:
    async with setup.unit_of_work() as ctx:
        found = await gradings.find(ctx, grading)
        assert found is not None
        return found


async def _set(setup: Setup, grading: uuid.UUID, **values: Any) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).where(Grading.id == grading).values(**values))


async def _graded(setup: Setup, grading: uuid.UUID) -> None:
    """The grading ended `done` with `RESULT`, as its report would leave it."""
    await _set(setup, grading, status=GradingStatus.DONE, result=RESULT, error=None)


Start = Callable[[Setup, Grading], Coroutine[Any, Any, None]]


@pytest.fixture
def unstarted(monkeypatch: pytest.MonkeyPatch) -> Start:
    """Gradings left `queued` when the submit commits, for a test of what the
    CI asks while a start is under way, and the start itself, run when the
    test says.
    """
    real = gradings.starting.start

    async def later(ctx: Any, grading: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(gradings.starting, "start", later)

    async def start(setup: Setup, row: Grading) -> None:
        async with setup.unit_of_work() as ctx:
            await real(ctx, row.id)

    return start


async def _listed(
    setup: Setup, organiser: Organiser, task: TaskId, *, limit: int = 100
) -> list[gradings.GradingRecord]:
    """The task's gradings as `gradings.list` gives them, without who
    submitted each.
    """
    return [
        entry.grading for entry in await gradings.list_gradings(setup, organiser, task, limit=limit)
    ]


def _held(acme: Acme, scope: Scope, role: Role) -> Organiser:
    """Someone checked as holding `role` at `scope` and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, role),),
        scope=scope,
        role=role,
        identity=acme.ada.identity,
    )


async def test_a_submit_starts_one_pipeline_as_the_org_account_on_the_platform_pool(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert after.run_id is not None
    assert (after.dispatched_at, after.deadline_at) == (clock.now(), None)
    [started] = acme.fake.calls_to("start_pipeline")
    assert isinstance(started.identity, AsOrgAccount)
    assert started.identity.org == "acme"
    [publication] = await acme.fake.workspaces.list_publications(entered.task)
    variables = started.arguments["variables"]
    assert variables["UNICON_GRADING_ID"] == str(row.id)
    assert variables["UNICON_PIPELINE"] == "early"
    assert variables["UNICON_COMPUTE"] == "pool:platform"
    assert variables["UNICON_PUBLICATION_COMMIT"] == publication.version
    assert variables["UNICON_SUBMISSION_COMMIT"] == row.submission_version


async def test_a_start_the_ci_does_not_know_the_dispatch_place_of_activates_it_and_starts(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    """The CI's database restored from before the org's first grading: the
    next start makes and activates the dispatch place again, once.
    """
    acme.fake.ci.activated.clear()

    row = await _submit(setup, acme, entered)

    assert (await _row(setup, row.id)).status == GradingStatus.DISPATCHED
    assert dispatch_of("acme") in acme.fake.ci.activated
    assert len(acme.fake.calls_to("activate_dispatch")) == 1
    assert len(acme.fake.calls_to("start_pipeline")) == 2


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
    start = acme.fake.grading.start_pipeline

    async def held_start(as_: AsOrgAccount, run: PipelineRun) -> RunId:
        starting.set()
        await release.wait()
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", held_start)
    first = asyncio.create_task(unstarted(setup, row))
    await asyncio.wait_for(starting.wait(), timeout=10)
    second = asyncio.create_task(unstarted(setup, row))
    release.set()
    await asyncio.gather(first, second)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_pipeline")) == 2
    assert [
        made
        for made, run in acme.fake.ci.runs.items()
        if run.variables["UNICON_PIPELINE"] == "early" and not run.cancelled
    ] == [RunId(str(after.run_id))]


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
    original = acme.fake.grading.start_pipeline

    async def down(*args: Any, **kwargs: Any) -> RunId:
        raise Unavailable("the CI went away")

    match trouble:
        case "refused":
            acme.fake.ci.refuse_starts = 1
        case "answer_lost":
            acme.fake.ci.lose_start_answer = True
        case _:
            monkeypatch.setattr(acme.fake.grading, "start_pipeline", down)
    row = await _submit(setup, acme, entered)
    monkeypatch.setattr(acme.fake.grading, "start_pipeline", original)

    assert (row.status, row.error, row.run_id) == (GradingStatus.SYSTEM_ERROR, reason, None)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    retried = await gradings.retry(setup, manager, row.id)
    assert (await _row(setup, retried.id)).status == GradingStatus.DISPATCHED


REASON = "The checker crashed on this one; it is not counted."


async def test_staff_cancel_only_a_grading_in_system_error_and_say_why(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    with pytest.raises(WrongStatus) as waiting:
        await gradings.cancel(setup, manager, row.id, REASON)
    assert waiting.value.extra == {"current": "dispatched"}
    for unsaid in ("", "   ", "x" * (CANCEL_REASON_MAX + 1)):
        with pytest.raises(InvalidReason):
            await gradings.cancel(setup, manager, row.id, unsaid)
    clock.advance(MACHINE_WAIT)

    cancelled = await gradings.cancel(setup, manager, row.id, f"  {REASON}\n")

    assert (cancelled.status, cancelled.error, cancelled.cancel_reason) == (
        GradingStatus.CANCELLED,
        NEVER_BEGAN,
        REASON,
    )
    assert acme.fake.ci.runs[run].cancelled is True
    with pytest.raises(WrongStatus) as refused:
        await gradings.cancel(setup, manager, row.id, REASON)
    assert refused.value.extra == {"current": "cancelled"}
    (listed,) = await _listed(setup, manager, entered.task)
    assert (listed.status, listed.cancel_reason) == (GradingStatus.CANCELLED, REASON)


async def test_a_cancelled_submission_reads_as_cancelled_and_frees_its_place_under_the_max(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _set(setup, row.id, status=GradingStatus.SYSTEM_ERROR, error="The checker crashed.")
    before = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert before is not None
    assert (before.status, before.reason) == (SubmissionState.DELAYED, None)

    await gradings.cancel(setup, manager, row.id, REASON)

    after = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert after is not None
    assert (after.status, after.reason) == (SubmissionState.CANCELLED, REASON)
    assert (await _row(setup, row.id)).error == "The checker crashed."
    # A task that takes one submission takes another once that one is cancelled.
    await _limit_to_one(setup, acme, entered)
    clock.advance(timedelta(seconds=31))
    second = await _submit(setup, acme, entered, key="key-0002-bbbb")
    assert second.submission_number == 2
    clock.advance(timedelta(seconds=31))
    with pytest.raises(SubmissionLimit):
        await _submit(setup, acme, entered, key="key-0003-cccc")
    # A rejudge leaves the cancelled one as it is.
    rejudged = await gradings.rejudge(setup, manager, entered.task)
    assert (rejudged.queued, rejudged.cancelled, rejudged.left_running) == (0, 0, 1)
    # So does a retry: the cancel ended the submission, and it stays out of the max.
    with pytest.raises(WrongStatus) as refused:
        await gradings.retry(setup, manager, row.id)
    assert "cancelled" in refused.value.detail
    assert refused.value.extra == {"current": "cancelled"}
    first = [found for found in await _rows(setup) if found.submission_number == 1]
    assert [(found.attempt, found.status) for found in first] == [(1, GradingStatus.CANCELLED)]


async def _limit_to_one(setup: Setup, acme: Acme, entered: Entered) -> None:
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    limited = current.content.replace(b"scoring:", b"submissions: {max: 1}\nscoring:")
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    await task_save.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(limited, head.tokens["task.yaml"])},
        confirm=True,
    )
    await checked(setup, entered.task)


async def test_only_the_latest_attempt_of_a_submission_is_cancelled(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _set(setup, row.id, status=GradingStatus.SYSTEM_ERROR, error="The checker crashed.")
    retried = await gradings.retry(setup, manager, row.id)
    await _set(setup, retried.id, status=GradingStatus.SYSTEM_ERROR, error="Again.")

    with pytest.raises(Conflict):
        await gradings.cancel(setup, manager, row.id, REASON)
    cancelled = await gradings.cancel(setup, manager, retried.id, REASON)

    assert (cancelled.attempt, cancelled.status) == (2, GradingStatus.CANCELLED)


async def test_only_the_latest_attempt_of_a_submission_is_retried(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _set(setup, row.id, status=GradingStatus.SYSTEM_ERROR, error="The checker crashed.")
    retried = await gradings.retry(setup, manager, row.id)
    await _set(setup, retried.id, status=GradingStatus.DONE, error=None)

    # The earlier attempt graded against what the later one replaced.
    with pytest.raises(Conflict) as refused:
        await gradings.retry(setup, manager, row.id)
    assert "later attempt" in refused.value.detail
    again = await gradings.retry(setup, manager, retried.id)

    assert (again.attempt, again.latest) == (3, True)
    assert [found.attempt for found in await _rows(setup)] == [1, 2, 3]
    listed = await _listed(setup, manager, entered.task, limit=2)
    assert [(found.attempt, found.latest) for found in listed] == [(3, True), (2, False)]


async def _broken_after_a_result(
    setup: Setup, acme: Acme, entered: Entered, manager: Organiser
) -> tuple[Grading, uuid.UUID]:
    """Submission 1 graded with a result, and its retry ended in a system
    error: the first attempt and the second's id.
    """
    row = await _submit(setup, acme, entered)
    await _graded(setup, row.id)
    retried = await gradings.retry(setup, manager, row.id)
    await _set(setup, retried.id, status=GradingStatus.SYSTEM_ERROR, error="The checker crashed.")
    return row, retried.id


async def test_a_failed_regrade_leaves_the_submission_at_its_last_result(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _broken_after_a_result(setup, acme, entered, manager)

    listed = {found.attempt: found for found in await _listed(setup, manager, entered.task)}
    told = (await submissions.one(setup, entered.session, entered.task, 1)).grading

    assert (listed[2].latest, listed[2].status, listed[2].last_good) == (
        True,
        GradingStatus.SYSTEM_ERROR,
        1,
    )
    assert (listed[1].latest, listed[1].last_good) == (False, None)
    assert told is not None and (told.attempt, told.status) == (1, SubmissionState.GRADED)
    assert [(run["group"], run["verdict"]) for run in told.runs] == [("all", "accepted")]
    assert [(value.name, value.number) for value in told.run_values] == [("score", Fraction(1))]
    seen = await submissions.organised(
        setup, manager, entered.task, EntrantOwner(entered.entrant), 1
    )
    assert seen.grading is not None and (seen.grading.attempt, seen.grading.status) == (
        1,
        GradingStatus.DONE,
    )


async def test_a_submission_with_no_result_whose_grading_failed_is_delayed_and_nothing_more(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    row = await _submit(setup, acme, entered)
    await _set(setup, row.id, status=GradingStatus.SYSTEM_ERROR, error="The checker crashed.")

    told = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    (listed,) = await _listed(setup, manager, entered.task)

    assert told is not None
    assert (told.attempt, told.status, told.reason, told.runs) == (
        1,
        SubmissionState.DELAYED,
        None,
        (),
    )
    assert (listed.status, listed.error, listed.last_good) == (
        GradingStatus.SYSTEM_ERROR,
        "The checker crashed.",
        None,
    )


async def test_a_cancel_after_a_result_keeps_the_result_and_its_place_under_the_max(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    _, broken = await _broken_after_a_result(setup, acme, entered, manager)

    cancelled = await gradings.cancel(setup, manager, broken, REASON)

    assert (cancelled.status, cancelled.last_good) == (GradingStatus.CANCELLED, 1)
    shown = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert shown is not None and (shown.attempt, shown.status) == (1, SubmissionState.GRADED)
    # A cancel stays final for grading: a rejudge passes the submission over.
    rejudged = await gradings.rejudge(setup, manager, entered.task)
    assert rejudged.queued == 0
    assert [found.attempt for found in await _rows(setup)] == [1, 2]
    # The submission still stands at its result, so it still takes its place under the max.
    await _limit_to_one(setup, acme, entered)
    clock.advance(timedelta(seconds=31))
    with pytest.raises(SubmissionLimit):
        await _submit(setup, acme, entered, key="key-0002-bbbb")


async def test_a_staff_retry_after_a_failed_one_keeps_the_last_result_meanwhile(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    _, broken = await _broken_after_a_result(setup, acme, entered, manager)

    retried = await gradings.retry(setup, manager, broken)
    assert (retried.attempt, retried.last_good, retried.automatic) == (3, 1, False)
    await _set(setup, retried.id, status=GradingStatus.SYSTEM_ERROR, error="Again.")

    listed = {found.attempt: found for found in await _listed(setup, manager, entered.task)}
    assert (listed[3].latest, listed[3].last_good) == (True, 1)
    assert (listed[2].latest, listed[2].last_good) == (False, None)
    told = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert told is not None and (told.attempt, told.status) == (1, SubmissionState.GRADED)


async def test_a_lost_regrade_leaves_the_last_result_and_a_retry_ends_it_saying_why(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    row = await _submit(setup, acme, entered)
    await _graded(setup, row.id)
    retried = await gradings.retry(setup, manager, row.id)
    run = RunId(str((await _row(setup, retried.id)).run_id))
    acme.fake.ci.runs[run].ci_state = RunState.LOST
    clock.advance(LOST_CHECK_AFTER)

    told = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert told is not None and (told.attempt, told.status) == (1, SubmissionState.GRADED)
    (latest,) = [found for found in await _listed(setup, manager, entered.task) if found.latest]
    assert (latest.status, latest.error, latest.last_good) == (GradingStatus.SYSTEM_ERROR, LOST, 1)

    await gradings.retry(setup, manager, retried.id)

    _, lost, _ = await _rows(setup)
    assert (lost.status, lost.error) == (GradingStatus.SYSTEM_ERROR, LOST)
    assert acme.fake.ci.runs[run].cancelled is True


async def test_a_broken_attempt_keeps_its_last_result_whatever_the_contests_settings(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    await _broken_after_a_result(setup, acme, entered, manager)
    await write_contest(acme.fake, "tasks: [\n")

    (listed,) = [found for found in await _listed(setup, manager, entered.task) if found.latest]

    assert (listed.status, listed.last_good) == (GradingStatus.SYSTEM_ERROR, 1)


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
        await gradings.cancel(setup, stranger, row.id, REASON)
    with pytest.raises(NotFound):
        await gradings.cancel(setup, acme.ada, uuid.uuid4(), REASON)
    with pytest.raises(Forbidden):
        await gradings.cancel(setup, observer, row.id, REASON)
    assert [found.id for found in await _listed(setup, observer, entered.task)] == [row.id]


async def test_a_retry_makes_a_new_attempt_and_keeps_the_old(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    with pytest.raises(WrongStatus):
        await gradings.retry(setup, manager, row.id)
    await _graded(setup, row.id)

    retried = await gradings.retry(setup, manager, row.id)

    assert (retried.attempt, retried.status, retried.publication) == (
        2,
        GradingStatus.QUEUED,
        row.publication_id,
    )
    assert (await _row(setup, retried.id)).status == GradingStatus.DISPATCHED
    old, new = await _rows(setup)
    assert (old.id, old.status, old.result) == (row.id, GradingStatus.DONE, RESULT)
    assert new.id == retried.id and new.idempotency_key is None
    with pytest.raises(Conflict):
        await gradings.retry(setup, manager, row.id)
    # While the new attempt is graded, the submission is read by the result it has.
    result = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert result is not None
    assert (result.attempt, result.status) == (1, SubmissionState.GRADED)


async def test_a_rejudge_grades_every_submission_again_against_the_current_publication(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    first = await _submit(setup, acme, entered)
    await _graded(setup, first.id)
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
    assert rows[0].result == RESULT
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
        await ctx.db.execute(delete(Grading).where(Grading.pipeline != "save"))
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

    (late,) = await _listed(setup, manager, entered.task)
    retried = await gradings.retry(setup, manager, row.id)
    await unstarted(setup, row)

    assert (late.status, late.error) == (GradingStatus.SYSTEM_ERROR, NEVER_STARTED)
    assert retried.attempt == 2
    assert acme.fake.calls_to("start_pipeline") == []


async def test_a_grading_whose_turn_comes_late_in_a_long_batch_is_started_all_the_same(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, unstarted: Start
) -> None:
    row = await _submit(setup, acme, entered)
    clock.advance(START_WAIT)

    await unstarted(setup, row)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_pipeline")) == 1


async def test_a_run_no_machine_took_reads_as_a_system_error(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    clock.advance(MACHINE_WAIT)

    (late,) = await _listed(setup, manager, entered.task)

    assert (late.status, late.error) == (GradingStatus.SYSTEM_ERROR, NEVER_BEGAN)
    assert (await _row(setup, row.id)).status == GradingStatus.DISPATCHED


async def test_a_start_the_ci_refuses_signs_the_org_account_in_again_and_starts(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    async with setup.unit_of_work() as ctx:
        lost = await org_accounts.identity(ctx, OrgId("acme"))
    acme.fake.ci.revoked_ci_tokens.add(token_in(lost.ci_state))

    row = await _submit(setup, acme, entered)

    after = await _row(setup, row.id)
    assert after.status == GradingStatus.DISPATCHED
    assert len(acme.fake.calls_to("start_pipeline")) == 2
    assert len(acme.fake.calls_to("refresh")) == 1


async def test_a_run_still_waiting_in_the_queue_is_left_alone_however_long(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    clock.advance(LOST_CHECK_AFTER - timedelta(seconds=1))
    await submissions.one(setup, entered.session, entered.task, 1)
    assert acme.fake.calls_to("run_state") == []
    clock.advance(timedelta(minutes=20))

    result = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert result is not None
    (listed,) = await _listed(setup, manager, entered.task)

    assert result.status == SubmissionState.GRADING
    assert (listed.status, listed.error) == (GradingStatus.DISPATCHED, None)
    assert (await _row(setup, row.id)).status == GradingStatus.DISPATCHED


async def test_a_run_the_ci_lost_reads_as_a_system_error_saying_so_and_nothing_is_written(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    acme.fake.ci.runs[run].ci_state = RunState.LOST
    clock.advance(LOST_CHECK_AFTER)

    result = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert result is not None
    (listed,) = await _listed(setup, manager, entered.task)

    # Its contestant is told it is delayed, and nothing more.
    assert result.status == SubmissionState.DELAYED
    assert (listed.status, listed.error) == (GradingStatus.SYSTEM_ERROR, LOST)
    after = await _row(setup, row.id)
    assert (after.status, after.error) == (GradingStatus.DISPATCHED, None)


async def test_the_ci_is_asked_about_a_run_at_most_once_while_its_answer_is_kept(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    clock.advance(LOST_CHECK_AFTER)

    for _ in range(5):
        await submissions.one(setup, entered.session, entered.task, 1)
        clock.advance(timedelta(seconds=2))
    assert len(acme.fake.calls_to("run_state")) == 1
    clock.advance(gradings.RUN_STATE_KEPT)
    await submissions.one(setup, entered.session, entered.task, 1)

    assert len(acme.fake.calls_to("run_state")) == 2


async def test_a_ci_that_does_not_say_where_a_run_is_loses_nothing(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _submit(setup, acme, entered)
    clock.advance(LOST_CHECK_AFTER)

    async def silent(run: RunId) -> RunState:
        raise Unavailable("The CI did not answer.")

    monkeypatch.setattr(acme.fake.grading, "run_state", silent)

    result = (await submissions.one(setup, entered.session, entered.task, 1)).grading
    assert result is not None
    assert result.status == SubmissionState.GRADING


async def test_a_retry_of_a_lost_grading_ends_it_saying_why_and_cancels_its_run(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    acme.fake.ci.runs[run].ci_state = RunState.LOST
    clock.advance(LOST_CHECK_AFTER)

    retried = await gradings.retry(setup, manager, row.id)

    old, new = await _rows(setup)
    assert (old.status, old.error) == (GradingStatus.SYSTEM_ERROR, LOST)
    assert acme.fake.ci.runs[run].cancelled is True
    assert (new.id, new.attempt, new.status) == (retried.id, 2, GradingStatus.DISPATCHED)
    assert new.run_id != run


async def test_a_retry_of_a_run_past_its_deadline_cancels_the_run_still_at_the_ci(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str(row.run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    deadline = clock.now() + timedelta(minutes=5)
    await _set(setup, row.id, status=GradingStatus.RUNNING, deadline_at=deadline)
    clock.set(deadline)

    await gradings.retry(setup, manager, row.id)

    old, _ = await _rows(setup)
    assert (old.status, old.error) == (GradingStatus.SYSTEM_ERROR, OVERDUE)
    assert acme.fake.ci.runs[run].cancelled is True


async def test_an_organiser_cancels_a_grading_that_reads_as_stuck(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    clock.advance(MACHINE_WAIT)

    cancelled = await gradings.cancel(setup, manager, row.id, REASON)

    assert cancelled.status == GradingStatus.CANCELLED
    assert acme.fake.ci.runs[run].cancelled is True


async def test_a_run_that_ended_before_its_steps_began_reads_as_lost_and_retries(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    row = await _submit(setup, acme, entered)
    run = RunId(str((await _row(setup, row.id)).run_id))
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    acme.fake.ci.runs[run].ci_state = RunState.FINISHED
    clock.advance(LOST_CHECK_AFTER)

    (listed,) = await _listed(setup, manager, entered.task)
    retried = await gradings.retry(setup, manager, row.id)

    assert (listed.status, listed.error) == (GradingStatus.SYSTEM_ERROR, LOST)
    old, new = await _rows(setup)
    assert (old.status, old.error) == (GradingStatus.SYSTEM_ERROR, LOST)
    assert (new.id, new.status) == (retried.id, GradingStatus.DISPATCHED)


async def test_a_cancel_the_ci_refuses_still_cancels_the_grading(
    setup: Setup,
    acme: Acme,
    entered: Entered,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _submit(setup, acme, entered)
    manager = await organiser(setup, acme.fake, 7, SUM, Role.MANAGER)
    clock.advance(MACHINE_WAIT)

    async def refused(*args: object, **kwargs: object) -> None:
        raise Unavailable("the CI is away")

    monkeypatch.setattr(acme.fake.grading, "cancel_run", refused)

    cancelled = await gradings.cancel(setup, manager, row.id, REASON)

    assert cancelled.status == GradingStatus.CANCELLED
    assert (await _row(setup, row.id)).status == GradingStatus.CANCELLED
