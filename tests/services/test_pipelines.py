"""A run task's gradings as pipelines on the org's dispatch repository
(SIMPLER-CORE.md 11.3, 11.7, 11.8), against the fake CI, which asks the
platform what each pipeline is as it starts it, as Woodpecker does:
islands' early pipeline holds the compile and the sample runs, its late
one the rest, made only once the early one ends `done`; an early compile
error gives every late run its verdict with no late pipeline; an early
`system_error` is retried once and makes no late row, nor does a cancel;
a late one is retried once, lost or failed, found so by its entrant's
read, and told to no entrant; a regrade stops a late row of the grading
it replaces; a late row reads what its early row read, and reuses as it
did; an upload is linked only when the submission took it; a task with
nothing early makes its late row at once, with or without the platform's
images; each row has a user of its own; a save is
checked by a pipeline of its own before it takes over, compared with the
publication entrants are graded on, and one whose checker does not
compile is refused while the old version keeps grading, shown so to its
organisers and checked again by a save of the same files; a lost save
row is found by its organisers' read; a take-over whose work never ran
is finished by the next read; and the platform answers a pipeline only
while its row starts, and only for the variables it was started with.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from forge.adapters.ci.fake import dispatch_of, pipeline_variables
from forge.db.tables import Grading, GradingStep, StepOutput
from forge.db.tables import Upload as UploadRow
from forge.domain.errors import CiRequestRefused, Misconfigured
from forge.domain.grading import (
    BOXED_SHAPE,
    LOST_CHECK_AFTER,
    PLATFORM_POOL,
    GradingStatus,
    PipelineRun,
)
from forge.domain.identity import PLATFORM, AsOrgAccount
from forge.domain.ids import OrgId, RunId
from forge.domain.roles import Role, Scope
from forge.domain.run_plan import PipelineKind
from forge.domain.showing import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import (
    gradings,
    pipelines,
    publications,
    published,
    runs,
    steps,
    submissions,
    tasks,
)
from forge.services.pipelines import USER_FIRST
from forge.settings import Settings
from forge.testing import APP_URL, FORGE_URL, FakeClock, register_entrant
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
    assert (save["labels"], save["clone"]) == ([PLATFORM_POOL], None)
    assert first["labels"] == [PLATFORM_POOL, BOXED_SHAPE]
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
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch(score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE
    assert _verdicts(await _row(setup, late.id)) == [("r3", "accepted"), ("r4", "accepted")]
    assert await _seen(setup, session, islands) == [(1, SubmissionState.GRADED)]


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
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]]
) -> None:
    islands = await _checked(setup, acme, NOTHING_EARLY)
    session = await _submit(setup, acme, islands)

    [early] = await _rows(setup, "early")
    assert (early.status, early.run_id) == (GradingStatus.DONE, None)
    assert early.result is not None and early.result["runs"] == []
    [late] = await _rows(setup, "late")
    assert (late.early_id, late.status) == (early.id, GradingStatus.DISPATCHED)
    assert [step[0] for step in _steps(asked[-1])] == ["compile", "solution", "compare", "report"]
    assert await _seen(setup, session, islands) == [(1, SubmissionState.GRADED)]


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
