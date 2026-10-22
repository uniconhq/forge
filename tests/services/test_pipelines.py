"""A run task's gradings as pipelines on the org's dispatch repository
(SIMPLER-CORE.md 11.3, 11.7, 11.8), against the fake CI, which asks the
platform what each pipeline is as it starts it, as Woodpecker does:
islands' early pipeline holds the compile and the sample runs, its late
one the rest, made only once the early one ends `done`; an early compile
error gives every late run its verdict with no late pipeline; an early
`system_error` is retried once and makes no late row, nor does a cancel;
a late one is retried once, lost or failed, found so by its entrant's
read, and told to no entrant until the reveal; a regrade stops a late
row of the grading it replaces, and a late row that failed is not tried
again once a regrade replaced its early row; a late row reads what its
early row read, and reuses as it did; an upload is linked only when the
submission took it; a task with nothing early makes its late row at
once, told as submitted until the reveal, with or without the platform's
images; a step whose image the platform withdrew ends its start saying
so and is refused at a save; each row has a user of its own, and takes
one a lost row held; a save is checked by a pipeline of its own before
it takes over, compared with the publication entrants are graded on, and
one whose checker does not compile is refused while the old version
keeps grading, shown so to its organisers and checked again by a save of
the same files; a save that fails, or is cut off by a restart, once its
publication is made leaves it held back until a save of the same files
checks it; a lost save row is found by its organisers' read; a take-over
whose work never ran is finished by the next read; and the platform
answers a pipeline only while its row starts, and only for the variables
it was started with.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from forge.adapters.ci.fake import dispatch_of, pipeline_variables
from forge.db.tables import Grading, GradingStep, StepOutput
from forge.db.tables import Upload as UploadRow
from forge.db.tables.entrants import Entrant
from forge.domain.content import Edit
from forge.domain.errors import CiRequestRefused, Misconfigured, Rejected, Unavailable
from forge.domain.grading import (
    BOXED_SHAPE,
    EARLY_LANE,
    LATE_LANE,
    LOST_CHECK_AFTER,
    MACHINE_WAIT,
    NEVER_BEGAN,
    PLATFORM_POOL,
    GradingStatus,
    PipelineRun,
    Slot,
)
from forge.domain.identity import PLATFORM, AsOrgAccount
from forge.domain.ids import OrgId, RunId
from forge.domain.keys import key_from_name
from forge.domain.live import Nudge
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role, Scope
from forge.domain.run_plan import PipelineKind
from forge.domain.showing import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import (
    gradings,
    pipelines,
    primitives,
    publications,
    published,
    runs,
    steps,
    submissions,
    tasks,
)
from forge.services.gradings import START_RETRY_WAIT
from forge.services.pipelines import USER_FIRST
from forge.services.publications import Draft, Published
from forge.settings import Settings
from forge.testing import APP_URL, CALLBACK_PATH, FORGE_URL, FakeClock, register_entrant
from tests.services.conftest import RUNNING, SPRING, Acme, organiser, signed_in, upload
from tests.services.test_run_task_saves import TASKS
from tests.services.test_steps import (
    CHECKER,
    CODE,
    PROGRAM,
    Islands,
    _batch,
    _bearer,
    _compiled,
    _download,
    _islands,
    _job,
    _mint,
    _report,
    _row,
    _save,
    _step,
)

LIVE = RUNNING.format(visibility="everyone") + "  - id: islands\n"
SMALL = "size:small"
"""The slot every pipeline of islands fits in, the smallest of the fake's."""
ISLANDS = Scope("acme", "spring", "islands")
NOTHING_EARLY = TASKS["islands"][0].replace(
    "now: [sample_score, sample_time, sample_tests, samples/]", "now: [samples/]"
)


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_URL,
        retry_failed=True,
    )


@pytest.fixture
def asked(
    setup: Setup, acme: Acme, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """What the platform answered for each pipeline the fake CI started,
    asked as the start is under way, once the CI holds the run."""
    answers: list[dict[str, Any]] = []
    start = acme.fake.grading.start_pipeline

    async def asking(as_: AsOrgAccount, run: PipelineRun) -> RunId:
        made = await start(as_, run)
        request = acme.fake.grading.config_request(
            None, pipeline_variables(run), now=clock.now(), org=run.org
        )
        answers.append(json.loads((await runs.config(setup, request)).body))
        return made

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", asking)
    return answers


async def _v2(acme: Acme, islands: Islands) -> str:
    """islands' task.yaml over `unicon/islands@v2`, made with its checker
    changed."""
    _, draft = await acme.fake.workflows.read_workflow_draft(
        PLATFORM, islands.workflow, "diff/diff.py"
    )
    await acme.fake.workflows.write_workflow_file(
        PLATFORM,
        islands.workflow,
        "diff/diff.py",
        draft.content + b"# changed\n",
        expected=draft.token,
        message="Changed",
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, islands.workflow, "v2")
    return TASKS["islands"][0].replace("unicon/islands@v1", "unicon/islands@v2")


async def _checked(setup: Setup, acme: Acme, text: str = TASKS["islands"][0]) -> Islands:
    """islands published in a running contest, its save pipeline run and
    reported, so its publication has taken over."""
    islands = await _islands(setup, acme, text=text, contest=LIVE)
    [save] = await _rows(setup, "save")
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    return islands


async def _submit(setup: Setup, acme: Acme, islands: Islands, user: int = 8) -> Any:
    """A submission of islands by a new entrant, signed in as `user`."""
    await register_entrant(setup, SPRING, user)
    session = await signed_in(setup, acme.fake, user)
    made = await upload(setup, acme.fake, session, islands.task, CODE, input="code")
    await submissions.submit(
        setup,
        session,
        islands.task,
        {"code": SubmittedInput(uploads=(made.id,)), "language": SubmittedInput(value="python")},
        idempotency_key=uuid.uuid4().hex,
    )
    await setup.settle()
    return session


async def _rows(setup: Setup, pipeline: PipelineKind) -> list[Grading]:
    """The rows of a pipeline as they were queued, those queued at one
    instant of the test's clock by publication, submission and attempt.
    """
    async with setup.unit_of_work() as ctx:
        return list(
            await ctx.db.scalars(
                select(Grading)
                .where(Grading.pipeline == pipeline)
                .order_by(
                    Grading.queued_at,
                    Grading.publication_id,
                    Grading.submission_id,
                    Grading.attempt,
                )
            )
        )


async def _early(setup: Setup, acme: Acme, row: uuid.UUID) -> GradingStatus:
    await _step(setup, acme, row, "early", "compile", _compiled())
    await _step(setup, acme, row, "early", "solution", _batch())
    await _step(setup, acme, row, "early", "compare", _batch(score=True))
    status = await _report(setup, row, "early")
    await setup.settle()
    return status


async def _fault(setup: Setup, acme: Acme, row: uuid.UUID, pipeline: PipelineKind) -> None:
    """The row's first step writing an `outputs.json` that does not read,
    and its report."""
    step = "compile" if pipeline == "early" else "solution"
    await _step(setup, acme, row, pipeline, step, lambda job: {"outputs.json": b"{"})
    assert await _report(setup, row, pipeline) is GradingStatus.SYSTEM_ERROR
    await setup.settle()


def _verdicts(row: Grading) -> list[tuple[str, str]]:
    assert row.result is not None
    return [(run["id"], run["verdict"]) for run in row.result["runs"]]


def _steps(answer: dict[str, Any]) -> list[tuple[str, int | None, list[str]]]:
    return [(step["name"], step["user"], step["depends_on"]) for step in answer["steps"]]


async def _seen(setup: Setup, session: Any, islands: Islands) -> list[Any]:
    """Each submission's grading as its entrant sees it: attempt and state."""
    found = await submissions.mine(setup, session, islands.task)
    return [(each.grading.attempt, each.grading.status) if each.grading else None for each in found]


async def test_islands_runs_its_early_pipeline_and_then_its_late_one_on_the_dispatch_place(
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]]
) -> None:
    islands = await _checked(setup, acme)
    session = await _submit(setup, acme, islands)

    [early] = await _rows(setup, "early")
    assert early.status == GradingStatus.DISPATCHED and early.run_id is not None
    assert acme.fake.ci.runs[RunId(early.run_id)].task == dispatch_of("acme")
    assert await _rows(setup, "late") == []
    save, first = asked
    assert (save["labels"], save["clone"]) == ([PLATFORM_POOL, SMALL, LATE_LANE], None)
    assert first["labels"] == [PLATFORM_POOL, BOXED_SHAPE, SMALL, EARLY_LANE]
    assert first["clone"]["image"] == setup.settings.plugin_git_image
    assert first["clone"]["submission"] == early.submission_id
    user = early.run_user
    assert user is not None and user >= USER_FIRST
    assert _steps(first) == [
        ("compile", user, []),
        ("solution", None, ["compile"]),
        ("compare", None, ["solution"]),
        ("report", user, ["compile", "solution", "compare"]),
    ]
    report = first["steps"][-1]
    assert (report["image"], report["always"]) == (setup.settings.primitive_base_image, True)
    assert report["settings"]["mode"] == "report"
    assert all(step["settings"]["data_token"] for step in first["steps"])

    assert await _early(setup, acme, early.id) is GradingStatus.DONE

    assert _verdicts(await _row(setup, early.id)) == [("r1", "accepted"), ("r2", "accepted")]
    [late] = await _rows(setup, "late")
    assert (late.early_id, late.attempt, late.status) == (early.id, 1, GradingStatus.DISPATCHED)
    assert late.idempotency_key is None
    assert [step[0] for step in _steps(asked[-1])] == ["solution", "compare", "report"]
    assert asked[-1]["labels"] == [PLATFORM_POOL, BOXED_SHAPE, SMALL, LATE_LANE]
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch(score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE
    assert _verdicts(await _row(setup, late.id)) == [("r3", "accepted"), ("r4", "accepted")]
    assert await _seen(setup, session, islands) == [(1, SubmissionState.GRADED)]


async def _grading(setup: Setup, session: Any, islands: Any) -> submissions.Result:
    [found] = await submissions.mine(setup, session, islands.task)
    assert found.grading is not None
    return found.grading


def _runs_seen(grading: submissions.Result) -> list[tuple[str, int, str]]:
    return [(run["group"], run["place"], run["verdict"]) for run in grading.runs]


BEFORE_CLOSE = datetime(2026, 9, 26, 14, 50, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 9, 26, 15, 5, tzinfo=UTC)


async def _graded_both(setup: Setup, acme: Acme, text: str = TASKS["islands"][0]) -> Any:
    """islands as `text` has it, one submission graded early and late
    before the close; the entrant's session."""
    islands = await _checked(setup, acme, text)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    [late] = await _rows(setup, "late")
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch(score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE
    return islands, session


async def test_the_entrant_reads_the_runs_of_the_groups_shown_now_and_after_the_reveal(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    [late] = await _rows(setup, "late")

    grading = await _grading(setup, session, islands)
    assert grading.status == SubmissionState.GRADED
    assert _runs_seen(grading) == [("sample_tests", 1, "accepted"), ("sample_tests", 2, "accepted")]
    assert set(grading.runs[0]) == {"group", "place", "key", "verdict", "passed", "reports"}
    assert grading.runs[0]["key"] == {"input": "samples/1.in", "answer": "samples/1.ans"}

    clock.set(AFTER_CLOSE)
    grading = await _grading(setup, session, islands)
    assert grading.status == SubmissionState.GRADING
    assert _runs_seen(grading) == [
        ("sample_tests", 1, "accepted"),
        ("sample_tests", 2, "accepted"),
        ("all", 1, "accepted"),
        ("all", 2, "accepted"),
    ]

    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch(score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE
    grading = await _grading(setup, session, islands)
    assert grading.status == SubmissionState.GRADED
    assert _runs_seen(grading) == [
        ("sample_tests", 1, "accepted"),
        ("sample_tests", 2, "accepted"),
        ("all", 1, "accepted"),
        ("all", 2, "accepted"),
        ("all", 3, "accepted"),
        ("all", 4, "accepted"),
    ]
    # No entry of show lists tests/, so a run's key never names its files.
    assert [run["key"] for run in grading.runs[4:]] == [{}, {}]


async def test_a_late_row_that_ends_is_told_to_its_entrant_only_after_the_reveal(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    # The same code again, its early steps reused, and its late ones too
    # once the first's have run.
    await _submit(setup, acme, islands, user=9)
    first, second = await _rows(setup, "late")
    told: list[Nudge] = []
    nudge = Context.nudge

    def heard(ctx: Context, made: Nudge) -> None:
        told.append(made)
        nudge(ctx, made)

    monkeypatch.setattr(Context, "nudge", heard)

    async def reported(late: Grading) -> list[tuple[str, bool]]:
        told.clear()
        assert await _report(setup, late.id, "late") is GradingStatus.DONE
        return [(each.id, each.entrant is not None) for each in told]

    await _step(setup, acme, first.id, "late", "solution", _batch())
    await _step(setup, acme, first.id, "late", "compare", _batch(score=True))
    assert await reported(first) == [(str(first.id), False)]
    clock.set(AFTER_CLOSE)
    assert await reported(second) == [(str(second.id), False), (str(second.early_id), True)]


VALUES_ONLY = (
    TASKS["islands"][0]
    .replace(
        "now: [sample_score, sample_time, sample_tests, samples/]", "now: [sample_score, samples/]"
    )
    .replace(
        "after_close: [st1, st2, total, all]",
        "after_close: [total]\n    on_release: [st1_tests, st2_tests]",
    )
)
"""islands showing values alone, and groups only on a release."""


async def test_a_value_shown_shows_no_run_and_a_group_on_release_none_before_a_release(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme, VALUES_ONLY)
    assert (await _grading(setup, session, islands)).runs == ()
    clock.set(AFTER_CLOSE)
    assert (await _grading(setup, session, islands)).runs == ()


async def test_a_group_moved_to_now_shows_its_runs_where_they_ran_late(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme)
    text = TASKS["islands"][0].replace(
        "now: [sample_score, sample_time, sample_tests, samples/]",
        "now: [sample_score, sample_time, sample_tests, st2_tests, samples/, tests/4.in]",
    )
    await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
    await setup.settle()

    grading = await _grading(setup, session, islands)
    assert _runs_seen(grading) == [
        ("sample_tests", 1, "accepted"),
        ("sample_tests", 2, "accepted"),
        ("st2_tests", 1, "accepted"),
        ("st2_tests", 2, "accepted"),
    ]
    # tests/4.in is shown now and tests/3.in is not, so only one key names its file.
    assert [run["key"] for run in grading.runs[2:]] == [{}, {"input": "tests/4.in"}]


async def test_organisers_read_every_run_of_both_rows_with_its_id(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, VALUES_ONLY)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    graded = await submissions.organised(setup, manager, islands.task, EntrantOwner(entrant), 1)
    assert graded.grading is not None
    assert [(run["id"], run["place"]) for run in graded.grading.runs] == [
        ("r1", 1),
        ("r2", 2),
        ("r3", 3),
        ("r4", 4),
    ]


async def test_an_early_compile_error_gives_every_late_run_its_verdict_and_no_late_pipeline(
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]]
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")

    await _step(setup, acme, early.id, "early", "compile", _compiled(outcome="compile_error"))
    access = await _mint(setup, early.id, "early", "solution")
    assert await steps.step_job(setup, early.id, "solution", _bearer(access)) is None
    assert await _report(setup, early.id, "early") is GradingStatus.DONE
    await setup.settle()

    [late] = await _rows(setup, "late")
    assert (late.status, late.run_id, late.automatic) == (GradingStatus.DONE, None, False)
    assert _verdicts(late) == [("r3", "compile_error"), ("r4", "compile_error")]
    assert len(asked) == 2


async def test_an_early_system_error_is_retried_once_and_makes_no_late_row(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")

    await _fault(setup, acme, first.id, "early")
    [_, retried] = await _rows(setup, "early")
    assert (retried.attempt, retried.automatic, retried.status) == (
        2,
        True,
        GradingStatus.DISPATCHED,
    )
    await _fault(setup, acme, retried.id, "early")

    assert len(await _rows(setup, "early")) == 2
    assert await _rows(setup, "late") == []
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await gradings.cancel(setup, manager, retried.id, "The judge was down.")
    assert await _rows(setup, "late") == []


async def test_a_late_row_is_retried_once_lost_or_cancelled_by_itself_and_told_to_no_entrant(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands = await _checked(setup, acme)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    seen = await _seen(setup, session, islands)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)

    [late] = await _rows(setup, "late")
    await _fault(setup, acme, late.id, "late")
    [_, retried] = await _rows(setup, "late")
    assert (retried.attempt, retried.automatic, retried.early_id) == (2, True, early.id)
    del acme.fake.ci.runs[RunId(retried.run_id or "")]
    clock.advance(LOST_CHECK_AFTER + timedelta(seconds=1))
    await _seen(setup, session, islands)
    await setup.settle()
    assert len(await _rows(setup, "late")) == 2
    await gradings.cancel(setup, manager, retried.id, "The machine went away.")

    assert (await _row(setup, retried.id)).status == GradingStatus.CANCELLED
    assert (await _row(setup, early.id)).status == GradingStatus.DONE
    assert await _seen(setup, session, islands) == seen == [(1, SubmissionState.GRADED)]


async def test_a_regrade_stops_the_late_row_of_the_grading_it_replaces(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    [late] = await _rows(setup, "late")
    assert late.status == GradingStatus.DISPATCHED
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)

    rejudged = await gradings.rejudge(setup, manager, islands.task)
    await setup.settle()

    assert rejudged.queued == 1
    assert (await _row(setup, late.id)).status == GradingStatus.CANCELLED
    assert [row.attempt for row in await _rows(setup, "early")] == [1, 2]


async def test_a_late_row_whose_early_row_was_replaced_is_not_tried_again(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    start = acme.fake.grading.start_pipeline

    async def refusing_late(as_: AsOrgAccount, run: PipelineRun) -> RunId:
        if run.pipeline == "late":
            raise Rejected("The CI refused the pipeline.")
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", refusing_late)
    await _early(setup, acme, early.id)
    [late] = await _rows(setup, "late")
    assert (late.status, late.run_id) == (GradingStatus.SYSTEM_ERROR, None)
    # While the late row waits to be tried again, a save's regrade replaces
    # the grading it follows.
    await _save(setup, acme, islands.task, {"task.yaml": (await _v2(acme, islands)).encode()})
    _, save = await _rows(setup, "save")
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    _, regraded = await _rows(setup, "early")
    assert regraded.status == GradingStatus.DISPATCHED
    clock.advance(START_RETRY_WAIT + timedelta(seconds=1))

    await _seen(setup, session, islands)
    await setup.settle()

    assert [row.id for row in await _rows(setup, "late")] == [late.id]


async def test_a_late_row_reads_what_its_early_row_read_and_reuses_as_it_did(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(GradingStep).where(GradingStep.grading_id == early.id).values(from_scratch=True)
        )
    await _step(setup, acme, early.id, "early", "compile", _compiled(), from_scratch=True)
    await _step(setup, acme, early.id, "early", "solution", _batch(), from_scratch=True)
    await _step(setup, acme, early.id, "early", "compare", _batch(score=True), from_scratch=True)
    # Another grading from scratch has since made another compile the
    # reused one of its key.
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(StepOutput).where(StepOutput.grading_id == early.id).values(reusable=False)
        )

    report = await _mint(setup, early.id, "early", "report", from_scratch=True)
    ended = await steps.step_report(setup, early.id, "report", _bearer(report), b"{}")
    assert ended is GradingStatus.DONE
    await setup.settle()

    [late] = await _rows(setup, "late")
    assert late.status == GradingStatus.DISPATCHED
    async with setup.unit_of_work() as ctx:
        minted = list(
            await ctx.db.scalars(
                select(GradingStep.from_scratch).where(GradingStep.grading_id == late.id)
            )
        )
    assert minted and all(minted)
    access = await _mint(setup, late.id, "late", "solution", from_scratch=True)
    job = await _job(setup, late.id, "solution", access)
    program = _download(job, job["inputs"]["batch"][0]["inputs"]["program"]["file"])
    assert acme.fake.objects.get(program["url"]) == PROGRAM


async def test_a_pointer_to_an_upload_the_submission_did_not_take_is_given_no_link(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    access = await _mint(setup, early.id, "early", "compile")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(UploadRow).values(consumed_by="acme/spring/@u9/islands#1"))

    with pytest.raises(Misconfigured, match="is no upload it took"):
        await steps.step_job(setup, early.id, "compile", _bearer(access))


async def test_a_task_with_nothing_early_makes_its_late_row_at_once(
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]], clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, NOTHING_EARLY)
    session = await _submit(setup, acme, islands)

    [early] = await _rows(setup, "early")
    assert (early.status, early.run_id) == (GradingStatus.DONE, None)
    assert early.result is not None and early.result["runs"] == []
    [late] = await _rows(setup, "late")
    assert (late.early_id, late.status) == (early.id, GradingStatus.DISPATCHED)
    assert [step[0] for step in _steps(asked[-1])] == ["compile", "solution", "compare", "report"]
    # Submitted, results after the contest: not graded, with nothing run.
    assert await _seen(setup, session, islands) == [(1, SubmissionState.SUBMITTED)]
    clock.set(AFTER_CLOSE)
    assert await _seen(setup, session, islands) == [(1, SubmissionState.GRADING)]


async def test_a_row_with_nothing_to_run_ends_without_the_platforms_images(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme, NOTHING_EARLY)
    monkeypatch.setattr(
        setup, "_settings", setup.settings.model_copy(update={"primitive_base_image": None})
    )

    await _submit(setup, acme, islands)

    [early] = await _rows(setup, "early")
    assert (early.status, early.run_id) == (GradingStatus.DONE, None)
    [late] = await _rows(setup, "late")
    assert (late.status, late.error) == (
        GradingStatus.SYSTEM_ERROR,
        "The platform names no image for a run task's report step.",
    )


async def test_a_withdrawn_image_ends_a_start_saying_so_and_refuses_a_save(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withdrawn by its digest, though under another registry's name and
    with a tag beside it: every step whose image has that digest is."""
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")
    async with setup.unit_of_work() as ctx:
        plan, _ = await steps.plan_of(ctx, first)
    image = plan.steps["compile"].image
    renamed = "mirror.example/compile:v1@" + image.partition("@")[2]
    monkeypatch.setattr(
        setup,
        "_settings",
        setup.settings.model_copy(update={"withdrawn_images": frozenset({renamed})}),
    )

    await _submit(setup, acme, islands, user=9)

    [other] = [row for row in await _rows(setup, "early") if row.id != first.id]
    assert (other.status, other.run_id) == (GradingStatus.SYSTEM_ERROR, None)
    assert other.error == pipelines.WITHDRAWN.format(image=image)
    head = await acme.fake.content.list_files(PLATFORM, islands.task)
    saved = await publications.save(
        setup,
        acme.ada,
        islands.task,
        {"statement.md": Edit(b"# Islands, again\n", head.tokens.get("statement.md"))},
        confirm=True,
    )
    assert isinstance(saved, Draft)
    messages = [problem["message"] for problem in saved.errors]
    assert messages[0] == (
        "In the workflow unicon/islands@v1, the step compile cannot run: "
        + primitives.WITHDRAWN.format(use=plan.steps["compile"].primitive)
    )
    same = {step.primitive for step in plan.steps.values() if step.image.endswith(image[-64:])}
    assert {
        message.partition("The primitive ")[2].partition(" ")[0] for message in messages
    } == same


async def test_each_row_running_has_a_user_of_its_own(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [held] = await _rows(setup, "early")
    assert held.run_user is not None
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            text("SELECT setval('grading_users', :number, false)"),
            {"number": held.run_user - USER_FIRST},
        )

    await _submit(setup, acme, islands, user=9)

    [other] = [row for row in await _rows(setup, "early") if row.id != held.id]
    assert other.run_user == held.run_user + 1


async def test_a_user_held_by_a_lost_automatic_retry_is_freed_for_the_next_start(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")
    await _fault(setup, acme, first.id, "early")
    [_, retried] = await _rows(setup, "early")
    assert retried.automatic and retried.run_user is not None
    # Its run lost and nobody reading it again, the retry keeps its row
    # dispatched; the next start's counter lands on its user, with no other
    # try.
    del acme.fake.ci.runs[RunId(retried.run_id or "")]
    clock.advance(MACHINE_WAIT + timedelta(seconds=1))
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            text("SELECT setval('grading_users', :number, false)"),
            {"number": retried.run_user - USER_FIRST},
        )
    monkeypatch.setattr(pipelines, "USER_TRIES", 1)

    await _submit(setup, acme, islands, user=9)

    [other] = [
        row for row in await _rows(setup, "early") if row.submission_id != first.submission_id
    ]
    assert (other.status, other.run_user) == (GradingStatus.DISPATCHED, retried.run_user)
    freed = await _row(setup, retried.id)
    assert (freed.status, freed.error) == (GradingStatus.SYSTEM_ERROR, NEVER_BEGAN)


async def test_a_save_whose_checker_does_not_compile_is_refused_and_the_old_version_grades(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    _, draft = await acme.fake.workflows.read_workflow_draft(
        PLATFORM, islands.workflow, "diff/diff.py"
    )
    await acme.fake.workflows.write_workflow_file(
        PLATFORM,
        islands.workflow,
        "diff/diff.py",
        draft.content + b"(\n",
        expected=draft.token,
        message="Broken",
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, islands.workflow, "v2")
    text = TASKS["islands"][0].replace("unicon/islands@v1", "unicon/islands@v2")
    saved = await _save(setup, acme, islands.task, {"task.yaml": text.encode()})

    assert saved.check == "checking"
    _, save = await _rows(setup, "save")
    assert (save.publication_id, save.status) == (saved.publication, GradingStatus.DISPATCHED)
    await _step(setup, acme, save.id, "save", "diff", _compiled(outcome="compile_error"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()

    refused = await _row(setup, save.id)
    assert {verdict for _, verdict in _verdicts(refused)} == {"compile_error"}
    async with setup.unit_of_work() as ctx:
        current = await published.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    assert early.publication_id == islands.publication
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    listed = await publications.list(setup, manager, islands.task)
    assert [each.check for each in listed] == [None, "refused"]
    state = await tasks.state(setup, manager, islands.task)
    assert state.latest is not None and state.latest.check == "refused"

    again = await _save(setup, acme, islands.task, {})
    assert (again.publication, again.check) == (saved.publication, "checking")
    *_, checking = await _rows(setup, "save")
    assert (checking.publication_id, checking.attempt) == (saved.publication, 2)


async def _held_back_then_checked(setup: Setup, acme: Acme, islands: Islands) -> None:
    """A run task's save that failed once its publication was made: the
    task stays as the publication before froze it, shown to its organisers
    as refused, until a save of the same files queues its check, which
    lets it take over once it passes."""
    made = await acme.fake.workspaces.list_publications(islands.task)
    assert [each.id for each in made][:-1] == [islands.publication]
    assert made[-1].checked_first
    async with setup.unit_of_work() as ctx:
        current = await published.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication
    assert [row.publication_id for row in await _rows(setup, "save")] == [islands.publication]
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    listed = await publications.list(setup, manager, islands.task)
    assert [each.check for each in listed] == [None, "refused"]

    again = await _save(setup, acme, islands.task, {})

    assert (again.publication, again.check) == (made[-1].id, "checking")
    _, save = await _rows(setup, "save")
    assert (save.publication_id, save.attempt) == (made[-1].id, 1)
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    async with setup.unit_of_work() as ctx:
        current = await published.task(ctx, islands.task)
    assert current is not None and current.publication.id == made[-1].id


async def test_a_save_that_fails_once_it_published_leaves_its_publication_held_until_checked(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    text = await _v2(acme, islands)

    async def down(*_args: Any, **_kwargs: Any) -> int:
        raise Unavailable("The forge did not answer.")

    monkeypatch.setattr(publications, "_number_of", down)
    with pytest.raises(Unavailable):
        await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
    monkeypatch.undo()

    await _held_back_then_checked(setup, acme, islands)


class _Killed(Exception):
    """The process stopping in the middle of a save."""


async def test_a_save_cut_off_by_a_restart_once_it_published_leaves_its_publication_held(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    text = await _v2(acme, islands)

    async def killed(*_args: Any, **_kwargs: Any) -> None:
        raise _Killed

    monkeypatch.setattr(pipelines, "queue_save", killed)
    with pytest.raises(_Killed):
        await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
    monkeypatch.undo()
    await setup.stop()

    restarted = Setup.build(
        setup.settings,
        callback_path=CALLBACK_PATH,
        forge=acme.fake,
        clock=clock,
        keys=key_from_name,
    )
    try:
        await _held_back_then_checked(restarted, acme, islands)
    finally:
        await restarted.stop()


async def test_a_lost_save_row_is_found_by_its_organisers_read_and_tried_again(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands = await _checked(setup, acme)
    await _save(setup, acme, islands.task, {"task.yaml": (await _v2(acme, islands)).encode()})
    _, save = await _rows(setup, "save")
    del acme.fake.ci.runs[RunId(save.run_id or "")]
    clock.advance(LOST_CHECK_AFTER + timedelta(seconds=1))
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)

    listed = await publications.list(setup, manager, islands.task)
    await setup.settle()

    assert [each.check for each in listed] == [None, "checking"]
    *_, retried = await _rows(setup, "save")
    assert (retried.attempt, retried.automatic) == (2, True)


async def test_a_save_while_another_is_checked_is_compared_with_what_entrants_are_graded_on(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    checker = await _save(
        setup, acme, islands.task, {"task.yaml": (await _v2(acme, islands)).encode()}
    )
    typo = await _save(setup, acme, islands.task, {"statement.md": b"# Islands, typo fixed\n"})

    assert typo.grading_changed
    _, replaced, save = await _rows(setup, "save")
    assert (replaced.publication_id, replaced.status) == (
        checker.publication,
        GradingStatus.CANCELLED,
    )
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()

    async with setup.unit_of_work() as ctx:
        current = await published.task(ctx, islands.task)
    assert current is not None and current.publication.id == typo.publication
    first, regraded = await _rows(setup, "early")
    assert (first.publication_id, regraded.publication_id) == (
        islands.publication,
        typo.publication,
    )
    assert (await _row(setup, save.id)).taken_over_at is not None


async def test_a_regrade_of_compares_alone_is_said_at_the_save_and_checks_nothing_out(
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]]
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    saved = await _save(
        setup, acme, islands.task, {"task.yaml": (await _v2(acme, islands)).encode()}
    )
    assert isinstance(saved, Published)
    # Nothing is queued until the save's check passes, which the effect says,
    # with the one step each submission runs again.
    assert (saved.regraded, saved.effect.summary, saved.effect.runs_again) == (
        0,
        "Results change.",
        (
            "Once the save's check passes, 1 submission is graded again, running compare "
            "again, for these changes:",
            "The workflow changed from unicon/islands@v1 to unicon/islands@v2.",
            "The workflow's file diff/diff.py changed.",
        ),
    )
    _, save = await _rows(setup, "save")
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()

    _, regraded = await _rows(setup, "early")
    assert regraded.run_id is not None
    assert [step[0] for step in _steps(asked[-1])] == ["compare", "report"]
    assert asked[-1]["clone"] is None


async def test_a_take_over_whose_work_never_ran_is_finished_by_the_next_read(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    await _save(setup, acme, islands.task, {"task.yaml": (await _v2(acme, islands)).encode()})
    _, save = await _rows(setup, "save")
    monkeypatch.setattr(pipelines, "_take_over_later", lambda ctx, task: None)
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    assert (await _row(setup, save.id)).taken_over_at is None
    assert len(await _rows(setup, "early")) == 1
    monkeypatch.undo()

    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await publications.list(setup, manager, islands.task)
    await setup.settle()

    assert (await _row(setup, save.id)).taken_over_at is not None
    assert len(await _rows(setup, "early")) == 2


async def test_a_save_whose_save_steps_are_stored_takes_over_at_once(
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]]
) -> None:
    islands = await _checked(setup, acme)

    saved = await _save(setup, acme, islands.task, {"statement.md": b"# Islands, again\n"})

    _, save = await _rows(setup, "save")
    assert (save.status, save.run_id) == (GradingStatus.DONE, None)
    async with setup.unit_of_work() as ctx:
        current = await published.task(ctx, islands.task)
    assert current is not None and current.publication.id == saved.publication
    assert len(asked) == 1


async def test_a_pipeline_no_slot_holds_is_not_answered(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slots made smaller since the task was saved leave its pipelines
    nowhere to run, so the platform answers none."""
    islands = await _checked(setup, acme)
    start = acme.fake.grading.start_pipeline
    started: list[PipelineRun] = []

    async def shrunk(as_: AsOrgAccount, run: PipelineRun) -> RunId:
        started.append(run)
        request = acme.fake.grading.config_request(
            None, pipeline_variables(run), now=clock.now(), org=run.org
        )
        with monkeypatch.context() as patched:
            patched.setattr(setup.settings, "slot_sizes", (Slot("tiny", 64),))
            with pytest.raises(CiRequestRefused):
                await runs.config(setup, request)
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", shrunk)
    await _submit(setup, acme, islands)

    assert [run.pipeline for run in started] == ["early"]


async def test_the_platform_answers_a_pipeline_only_while_it_starts_with_its_own_variables(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    started: list[PipelineRun] = []
    start = acme.fake.grading.start_pipeline

    async def refusing(as_: AsOrgAccount, run: PipelineRun) -> RunId:
        started.append(run)
        for variables in (
            {**pipeline_variables(run), "UNICON_PIPELINE": "late"},
            {**pipeline_variables(run), "UNICON_EXTRA": "1"},
        ):
            request = acme.fake.grading.config_request(
                None, variables, now=clock.now(), org=run.org
            )
            with pytest.raises(CiRequestRefused):
                await runs.config(setup, request)
        other = acme.fake.grading.config_request(
            None, pipeline_variables(run), now=clock.now(), org=OrgId("beta")
        )
        with pytest.raises(CiRequestRefused):
            await runs.config(setup, other)
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", refusing)
    await _submit(setup, acme, islands)

    [run] = started
    request = acme.fake.grading.config_request(
        None, pipeline_variables(run), now=clock.now(), org=run.org
    )
    with pytest.raises(CiRequestRefused):
        await runs.config(setup, request)
