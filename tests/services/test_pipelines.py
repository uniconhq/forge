"""A task's gradings as pipelines on the org's dispatch repository
(SIMPLER-CORE.md 11.3, 11.7, 11.8), against the fake CI, which asks the
platform what each pipeline is as it starts it, as Woodpecker does:
islands' early pipeline holds the compile and the sample runs, its late
one the rest, made only once the early one ends `done`; an early compile
error gives every late run its verdict with no late pipeline; an early
`system_error` is retried once and makes no late row, nor does a cancel;
a late one is retried once, lost or failed, found so by its entrant's
read, and told to no entrant until the reveal; islands' values are worked
out from both rows as `show` lists them, organisers read each at once with
what it read and the facts, an extension moving `seconds_late`; a fault is
told "being checked" through an early run, and through a late one only
once its runs show; an entrant reads only the texts their result shows,
by where it shows them, never told the output; a staff retry of a late
row takes nothing away while it runs; a regrade stops a late row of the
grading it replaces, and a late row that failed is not tried
again once a regrade replaced its early row; a late row reads what its
early row read, and reuses as it did; an upload is linked only when the
submission took it; a task with nothing early makes its late row at
once, told as submitted until the reveal, with or without the platform's
images, and `compiled` waits for every reader until that row compiles; a
step whose image the platform withdrew ends its start saying so and is
refused at a save; each row has a user of its own, and takes one a lost
row held; a save is checked by a pipeline of its own before
it takes over, compared with the publication entrants are graded on, and
one whose checker does not compile is refused while the old version
keeps grading, shown so to its organisers and checked again by a save of
the same files; a save that fails, or is cut off by a restart, once its
publication is made leaves it held back until a save of the same files
checks it; a lost save row is found by its organisers' read; a take-over
whose work never ran is finished by the next read; and the platform
answers a pipeline only while its row starts, and only for the variables
it was started with; a save's checks are graded by its own pipeline, and
one that does not get what it expects refuses the save, saying why; and
a task's regrade is summed up by each value and verdict it changed,
for a manager of the task, a save moving the pin asking first.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import func, select, text, update

from forge.adapters.ci.fake import dispatch_of, pipeline_variables
from forge.db.tables import Grading, GradingStep, Regrade, StepOutput
from forge.db.tables import Upload as UploadRow
from forge.db.tables.entrants import Entrant
from forge.db.tables.gradings import PASSED
from forge.domain import step_data
from forge.domain.content import Edit
from forge.domain.errors import (
    CiRequestRefused,
    ConfirmationRequired,
    Forbidden,
    Misconfigured,
    NotFound,
    Rejected,
    Unavailable,
)
from forge.domain.grading import (
    BOXED_SHAPE,
    LOST_CHECK_AFTER,
    MACHINE_WAIT,
    NEVER_BEGAN,
    PLATFORM_POOL,
    GradingStatus,
    PipelineRun,
    Slot,
)
from forge.domain.identity import PLATFORM, AsOrgAccount, User
from forge.domain.ids import OrgId, RunId, TaskId
from forge.domain.keys import key_from_name
from forge.domain.leaderboards import Counted
from forge.domain.live import Audience, Nudge, NudgeKind, hears
from forge.domain.names import EntrantOwner
from forge.domain.regrades import ValueChange, VerdictChange
from forge.domain.roles import Role, RoleGrant, Scope, task_scope
from forge.domain.run_plan import PipelineKind
from forge.domain.run_showing import TextAt
from forge.domain.submission_state import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import (
    entrant_view,
    gradings,
    leaderboards,
    pipelines,
    primitives,
    regrades,
    runs,
    steps,
    submissions,
    task_save,
    tasks,
)
from forge.services.access import Organiser
from forge.services.gradings import START_RETRY_WAIT
from forge.services.gradings.following import stop as stop_row
from forge.services.gradings.lookups import finish as end_row
from forge.services.pipelines import USER_FIRST
from forge.services.task_save import Draft, Published
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
    _named,
    _report,
    _row,
    _runs,
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
LOG_SHOWN = TASKS["islands"][0].replace("sample_tests, samples/]", "sample_tests, samples/, log]")
"""islands showing the compile log, a report of its step done once, with
its runs."""


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
            run.org, pipeline_variables(run), now=clock.now()
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


async def _checked(
    setup: Setup, acme: Acme, text: str = TASKS["islands"][0], contest: str = LIVE
) -> Islands:
    """islands published in a running contest, its save pipeline run and
    reported, so its publication has taken over."""
    islands = await _islands(setup, acme, text=text, contest=contest)
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
    # Every pipeline takes any slot of its size, first come, first served.
    assert (save["labels"], save["clone"]) == ([PLATFORM_POOL, SMALL], None)
    assert first["labels"] == [PLATFORM_POOL, BOXED_SHAPE, SMALL]
    assert first["clone"]["image"] == setup.settings.plugin_git_image
    assert first["clone"]["submission"] == early.submission_id
    user = early.run_user
    assert user is not None and user >= USER_FIRST
    assert _steps(first) == [
        ("s1", user, []),
        ("s2", None, ["s1"]),
        ("s4", None, ["s2"]),
        ("report", user, ["s1", "s2", "s4"]),
    ]
    assert all(
        step["settings"]["data_url"].endswith(f"/steps/{step['name']}") for step in first["steps"]
    )
    report = first["steps"][-1]
    assert (report["image"], report["always"]) == (setup.settings.primitive_base_image, True)
    assert report["settings"]["mode"] == "report"
    assert all(step["settings"]["data_token"] for step in first["steps"])

    assert await _early(setup, acme, early.id) is GradingStatus.DONE

    assert _verdicts(await _row(setup, early.id)) == [("r1", "accepted"), ("r2", "accepted")]
    [late] = await _rows(setup, "late")
    assert (late.early_id, late.attempt, late.status) == (early.id, 1, GradingStatus.DISPATCHED)
    assert late.idempotency_key is None
    assert [step[0] for step in _steps(asked[-1])] == ["s2", "s4", "report"]
    assert asked[-1]["labels"] == [PLATFORM_POOL, BOXED_SHAPE, SMALL]
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


async def _graded_both(
    setup: Setup, acme: Acme, text: str = TASKS["islands"][0], contest: str = LIVE
) -> Any:
    """islands as `text` has it in the contest `contest`, one submission
    graded early and late before the close; the entrant's session."""
    islands = await _checked(setup, acme, text, contest)
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
    assert set(grading.runs[0]) == {
        "group",
        "place",
        "tuples",
        "verdict",
        "passed",
        "reports",
        "texts",
        "message",
    }
    assert grading.runs[0]["tuples"] == [
        {"title": None, "key": {"input": "samples/1.in", "answer": "samples/1.ans"}}
    ]
    by_title = {report["title"]: report for report in grading.runs[0]["reports"]}
    assert by_title["Run time"] == {"title": "Run time", "unit": "ms", "number": 12}
    assert by_title["Memory used"] == {"title": "Memory used", "unit": "KB", "number": 1500}
    # When the page reads it again, its late runs then shown.
    assert grading.reveal_at is not None and BEFORE_CLOSE < grading.reveal_at <= AFTER_CLOSE

    clock.set(AFTER_CLOSE)
    grading = await _grading(setup, session, islands)
    assert grading.status == SubmissionState.GRADING
    assert grading.reveal_at is None
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
    # No entry of show lists tests/, so a run's tuple never names its files.
    assert [run["tuples"] for run in grading.runs[4:]] == [
        [{"title": None, "key": {}}],
        [{"title": None, "key": {}}],
    ]


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
        "on_release: [total, st1_tests, st2_tests]",
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
    # tests/4.in is shown now and tests/3.in is not, so only one tuple names its file.
    assert [run["tuples"][0]["key"] for run in grading.runs[2:]] == [{}, {"input": "tests/4.in"}]


TOTALS = LIVE + (
    "leaderboards:\n"
    "  totals:\n"
    "    audience: {who: entrants, rows: own}\n"
    "    columns:\n"
    "      total: {value: [{block: latest, from: {results: islands}}, total]}\n"
    "    display: [total]\n"
)
"""A leaderboard each entrant reads alone, over their latest submission's
total."""


async def test_a_value_moved_to_now_is_missing_until_the_reveal_where_it_read_late_runs(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    # Item 134: st1, st2 and total read runs this grading ran late.
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme, contest=TOTALS)
    text = (
        TASKS["islands"][0]
        .replace("samples/]", "samples/, st1, st2, total]")
        .replace("[st1, st2, total, all]", "[all]")
    )
    await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
    await setup.settle()
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None

    before = _values(await _grading(setup, session, islands))
    [own] = await leaderboards.seen(setup, session, SPRING)
    staff = await submissions.organised(setup, manager, islands.task, EntrantOwner(entrant), 1)
    clock.set(AFTER_CLOSE)
    after = _values(await _grading(setup, session, islands))
    [revealed] = await leaderboards.seen(setup, session, SPRING)

    # Missing, not 0 from late runs read as never run, nor told waiting.
    assert before == {"sample_score": 1, "sample_time": 12, "st1": None, "st2": None, "total": None}
    assert own.rows[0].cells[0].number is None and not own.rows[0].cells[0].waiting
    assert staff.grading is not None
    assert {value.name: value.number for value in staff.grading.run_values}["total"] == 100
    assert after["total"] == 100
    assert revealed.rows[0].cells[0].number == 100


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


def _values(grading: submissions.Result) -> dict[str, Any]:
    """Each value shown: its number, or `waiting`."""
    return {
        value.name: "waiting" if value.waiting else value.number for value in grading.run_values
    }


async def test_islands_values_are_worked_out_from_both_rows_as_show_lists_them(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, LOG_SHOWN)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    [late] = await _rows(setup, "late")
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch({"r4": "wrong_answer"}, score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE

    grading = await _grading(setup, session, islands)
    assert _values(grading) == {"sample_score": 1, "sample_time": 12}
    assert grading.compiled == 1 and set(grading.once["texts"]) == {"log"}
    assert grading.facts == {}

    clock.set(AFTER_CLOSE)
    grading = await _grading(setup, session, islands)
    # r4's wrong answer counts as score's worst bound, 0.
    assert _values(grading) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": 1,
        "st2": 0,
        "total": 40,
    }
    assert all(not value.self_reported for value in grading.run_values)


async def test_organisers_read_every_value_at_once_with_what_it_read_and_the_facts(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(datetime(2026, 9, 26, 14, 30, tzinfo=UTC))
    due = LIVE + "    due: 2026-09-26T14:00:00Z\n"
    islands = await _checked(setup, acme, contest=due)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None

    async def read() -> submissions.Result:
        graded = await submissions.organised(setup, manager, islands.task, EntrantOwner(entrant), 1)
        assert graded.grading is not None
        return graded.grading

    grading = await read()
    # The late runs are still to come, so what reads them waits.
    assert _values(grading) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": "waiting",
        "st2": "waiting",
        "total": "waiting",
    }
    total = next(value for value in grading.run_values if value.name == "total")
    assert (total.show, total.runs, total.values) == (
        "after_close",
        ("r3", "r4"),
        ("st1", "st2"),
    )
    assert grading.facts == {
        "seconds_late": 1800,
        "seconds": 4 * 3600 + 1800,
        "compiled": 1,
    }
    # An extension moves the row's due, and seconds_late with it, on read.
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Entrant).where(Entrant.id == entrant).values(time_extension_seconds=600)
        )
    assert (await read()).facts["seconds_late"] == Fraction(1200)


async def test_an_early_fault_is_told_being_checked_with_its_runs(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    session = await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")
    await _fault(setup, acme, first.id, "early")
    [_, retried] = await _rows(setup, "early")
    await _fault(setup, acme, retried.id, "early")

    grading = await _grading(setup, session, islands)
    assert grading.status == SubmissionState.DELAYED
    assert _values(grading) == {"sample_score": "waiting", "sample_time": "waiting"}
    assert _runs_seen(grading) == [
        ("sample_tests", 1, "system_error"),
        ("sample_tests", 2, "system_error"),
    ]


async def test_a_late_fault_is_told_only_once_its_runs_are_shown(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    other = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _early(setup, acme, early.id)
    [late] = await _rows(setup, "late")
    await _fault(setup, acme, late.id, "late")
    [_, again] = await _rows(setup, "late")
    await _fault(setup, acme, again.id, "late")
    found = await _grading(setup, other, islands)
    assert (found.status, _values(found)) == (
        SubmissionState.GRADED,
        {"sample_score": 1, "sample_time": 12},
    )
    clock.set(AFTER_CLOSE)
    found = await _grading(setup, other, islands)
    assert found.status == SubmissionState.DELAYED
    assert _values(found) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": "waiting",
        "st2": "waiting",
        "total": "waiting",
    }


async def test_an_entrant_reads_only_the_texts_their_result_shows_them_by_where(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme, LOG_SHOWN)
    grading = await _grading(setup, session, islands)
    # No stored output's id reaches the entrant: a text is read by where it is shown.
    assert (grading.runs[0]["message"], grading.once["texts"]) == ({}, {"log": {}})
    assert "output" not in repr((grading.runs, grading.once))
    [late] = await _rows(setup, "late")
    assert late.result is not None
    hidden = late.result["runs"][0]["message"]
    late_message = TextAt("all", 3, "message", None)

    read = await submissions.read_text(
        setup, session, islands.task, 1, TextAt("sample_tests", 1, "message", None)
    )
    assert read == "Right."
    log = TextAt(None, None, "texts", "log")
    assert await submissions.read_text(setup, session, islands.task, 1, log) == "built"
    for at in (
        late_message,
        TextAt("sample_tests", 1, "texts", "score"),
        TextAt("sample_tests", 9, "message", None),
    ):
        with pytest.raises(NotFound, match="no such text"):
            await submissions.read_text(setup, session, islands.task, 1, at)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    organised = await submissions.read_organised_text(
        setup, manager, islands.task, EntrantOwner(entrant), 1, hidden["output"], "message"
    )
    assert organised == "Right."
    with pytest.raises(NotFound, match="no such text"):
        await submissions.read_organised_text(
            setup, manager, islands.task, EntrantOwner(entrant), 1, hidden["output"], "score"
        )
    clock.set(AFTER_CLOSE)
    assert await submissions.read_text(setup, session, islands.task, 1, late_message) == "Right."


async def test_a_report_of_a_step_done_once_reaches_an_entrant_only_when_show_lists_it(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme)
    log = TextAt(None, None, "texts", "log")

    grading = await _grading(setup, session, islands)
    assert grading.runs and grading.compiled == 1
    assert grading.once == {"reports": {}, "texts": {}}
    with pytest.raises(NotFound, match="no such text"):
        await submissions.read_text(setup, session, islands.task, 1, log)
    clock.set(AFTER_CLOSE)
    assert (await _grading(setup, session, islands)).once == {"reports": {}, "texts": {}}
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    graded = await submissions.organised(setup, manager, islands.task, EntrantOwner(entrant), 1)
    assert graded.grading is not None and set(graded.grading.once["texts"]) == {"log"}


async def test_a_staff_retry_of_the_late_row_takes_nothing_away_while_it_runs(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme)
    [late] = await _rows(setup, "late")
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)

    await gradings.retry(setup, manager, late.id)

    assert [row.attempt for row in await _rows(setup, "late")] == [1, 2]
    clock.set(AFTER_CLOSE)
    grading = await _grading(setup, session, islands)
    assert grading.status == SubmissionState.GRADED
    assert _values(grading)["total"] == 100


async def test_an_early_compile_error_gives_every_late_run_its_verdict_and_no_late_pipeline(
    setup: Setup, acme: Acme, spring: str, asked: list[dict[str, Any]]
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")

    await _step(setup, acme, early.id, "early", "compile", _compiled(outcome="compile_error"))
    access = await _mint(setup, early.id, "early", "solution")
    assert await steps.step_job(setup, early.id, _named(access), _bearer(access)) is None
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
        await steps.step_job(setup, early.id, _named(access), _bearer(access))


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
    assert [step[0] for step in _steps(asked[-1])] == ["s1", "s2", "s4", "report"]
    # Submitted, results after the contest: not graded, with nothing run.
    assert await _seen(setup, session, islands) == [(1, SubmissionState.SUBMITTED)]
    clock.set(AFTER_CLOSE)
    assert await _seen(setup, session, islands) == [(1, SubmissionState.GRADING)]


BUILT_LATE = NOTHING_EARLY.replace(
    "  values:\n", "  values:\n    built: {fact: compiled}\n"
).replace("after_close: [st1, st2, total, all]", "after_close: [st1, st2, total, all, built]")
"""islands with nothing early and a value reading `compiled` shown after
the close, so its compile runs in the late row."""
BUILT_LEADERBOARD = (
    LIVE
    + "leaderboards:\n  built:\n    audience: {who: staff}\n    columns:\n"
    + "      c: {value: [{block: latest, from: {results: islands}}, compiled]}\n"
    + "    display: [c]\n"
)
"""A leaderboard for staff reading the latest submission's `compiled`."""


async def test_compiled_waits_for_every_reader_until_the_late_row_that_compiles_has_run(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, BUILT_LATE, contest=BUILT_LEADERBOARD)
    session = await _submit(setup, acme, islands)
    [late] = await _rows(setup, "late")
    staff = await _staff(setup, acme)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None

    async def read() -> tuple[Any, ...]:
        graded = await submissions.organised(setup, manager, islands.task, EntrantOwner(entrant), 1)
        assert graded.grading is not None
        [leaderboard] = await leaderboards.seen(setup, staff, SPRING, "built")
        mine = _values(await _grading(setup, session, islands)).get("built")
        built = _values(graded.grading)["built"]
        cell = leaderboard.rows[0].cells[0]
        return built, graded.grading.compiled, cell.number, cell.waiting, mine

    # Staff, and the entrant from the reveal, read it waiting, never 1.
    waiting = (Counted("islands", 1),)
    assert await read() == ("waiting", None, None, waiting, None)
    clock.set(AFTER_CLOSE)
    assert await read() == ("waiting", None, None, waiting, "waiting")
    await _step(setup, acme, late.id, "late", "compile", _compiled())
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch(score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE
    assert await read() == (1, 1, 1, (), 1)


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
        "The platform names no image for a task's report step.",
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
    saved = await task_save.save(
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
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    assert early.publication_id == islands.publication
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    listed = await task_save.list_publications(setup, manager, islands.task)
    assert [each.check for each in listed] == [None, "refused"]
    state = await tasks.state(setup, manager, islands.task)
    assert state.latest is not None and state.latest.check == "refused"

    again = await _save(setup, acme, islands.task, {})
    assert (again.publication, again.check) == (saved.publication, "checking")
    *_, checking = await _rows(setup, "save")
    assert (checking.publication_id, checking.attempt) == (saved.publication, 2)


async def _held_back_then_checked(setup: Setup, acme: Acme, islands: Islands) -> None:
    """A task's save that failed once its publication was made: the
    task stays as the publication before froze it, shown to its organisers
    as unchecked, until a save of the same files queues its check, which
    lets it take over once it passes."""
    made = await acme.fake.workspaces.list_publications(islands.task)
    assert [each.id for each in made][:-1] == [islands.publication]
    assert made[-1].checked_first
    async with setup.unit_of_work() as ctx:
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication
    assert [row.publication_id for row in await _rows(setup, "save")] == [islands.publication]
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    listed = await task_save.list_publications(setup, manager, islands.task)
    assert [each.check for each in listed] == [None, "unchecked"]

    again = await _save(setup, acme, islands.task, {})

    assert (again.publication, again.check) == (made[-1].id, "checking")
    _, save = await _rows(setup, "save")
    assert (save.publication_id, save.attempt) == (made[-1].id, 1)
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    async with setup.unit_of_work() as ctx:
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == made[-1].id


async def test_a_save_that_fails_once_it_published_leaves_its_publication_held_until_checked(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    text = await _v2(acme, islands)

    async def down(*_args: Any, **_kwargs: Any) -> int:
        raise Unavailable("The forge did not answer.")

    monkeypatch.setattr(task_save.sources, "_number_of", down)
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

    listed = await task_save.list_publications(setup, manager, islands.task)
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
        current = await entrant_view.task(ctx, islands.task)
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
    assert [step[0] for step in _steps(asked[-1])] == ["s4", "report"]
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
    await task_save.list_publications(setup, manager, islands.task)
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
        current = await entrant_view.task(ctx, islands.task)
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
            run.org, pipeline_variables(run), now=clock.now()
        )
        with monkeypatch.context() as patched:
            patched.setattr(setup.settings, "slot_sizes", (Slot("tiny", 1, 64),))
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
            request = acme.fake.grading.config_request(run.org, variables, now=clock.now())
            with pytest.raises(CiRequestRefused):
                await runs.config(setup, request)
        other = acme.fake.grading.config_request(
            OrgId("beta"), pipeline_variables(run), now=clock.now()
        )
        with pytest.raises(CiRequestRefused):
            await runs.config(setup, other)
        return await start(as_, run)

    monkeypatch.setattr(acme.fake.grading, "start_pipeline", refusing)
    await _submit(setup, acme, islands)

    [run] = started
    request = acme.fake.grading.config_request(run.org, pipeline_variables(run), now=clock.now())
    with pytest.raises(CiRequestRefused):
        await runs.config(setup, request)


# Checks


CHECKS = """\
checks:
  - submit: {code: checks/good}
    expect: {verdict: accepted, total: 100}
  - submit: {code: checks/empty}
    expect: {verdict: wrong_answer}
  - submit: {code: checks/slow}
    expect: {st1: 1, st2: 0}
"""
CHECK_FILES = {
    "checks/good/main.cpp": b"int main() {}\n",
    "checks/empty/main.cpp": b"int main() { return 0; }\n",
    "checks/slow/main.cpp": b"int main() { for (;;); }\n",
}
EVERY = ("r1", "r2", "r3", "r4")


def _tagged(write: Any, tag: bytes) -> Any:
    """`write`, each file it writes but `outputs.json` ending in `tag`."""

    def tagged(job: dict[str, Any]) -> dict[str, bytes]:
        written: dict[str, bytes] = write(job)
        return {path: content + tag * (path != "outputs.json") for path, content in written.items()}

    return tagged


async def _checks_run(
    setup: Setup,
    acme: Acme,
    save: uuid.UUID,
    good: dict[str, str] | None = None,
    *,
    diff: bool = True,
) -> dict[str, dict[str, Any]]:
    """A save row's diff, unless it was done before, and its three checks'
    copies run as a machine would: the good one accepted on every run but
    those `good` names, the empty one a wrong answer on every run, and the
    slow one out of time on r4; each step's job. Each check's steps write
    files of their own, so no copy reuses another's."""
    jobs = {}
    if diff:
        jobs["diff"] = await _step(setup, acme, save, "save", "diff", _compiled(CHECKER))
    compare = {
        1: _batch(good, score=True),
        2: _batch(dict.fromkeys(EVERY, "wrong_answer"), score=True),
        3: _batch(score=True),
    }
    solution = {1: _batch(), 2: _batch(), 3: _batch({"r4": "time_limit"})}
    for index in (1, 2, 3):
        name = f"c{index}-"
        tag = name.encode()
        for step, write in (
            ("compile", _compiled()),
            ("solution", solution[index]),
            ("compare", compare[index]),
        ):
            jobs[f"{name}{step}"] = await _step(
                setup, acme, save, "save", f"{name}{step}", _tagged(write, tag)
            )
    return jobs


async def test_a_saves_checks_are_graded_by_its_pipeline_and_each_gets_what_it_expects(
    setup: Setup, acme: Acme, spring: str
) -> None:
    text = TASKS["islands"][0] + CHECKS
    islands = await _islands(setup, acme, text=text, contest=LIVE, files=CHECK_FILES)
    [save] = await _rows(setup, "save")

    jobs = await _checks_run(setup, acme, save.id)
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()

    compiled = jobs["c1-compile"]
    assert compiled["fail_on_non_accepted"] is False
    assert compiled["inputs"]["inputs"]["language"] == "cpp"
    assert {entry["path"] for entry in compiled["files"]} == {
        f"{compiled['inputs']['inputs']['source']['folder']}/main.cpp"
    }
    assert _runs(jobs["c3-compare"]) == ["r1", "r2", "r3"]
    row = await _row(setup, save.id)
    assert row.status == GradingStatus.DONE and row.result is not None
    assert [(each["passed"], each["failing"]) for each in row.result["checks"]] == [
        (True, []),
        (True, []),
        (True, []),
    ]
    # A check that passed keeps none of its runs in the save's result.
    assert [each["runs"] for each in row.result["checks"]] == [[], [], []]
    async with setup.unit_of_work() as ctx:
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication


async def test_a_check_that_does_not_get_what_it_expects_refuses_its_save_saying_why(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    text = TASKS["islands"][0] + CHECKS
    saved = await _save(setup, acme, islands.task, {"task.yaml": text.encode(), **CHECK_FILES})
    assert saved.check == "checking"
    _, save = await _rows(setup, "save")
    told: list[Nudge] = []
    nudge = Context.nudge

    def heard(ctx: Context, made: Nudge) -> None:
        told.append(made)
        nudge(ctx, made)

    monkeypatch.setattr(Context, "nudge", heard)

    await _checks_run(setup, acme, save.id, good={"r4": "wrong_answer"}, diff=False)
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    [checks] = [each for each in told if each.kind is NudgeKind.CHECKS]
    assert checks.id == save.publication_id and checks.entrant is None

    async with setup.unit_of_work() as ctx:
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    listed = await task_save.list_publications(setup, manager, islands.task)
    assert [each.check for each in listed] == [None, "refused"]
    [failed] = listed[1].failed_checks
    assert (failed.index, failed.failing) == (
        1,
        ("The run r4 got wrong_answer, not accepted.", "total is 40, not 100."),
    )
    assert [(run.id, run.verdict) for run in failed.runs][-1] == ("r4", "wrong_answer")
    assert failed.outcomes == {"compile": "accepted"} and failed.error is None
    state = await tasks.state(setup, manager, islands.task)
    assert state.latest is not None and state.latest.failed_checks == listed[1].failed_checks


async def test_each_end_of_a_save_row_nudges_the_tasks_organisers(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save row that ends done, with a system error, or cancelled by a
    later save each tells the task's organisers once, naming the
    publication. An entrant is not told, and an early row's end is not
    this nudge. A save whose checks refuse it ends done, and that path
    sends the same nudge."""
    islands = await _checked(setup, acme)
    text = TASKS["islands"][0] + CHECKS
    await _save(setup, acme, islands.task, {"task.yaml": text.encode(), **CHECK_FILES})
    _, save = await _rows(setup, "save")
    told: list[Nudge] = []
    nudge = Context.nudge

    def heard(ctx: Context, made: Nudge) -> None:
        told.append(made)
        nudge(ctx, made)

    monkeypatch.setattr(Context, "nudge", heard)

    async def ended(status: GradingStatus) -> Nudge:
        told.clear()
        async with setup.unit_of_work() as ctx:
            row = await ctx.db.scalar(select(Grading).where(Grading.id == save.id))
            assert row is not None
            end_row(ctx, row, status)
        checks = [each for each in told if each.kind is NudgeKind.CHECKS]
        assert len(checks) == 1
        return checks[0]

    done = await ended(GradingStatus.DONE)
    faulted = await ended(GradingStatus.SYSTEM_ERROR)
    told.clear()
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.scalar(select(Grading).where(Grading.id == save.id))
        assert row is not None
        stop_row(ctx, row)
    [cancelled] = [each for each in told if each.kind is NudgeKind.CHECKS]
    for each in (done, faulted, cancelled):
        assert each.id == save.publication_id
        assert (each.user, each.entrant, each.contest) == (None, None, None)
        assert each.scope == task_scope(islands.task)
    organiser = Audience(7, (RoleGrant(task_scope(islands.task), Role.OBSERVER),), frozenset())
    member = Audience(8, (), frozenset({SPRING}), frozenset({"someone"}))
    elsewhere = Audience(
        9, (RoleGrant(Scope("acme", "spring", "other"), Role.OBSERVER),), frozenset()
    )
    assert hears(organiser, done)
    assert not hears(member, done)
    assert not hears(elsewhere, done)

    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    told.clear()
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.scalar(select(Grading).where(Grading.id == early.id))
        assert row is not None
        end_row(ctx, row, GradingStatus.DONE)
    assert [each for each in told if each.kind is NudgeKind.CHECKS] == []


async def test_a_check_that_faults_ends_the_save_row_a_system_error_and_it_is_tried_again(
    setup: Setup, acme: Acme, spring: str
) -> None:
    text = TASKS["islands"][0] + CHECKS
    islands = await _islands(setup, acme, text=text, contest=LIVE, files=CHECK_FILES)
    [save] = await _rows(setup, "save")
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER))
    for step, write in (
        ("c1-compile", _compiled()),
        ("c1-solution", _batch()),
        ("c1-compare", _batch(score=True)),
        ("c2-compile", lambda job: {"outputs.json": b"{"}),
    ):
        await _step(setup, acme, save.id, "save", step, _tagged(write, step[:3].encode()))

    assert await _report(setup, save.id, "save") is GradingStatus.SYSTEM_ERROR
    await setup.settle()
    row = await _row(setup, save.id)
    assert row.error is not None and row.error.startswith("Check 2: ")
    assert row.result is not None
    assert row.result["checks"][1]["failing"][0].startswith("Its grading faulted: ")
    # The row is tried again, the steps done before not run again, and once
    # its checks pass the save takes over.
    [_, again] = await _rows(setup, "save")
    assert again.attempt == 2
    wrong = dict.fromkeys(EVERY, "wrong_answer")
    for step, write in (
        ("c2-compile", _compiled()),
        ("c2-solution", _batch()),
        ("c2-compare", _batch(wrong, score=True)),
        ("c3-compile", _compiled()),
        ("c3-solution", _batch({"r4": "time_limit"})),
        ("c3-compare", _batch(score=True)),
    ):
        await _step(setup, acme, again.id, "save", step, _tagged(write, step[:3].encode()))
    assert await _report(setup, again.id, "save") is GradingStatus.DONE
    await setup.settle()
    async with setup.unit_of_work() as ctx:
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication


async def test_checks_at_a_publication_whose_settings_do_not_read_fail_saying_so(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands = await _checked(setup, acme)
    text = TASKS["islands"][0] + CHECKS
    await _save(setup, acme, islands.task, {"task.yaml": text.encode(), **CHECK_FILES})
    _, save = await _rows(setup, "save")

    def unread(content: bytes) -> Any:
        raise ValueError("unread")

    monkeypatch.setattr(steps, "parse_task", unread)
    await _checks_run(setup, acme, save.id, diff=False)
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()

    row = await _row(setup, save.id)
    assert row.result is not None
    assert [(each["passed"], each["failing"]) for each in row.result["checks"]] == [
        (False, [steps.CHECKS_UNREAD])
    ] * 3
    async with setup.unit_of_work() as ctx:
        current = await entrant_view.task(ctx, islands.task)
    assert current is not None and current.publication.id == islands.publication


# A task's regrade summary


RUNS = {
    "r1": {"input": "samples/1.in", "answer": "samples/1.ans"},
    "r2": {"input": "samples/2.in", "answer": "samples/2.ans"},
    "r3": {"input": "tests/3.in", "answer": "tests/3.ans"},
    "r4": {"input": "tests/4.in", "answer": "tests/4.ans"},
}


def _result(runs: dict[str, str], compile: str = "accepted") -> dict[str, Any]:
    """A result version 6 holding `runs`, each with its verdict, an accepted
    run scoring 1, and the compile's outcome when it holds one."""
    return {
        "schema_version": 6,
        "once": {
            "texts": {},
            "reports": {},
            "outcomes": {"compile": compile} if "r1" in runs else {},
        },
        "runs": [
            {
                "id": run,
                "key": RUNS[run],
                "texts": {},
                "passed": int(verdict == "accepted"),
                "reports": {"score": 1, "time_ms": 12, "memory_kb": 1500}
                if verdict == "accepted"
                else {},
                "verdict": verdict,
            }
            for run, verdict in runs.items()
        ],
        "error": None,
        "staff": [],
        "run_log": None,
    }


ACCEPTED_EARLY = _result({"r1": "accepted", "r2": "accepted"})
ACCEPTED_LATE = _result({"r3": "accepted", "r4": "accepted"})


async def _ended(
    setup: Setup, early: Grading, result: dict[str, Any], late: dict[str, Any] | None
) -> None:
    """The early row `early` ended `done` with `result`, and its late row,
    when `late` is given, made and ended `done` with it."""
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Grading, early.id)
        assert row is not None
        row.status, row.result, row.finished_at = GradingStatus.DONE, result, ctx.now
        if late is not None:
            made = await ctx.db.scalar(
                select(func.count())
                .select_from(Grading)
                .where(Grading.submission_id == row.submission_id, Grading.pipeline == "late")
            )
            ctx.db.add(
                Grading(
                    task_id=row.task_id,
                    workspace_id=row.workspace_id,
                    submission_id=row.submission_id,
                    submission_number=row.submission_number,
                    submission_version=row.submission_version,
                    submitted_at=row.submitted_at,
                    publication_id=row.publication_id,
                    pipeline="late",
                    early_id=row.id,
                    regrade_id=row.regrade_id,
                    attempt=(made or 0) + 1,
                    status=GradingStatus.DONE,
                    queued_at=ctx.now,
                    finished_at=ctx.now,
                    result=late,
                )
            )


GRADEBOOK = LIVE + (
    "leaderboards:\n"
    "  gradebook:\n"
    "    title: Gradebook\n"
    "    columns:\n"
    "      picked: {block: latest, from: {results: islands}}\n"
    "      st2: {block: otherwise, of: [{value: [{column: picked}, st2]}, 0]}\n"
    "      total: {block: otherwise, of: [{value: [{column: picked}, total]}, 0]}\n"
    "    sort: [{total: desc}]\n"
    "    display: [st2, total]\n"
)
"""The running contest with a staff leaderboard reading islands' latest
submission, ranked by its total."""


async def _regraded(
    setup: Setup, acme: Acme, count: int, contest: str = LIVE
) -> tuple[Islands, uuid.UUID, list[Grading]]:
    """islands with `count` submissions each graded every run accepted, then
    rejudged; the regrade and its early rows, still to run."""
    islands = await _checked(setup, acme, contest=contest)
    for user in range(20, 20 + count):
        acme.fake.add_user(user, f"entrant{user}")
        await _submit(setup, acme, islands, user=user)
    for row in await _rows(setup, "early"):
        await _ended(setup, row, ACCEPTED_EARLY, ACCEPTED_LATE)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    rejudged = await gradings.rejudge(setup, manager, islands.task)
    assert rejudged.regrade is not None and rejudged.queued == count
    async with setup.unit_of_work() as ctx:
        rows = list(
            await ctx.db.scalars(
                select(Grading)
                .where(Grading.regrade_id == rejudged.regrade)
                .order_by(Grading.queued_at, Grading.submission_number, Grading.id)
            )
        )
    return islands, rejudged.regrade, rows


async def _staff(setup: Setup, acme: Acme) -> Any:
    """A session of an observer of the contest."""
    await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)
    return await signed_in(setup, acme.fake, 7)


async def test_staff_read_every_row_of_a_staff_leaderboard_and_nobody_else_reads_it(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme, contest=GRADEBOOK)
    entrant = await _submit(setup, acme, islands)
    await _submit(setup, acme, islands, user=9)
    first, waiting = await _rows(setup, "early")
    await _ended(setup, first, ACCEPTED_EARLY, ACCEPTED_LATE)
    staff = await _staff(setup, acme)

    [leaderboard] = await leaderboards.seen(setup, staff, SPRING)

    # A leaderboard with no audience is staff's, so an entrant and a visitor
    # read none.
    assert await leaderboards.seen(setup, entrant, SPRING) == ()
    assert await leaderboards.seen(setup, None, SPRING) == ()
    assert (leaderboard.key, leaderboard.title, leaderboard.audience.who) == (
        "gradebook",
        "Gradebook",
        "staff",
    )
    graded, grading = leaderboard.rows
    assert (graded.place, grading.place) == (1, 2)
    assert [(cell.column, cell.number) for cell in graded.cells] == [
        ("st2", Fraction(1)),
        ("total", Fraction(100)),
    ]
    assert graded.cells[1].counted == (Counted("islands", first.submission_number),)
    # A submission still grading is left out as waiting, so its row has
    # no total yet and otherwise gives 0.
    assert [(cell.column, cell.number) for cell in grading.cells] == [
        ("st2", Fraction(0)),
        ("total", Fraction(0)),
    ]
    assert grading.cells[1].waiting == (Counted("islands", waiting.submission_number),)


async def test_a_staff_read_of_a_leaderboard_starts_the_retry_of_a_failure_it_finds(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme, contest=GRADEBOOK)
    await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Grading, first.id)
        assert row is not None
        row.status, row.error = GradingStatus.SYSTEM_ERROR, "The step crashed."

    await leaderboards.seen(setup, await _staff(setup, acme), SPRING)
    await setup.settle()

    assert [(row.attempt, row.automatic) for row in await _rows(setup, "early")] == [
        (1, False),
        (2, True),
    ]


async def test_a_visitors_or_an_entrants_read_of_a_leaderboard_starts_no_retry(
    setup: Setup, acme: Acme, spring: str
) -> None:
    contest = GRADEBOOK.replace("    title: Gradebook\n", "    audience: {who: everyone}\n")
    islands = await _checked(setup, acme, contest=contest)
    entrant = await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Grading, first.id)
        assert row is not None
        row.status, row.error = GradingStatus.SYSTEM_ERROR, "The step crashed."

    # Only staff's read sees to a failure: a read with no session writes
    # nothing, and the entrant's own submission page sees to theirs.
    assert len(await leaderboards.seen(setup, None, SPRING)) == 1
    assert len(await leaderboards.seen(setup, entrant, SPRING)) == 1
    await setup.settle()

    assert [(row.attempt, row.automatic) for row in await _rows(setup, "early")] == [(1, False)]


async def test_staff_read_a_rows_earlier_result_while_its_newest_submission_grades(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands = await _checked(setup, acme, contest=GRADEBOOK)
    session = await _submit(setup, acme, islands)
    [first] = await _rows(setup, "early")
    await _ended(setup, first, ACCEPTED_EARLY, ACCEPTED_LATE)
    clock.advance(timedelta(minutes=1))
    made = await upload(setup, acme.fake, session, islands.task, CODE, input="code")
    await submissions.submit(
        setup,
        session,
        islands.task,
        {"code": SubmittedInput(uploads=(made.id,)), "language": SubmittedInput(value="python")},
        idempotency_key=uuid.uuid4().hex,
    )
    await setup.settle()

    [leaderboard] = await leaderboards.seen(setup, await _staff(setup, acme), SPRING)

    # The latest submission's st2 and total still wait, so the pick passes
    # over it to the one before, and says it is waiting.
    [row] = leaderboard.rows
    assert [(cell.column, cell.number) for cell in row.cells] == [
        ("st2", Fraction(1)),
        ("total", Fraction(100)),
    ]
    assert row.cells[1].counted == (Counted("islands", 1),)
    assert row.cells[1].waiting == (Counted("islands", 2),)


async def test_a_manager_or_observer_of_the_task_alone_reads_no_leaderboard(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme, contest=GRADEBOOK)
    await _submit(setup, acme, islands)

    for user, role in ((11, Role.MANAGER), (12, Role.OBSERVER)):
        acme.fake.add_user(user, f"staff{user}")
        await acme.fake.orgs.grant_role(user, ISLANDS, role)
        session = await signed_in(setup, acme.fake, user)
        # A leaderboard reads other tasks, so a role at one task reads none.
        assert await leaderboards.seen(setup, session, SPRING) == ()
        assert await leaderboards.seen(setup, session, SPRING, "gradebook") == ()


def _task_only(acme: Acme, role: Role) -> Any:
    """Someone holding `role` at islands and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(ISLANDS, role),),
        scope=ISLANDS,
        role=role,
        identity=acme.ada.identity,
    )


async def test_a_regrade_lists_each_submission_whose_values_it_changed_old_to_new(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands, regrade, rows = await _regraded(setup, acme, 5, GRADEBOOK)
    wrong = _result({"r3": "accepted", "r4": "wrong_answer"})
    for index, row in enumerate(rows):
        await _ended(setup, row, ACCEPTED_EARLY, wrong if index < 4 else ACCEPTED_LATE)

    summary = await regrades.run_summary(setup, acme.ada, islands.task, regrade)

    assert (summary.total, summary.running, summary.failed) == (5, 0, 0)
    assert len(summary.changed) == 4
    assert {each.grading for each in summary.changed} == {row.id for row in rows[:4]}
    for each in summary.changed:
        assert each.values == (
            ValueChange("st2", Fraction(1), Fraction(0)),
            ValueChange("total", Fraction(100), Fraction(40)),
        )
        assert each.verdicts == (VerdictChange("r4", "accepted", "wrong_answer"),)
    # The contest's manager reads every row of the leaderboard reading islands,
    # its places and cells before and after.
    assert summary.leaderboards is not None
    [leaderboard] = summary.leaderboards
    assert (leaderboard.leaderboard, leaderboard.title, leaderboard.sort) == (
        "gradebook",
        "Gradebook",
        (("total", "desc"),),
    )
    assert [header.name for header in leaderboard.headers] == ["st2", "total"]
    # Every row tied first before; after, the one whose st2 held leads
    # and the 4 tie second, listed by name.
    assert [(row.place_before, row.place_after) for row in leaderboard.rows] == [(1, 1)] + [
        (1, 2)
    ] * 4
    dropped = [row.name for row in leaderboard.rows[1:]]
    assert dropped == sorted(dropped) and len(set(dropped)) == 4
    for dropped_row in leaderboard.rows[1:]:
        assert [(cell.column, cell.before, cell.after) for cell in dropped_row.cells] == [
            ("st2", Fraction(1), Fraction(0)),
            ("total", Fraction(100), Fraction(40)),
        ]
    assert [cell.changed for cell in leaderboard.rows[0].cells] == [False, False]


async def test_the_leaderboard_diff_reads_the_result_before_while_a_regrade_runs_or_was_cancelled(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands, regrade, rows = await _regraded(setup, acme, 3, GRADEBOOK)
    # One regrade attempt done unchanged, one still queued, one cancelled.
    await _ended(setup, rows[0], ACCEPTED_EARLY, ACCEPTED_LATE)
    async with setup.unit_of_work() as ctx:
        cancelled = await ctx.db.get(Grading, rows[2].id)
        assert cancelled is not None
        cancelled.status = GradingStatus.CANCELLED

    summary = await regrades.run_summary(setup, acme.ada, islands.task, regrade)
    [leaderboard] = await leaderboards.seen(setup, await _staff(setup, acme), SPRING)

    assert (summary.running, summary.failed, summary.changed) == (1, 1, ())
    assert summary.leaderboards is not None
    [diff] = summary.leaderboards
    # Each row reads as the live leaderboard does: the result from before counts
    # until the regrade's own attempt finishes with one, so nothing drops.
    live = {
        row.owner: [(cell.column, cell.number) for cell in row.cells] for row in leaderboard.rows
    }
    for row in diff.rows:
        assert [(cell.column, cell.after) for cell in row.cells] == live[row.owner]
        assert [(cell.column, cell.before, cell.after) for cell in row.cells] == [
            ("st2", Fraction(1), Fraction(1)),
            ("total", Fraction(100), Fraction(100)),
        ]
        assert (row.place_before, row.place_after) == (1, 1)
        assert all(cell.waiting == () for cell in row.cells)
    # The row the regrade is still grading says so; the cancelled one is
    # done with.
    assert [row.regrading for row in diff.rows if row.regrading] == [
        (Counted("islands", rows[1].submission_number),)
    ]


MIRRORED = GRADEBOOK + (
    "  mirror:\n"
    "    title: Mirror\n"
    "    columns:\n"
    "      total: {column: [gradebook, total]}\n"
    "    sort: [{total: desc}]\n"
    "    display: [total]\n"
)
"""The gradebook, and a leaderboard reading islands only through its total."""


async def test_the_leaderboard_diff_covers_a_leaderboard_reading_the_task_through_another_alone(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands, regrade, rows = await _regraded(setup, acme, 2, MIRRORED)
    wrong = _result({"r3": "accepted", "r4": "wrong_answer"})
    await _ended(setup, rows[0], ACCEPTED_EARLY, wrong)
    await _ended(setup, rows[1], ACCEPTED_EARLY, ACCEPTED_LATE)
    # A submission made after the regrade began has no result that counted
    # then, nor one of the regrade's, so it is on neither side.
    clock.advance(timedelta(minutes=1))
    acme.fake.add_user(30, "entrant30")
    await _submit(setup, acme, islands, user=30)
    newest = (await _rows(setup, "early"))[-1]
    await _ended(setup, newest, ACCEPTED_EARLY, ACCEPTED_LATE)

    summary = await regrades.run_summary(setup, acme.ada, islands.task, regrade)

    assert summary.leaderboards is not None
    assert [each.leaderboard for each in summary.leaderboards] == ["gradebook", "mirror"]
    mirror = summary.leaderboards[1]
    assert [
        (row.name, row.place_before, row.place_after, row.cells[0].before, row.cells[0].after)
        for row in mirror.rows
    ] == [
        (mirror.rows[0].name, 1, 1, Fraction(100), Fraction(100)),
        (mirror.rows[1].name, 1, 2, Fraction(100), Fraction(40)),
        ("entrant30", 3, 3, Fraction(0), Fraction(0)),
    ]


def test_a_regrades_sides_read_the_attempts_that_counted_when_it_began_and_its_own() -> None:
    began = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    regrade, other = uuid.uuid4(), uuid.uuid4()

    def attempt(number: int, queued: datetime, of: uuid.UUID | None = None) -> Grading:
        return Grading(attempt=number, queued_at=queued, regrade_id=of)

    earlier = attempt(1, began - timedelta(hours=1))
    own = attempt(2, began, regrade)
    later = attempt(3, began + timedelta(minutes=5), other)
    newcomer = attempt(1, began + timedelta(minutes=1))
    task: Any = None
    before, after = leaderboards.regrade_sides(
        regrade, began, TaskId("acme/spring/islands"), task, task
    )

    assert before.attempts([earlier, own, later]) == [earlier]
    assert after.attempts([earlier, own, later]) == [earlier, own]
    # One made after the regrade began is on neither side.
    assert before.attempts([newcomer]) == after.attempts([newcomer]) == []


async def test_a_regrade_whose_earlier_publication_no_longer_reads_gives_leaderboards_unread(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands, regrade, [row] = await _regraded(setup, acme, 1, GRADEBOOK)
    await _ended(setup, row, ACCEPTED_EARLY, _result({"r3": "accepted", "r4": "wrong_answer"}))
    as_of = regrades._as_of
    calls: list[str] = []

    async def unread(ctx: Context, current: Any, publication: Any) -> Any:
        # The first read is of the publication the results counted under.
        calls.append(publication)
        return None if len(calls) == 1 else await as_of(ctx, current, publication)

    monkeypatch.setattr(regrades, "_as_of", unread)

    summary = await regrades.run_summary(setup, acme.ada, islands.task, regrade)

    assert summary.unread == 1
    # The leaderboard is not worked out under the new scoring and called the
    # leaderboard before: it says it cannot be compared, as the summary does.
    assert summary.leaderboards is not None
    [leaderboard] = summary.leaderboards
    assert (leaderboard.leaderboard, leaderboard.title, leaderboard.unread, leaderboard.rows) == (
        "gradebook",
        "Gradebook",
        True,
        (),
    )


async def test_a_regrade_that_makes_a_submission_fail_to_compile_lists_its_verdicts(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands, regrade, [row, _] = await _regraded(setup, acme, 2)
    # The late row takes the early compile's verdict for every run.
    await _ended(
        setup,
        row,
        _result({"r1": "compile_error", "r2": "compile_error"}, "compile_error"),
        _result({"r3": "compile_error", "r4": "compile_error"}),
    )

    summary = await regrades.run_summary(setup, acme.ada, islands.task, regrade)

    assert (summary.total, summary.running) == (2, 1)
    [changed] = summary.changed
    assert changed.grading == row.id
    # Every run changed alike, so one line says so.
    assert changed.verdicts == (VerdictChange(None, "accepted", "compile_error", 4),)
    assert ValueChange("total", Fraction(100), Fraction(0)) in changed.values


async def test_a_regrade_counts_what_it_cannot_compare_and_lists_a_bounded_few(
    setup: Setup, acme: Acme, spring: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    islands, regrade, rows = await _regraded(setup, acme, 4)
    wrong = _result({"r3": "accepted", "r4": "wrong_answer"})
    for row in rows:
        await _ended(setup, row, ACCEPTED_EARLY, wrong)
    monkeypatch.setattr(regrades, "CHANGED_SHOWN", 3)

    bounded = await regrades.run_summary(setup, acme.ada, islands.task, regrade)
    as_of = regrades._as_of
    calls: list[str] = []

    async def unread(ctx: Context, current: Any, publication: Any) -> Any:
        # The first read is of the publication the results counted under.
        calls.append(publication)
        return None if len(calls) == 1 else await as_of(ctx, current, publication)

    monkeypatch.setattr(regrades, "_as_of", unread)
    unreadable = await regrades.run_summary(setup, acme.ada, islands.task, regrade)

    assert [each.grading for each in bounded.changed] == [row.id for row in rows[:3]]
    assert (bounded.more_changed, bounded.unread) == (1, 0)
    # Results that counted under a publication that no longer reads are
    # counted, never listed as changing every value.
    assert (unreadable.unread, unreadable.changed, unreadable.more_changed) == (4, (), 0)


async def test_a_task_manager_reads_its_regrades_values_and_an_observer_is_refused(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands, regrade, [row] = await _regraded(setup, acme, 1, GRADEBOOK)
    await _ended(setup, row, ACCEPTED_EARLY, _result({"r3": "accepted", "r4": "wrong_answer"}))

    summary = await regrades.run_summary(
        setup, _task_only(acme, Role.MANAGER), islands.task, regrade
    )

    assert len(summary.changed) == 1 and summary.leaderboards is None
    with pytest.raises(Forbidden):
        await regrades.run_summary(setup, _task_only(acme, Role.OBSERVER), islands.task, regrade)
    with pytest.raises(NotFound):
        await regrades.run_summary(setup, acme.ada, islands.task, uuid.uuid4())
    # A regrade of another task is not found at this one.
    with pytest.raises(NotFound):
        await regrades.run_summary(setup, acme.ada, TaskId("acme/spring/other"), regrade)


async def test_a_save_moving_the_pin_asks_first_and_its_regrade_is_summed_up(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _ended(setup, early, ACCEPTED_EARLY, ACCEPTED_LATE)
    text = await _v2(acme, islands)
    head = await acme.fake.content.list_files(PLATFORM, islands.task)
    edits = {"task.yaml": Edit(text.encode(), head.tokens.get("task.yaml"))}

    with pytest.raises(ConfirmationRequired) as asked:
        await task_save.save(setup, acme.ada, islands.task, edits)

    assert task_save.PIN_MOVED in asked.value.detail
    assert asked.value.extra["regrades"] == 1
    # Only the answer to the question that named the pin confirms it: a
    # bare confirm, or an answer naming the version alone, asks again.
    basis = asked.value.extra["asked"].rsplit(":", 1)[0]
    for answer in (None, basis):
        with pytest.raises(ConfirmationRequired):
            await task_save.save(setup, acme.ada, islands.task, edits, confirm=True, asked=answer)
    saved = await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
    _, save = await _rows(setup, "save")
    await _step(setup, acme, save.id, "save", "diff", _compiled(CHECKER + b"# changed\n"))
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    async with setup.unit_of_work() as ctx:
        made = await ctx.db.scalar(select(Regrade).where(Regrade.task_id == islands.task))
    assert made is not None and made.publication_id == saved.publication
    summary = await regrades.run_summary(setup, acme.ada, islands.task, made.id)
    assert (summary.total, summary.running) == (1, 1)


TAKE_OVER_CASES: list[dict[str, Any]] = [
    {"runs": [{"verdict": "accepted"}, {"verdict": "skipped"}]},
    {"runs": [{"verdict": "accepted"}], "once": {"outcomes": {"build-mean": "accepted"}}},
    {"runs": [{"verdict": "accepted"}], "once": {"outcomes": {"build-mean": "compile_error"}}},
    {"runs": [{"verdict": "wrong_answer"}]},
    {"runs": [{"id": "r1"}]},
    {"runs": [], "checks": [{"passed": True}]},
    {"runs": [], "checks": [{"passed": False}]},
    {"runs": [], "checks": [{"failing": []}]},
    {"runs": [], "checks": [{"passed": "true"}]},
    {"runs": []},
]


async def test_the_take_over_in_sql_is_all_accepted_exactly(
    setup: Setup, acme: Acme, spring: str
) -> None:
    async with setup.unit_of_work() as ctx:
        rows = [
            Grading(
                task_id="acme/spring/islands",
                publication_id=f"acme/spring/islands#{number}",
                pipeline="save",
                status=GradingStatus.DONE,
                result={"schema_version": 6, **case},
            )
            for number, case in enumerate(TAKE_OVER_CASES, start=1)
        ]
        ctx.db.add_all(rows)
        await ctx.db.flush()
        passed = set(await ctx.db.scalars(select(Grading.id).where(PASSED)))

    assert [row.id in passed for row in rows] == [
        step_data.all_accepted(row.result) for row in rows
    ]
    assert [row.id in passed for row in rows] == [
        True,
        True,
        False,
        False,
        False,
        True,
        False,
        False,
        False,
        True,
    ]
