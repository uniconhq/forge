"""A task's steps through their door, and the store of what they did
(SIMPLER-CORE.md 11.6, 11.9): islands graded step by step as a machine
would, each job checked against runner's `job.schema.json`, the solution
compiling the entrant's code in its own batch, early and late; a run an
earlier step did not accept left out, and a step left with nothing to do
answered so while the rest still run; a compile error from the solution's
batch every run's verdict and its log told once, stored or not, and a
malformed `outputs.json`, a NUL in it or a file listed and never put a
`system_error`; the store reusing a step, a checker change building the
checker again and running only the compare steps, and from scratch
reusing nothing and replacing what is reused; an output never reused across orgs or from a
late pipeline into an early one; two gradings making one key keeping
one; a step holding only its own row and the report every step's; and
the door's rules: a token opens only its own step of its own grading,
while that step works, and never reaches a link or a log; uploads only
where a step may write, within its bounds, before it is done; done and
the report answered the same when said again; links short-lived and for
one object; a large task file by its link and no other hash given one;
entrant paths never leaving their folder, and an empty one made by the
job; a file given to a folder port a folder holding it under its name; a
block of box users held while a step runs.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select, update

from forge.db.tables import Grading, GradingStep, StepOutput
from forge.domain import exact_json
from forge.domain.content import Edit
from forge.domain.contracts import violation
from forge.domain.errors import (
    ConfirmationRequired,
    Conflict,
    Forbidden,
    GradingClosed,
    InvalidCallback,
    InvalidToken,
    Misconfigured,
    NotFound,
    Rejected,
    Unavailable,
)
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import PublicationId, TaskId, WorkflowId, WorkspaceId
from forge.domain.names import EntrantOwner
from forge.domain.run_plan import PipelineKind
from forge.domain.step_data import BOX_BLOCK, LINK_LIFE, Fault
from forge.domain.submissions import Submitted
from forge.domain.uploads import pointer_text
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import gradings, steps, task_save
from forge.services.steps import TOKEN_LIFE, StepAccess
from forge.services.task_save import Published
from forge.testing import FakeClock
from tests.conftest import sibling
from tests.services.conftest import SPRING, Acme, make_task, write_contest
from tests.services.test_run_task_saves import DRAFT, ISLANDS_DATA, TASKS

CODE = b"print(1)\n"
CHECKER = b"\x7fELF checker\n"
JOB = json.loads(sibling("runner", "schemas", "job.schema.json").read_text("utf-8"))
JOB_CHECK = Draft202012Validator(JOB, format_checker=FormatChecker())
Written = dict[str, bytes]


@dataclass(frozen=True, slots=True)
class Islands:
    """The islands task published in acme/spring, and its workflow."""

    task: TaskId
    publication: PublicationId
    workflow: WorkflowId


@pytest.fixture
async def islands(setup: Setup, acme: Acme, spring: str) -> Islands:
    return await _islands(setup, acme)


async def _islands(
    setup: Setup,
    acme: Acme,
    *,
    before: Callable[[TaskId], None] | None = None,
    text: str = TASKS["islands"][0],
    contest: str = DRAFT,
    files: Mapping[str, bytes] | None = None,
    workflow_text: Callable[[bytes], bytes] | None = None,
) -> Islands:
    """islands saved as `text`, with `files` beside its own, and published
    over the workflow `unicon/islands@v1` in `contest`, its `workflow.yaml`
    changed by `workflow_text`, `before` called on the task once it is
    made."""
    task = await make_task(setup, acme, "islands")
    _, held, data = TASKS["islands"]
    data = {**data, **(files or {})}
    held = dict(held)
    if workflow_text is not None:
        held["workflow.yaml"] = workflow_text(held["workflow.yaml"])
    workflow = await acme.fake.workflows.create_workflow(
        PLATFORM, "unicon", "islands", held, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, workflow, "v1")
    await write_contest(acme.fake, contest)
    if before is not None:
        before(task)
        data = {
            path: content for path, content in data.items() if path not in _repo(acme, task).files
        }
    published = await _save(
        setup,
        acme,
        task,
        {"task.yaml": text.encode(), "statement.md": b"Count the islands.\n", **data},
    )
    return Islands(task, published.publication, workflow)


async def _save(setup: Setup, acme: Acme, task: TaskId, contents: Mapping[str, bytes]) -> Published:
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {path: Edit(content, head.tokens.get(path)) for path, content in contents.items()}
    try:
        found = await task_save.save(setup, acme.ada, task, edits, confirm=True)
    except ConfirmationRequired as asked:
        found = await task_save.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.extra["asked"]
        )
    assert isinstance(found, Published), found
    return found


def _repo(acme: Acme, task: TaskId) -> Any:
    return acme.fake.state.repos[("acme", f"spring.{task.rsplit('/', 1)[1]}.task")]


async def _submitted(
    acme: Acme, islands: Islands, *, code: bytes = CODE, document: Any = None
) -> tuple[WorkspaceId, Submitted]:
    """A submission of islands by a new entrant, version 6: `code` as its
    one file of `code`, in python."""
    workspaces = acme.fake.workspaces
    workspace = await workspaces.open_workspace(SPRING, EntrantOwner(uuid.uuid4()), [8])
    await workspaces.open_submission_place(workspace, islands.task, [8])
    given = document or {
        "schema_version": 6,
        "inputs": {
            "code": {"files": ["files/code/main.py"]},
            "language": {"value": "python"},
        },
    }
    files = {"submission.json": json.dumps(given).encode(), "files/code/main.py": code}
    made = await workspaces.record_submission(
        PLATFORM, workspace, islands.task, files, key=uuid.uuid4().hex
    )
    return workspace, made


async def _grading(setup: Setup, acme: Acme, islands: Islands, **submitted: Any) -> uuid.UUID:
    """A grading of a new submission of islands, dispatched."""
    workspace, made = await _submitted(acme, islands, **submitted)
    async with setup.unit_of_work() as ctx:
        row = Grading(
            task_id=islands.task,
            workspace_id=workspace,
            submission_id=made.id,
            submission_number=made.number,
            submission_version=made.version,
            submitted_at=made.at,
            publication_id=islands.publication,
            attempt=1,
            status=GradingStatus.DISPATCHED,
            queued_at=ctx.now,
        )
        ctx.db.add(row)
        await ctx.db.flush()
        return row.id


# Playing a machine


def _bearer(access: StepAccess | str) -> str:
    return f"Bearer {access if isinstance(access, str) else access.token}"


def _named(access: StepAccess) -> str:
    """The name the door's URL carries, which is the only one the step knows."""
    return access.data_url.rstrip("/").rsplit("/", 1)[-1]


async def _mint(
    setup: Setup, row: uuid.UUID, pipeline: PipelineKind, step: str, from_scratch: bool = False
) -> StepAccess:
    async with setup.unit_of_work() as ctx:
        found = await gradings.find(ctx, row)
        assert found is not None
        return await steps.mint(ctx, found, pipeline=pipeline, step=step, from_scratch=from_scratch)


async def _job(setup: Setup, row: uuid.UUID, step: str, access: StepAccess | str) -> dict[str, Any]:
    asked = _named(access) if isinstance(access, StepAccess) else step
    built = await steps.step_job(setup, row, asked, _bearer(access))
    assert built is not None
    job: dict[str, Any] = exact_json.loads(built)
    assert list(JOB_CHECK.iter_errors(job)) == []
    return job


async def _finish(
    setup: Setup, acme: Acme, row: uuid.UUID, step: str, access: StepAccess, written: Written
) -> dict[str, str]:
    """Upload what the step wrote, put each file by its link, and say done."""
    listed = [
        {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for path, content in written.items()
    ]
    body = json.dumps({"files": listed}).encode()
    asked = _named(access)
    links = await steps.step_uploads(setup, row, asked, _bearer(access), body)
    for path, link in links["files"].items():
        acme.fake.objects.put(link, written[path])
    await steps.step_done(setup, row, asked, _bearer(access), b"{}")
    return dict(links["files"])


def _runs(job: Mapping[str, Any]) -> list[str]:
    return [entry["run"] for entry in job["inputs"]["batch"]]


async def _step(
    setup: Setup,
    acme: Acme,
    row: uuid.UUID,
    pipeline: PipelineKind,
    step: str,
    write: Callable[[dict[str, Any]], Written],
    from_scratch: bool = False,
) -> dict[str, Any]:
    """One step run as its wrapper runs it: its job, then what `write`
    makes of the job uploaded, then done."""
    access = await _mint(setup, row, pipeline, step, from_scratch)
    job = await _job(setup, row, step, access)
    await _finish(setup, acme, row, step, access, write(job))
    return job


async def _report(setup: Setup, row: uuid.UUID, pipeline: PipelineKind) -> GradingStatus:
    access = await _mint(setup, row, pipeline, "report")
    return await steps.step_report(setup, row, "report", _bearer(access), b"{}")


def _outputs(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document).encode()


def _built(outcome: str = "accepted", log: str = "built") -> Callable[..., Written]:
    """A build at the save: whether its source compiled, and the log."""

    def write(job: Mapping[str, Any]) -> Written:
        outputs = {"outcome": outcome, "log": log}
        return {"outputs.json": _outputs({"schema_version": 6, "outputs": outputs})}

    return write


def _batch(
    outcome: Mapping[str, str] | None = None, *, score: bool = False, log: str = "built"
) -> Callable[..., Written]:
    """A batch of exec runs, each accepted but those `outcome` names, each
    giving the compile `log`."""

    def write(job: Mapping[str, Any]) -> Written:
        entries: list[dict[str, Any]] = []
        files: Written = {}
        for run in _runs(job):
            outputs: dict[str, Any] = {"outcome": (outcome or {}).get(run, "accepted"), "log": log}
            if outputs["outcome"] == "accepted":
                outputs |= {"output": {"file": f"out/{run}/output"}, "memory_kb": 1500}
                outputs |= {"time_ms": 12} | ({"score": 1, "message": "Right."} if score else {})
                files[f"out/{run}/output"] = f"{run}\n".encode()
            entries.append({"run": run, "outputs": outputs})
        return {"outputs.json": _outputs({"schema_version": 6, "batch": entries}), **files}

    return write


def _other_output(job: Mapping[str, Any]) -> Written:
    """A batch of exec runs, each accepted with an output other than
    `_batch`'s."""
    written = _batch()(job)
    return {
        path: content + b"other\n" if path.startswith("out/") else content
        for path, content in written.items()
    }


async def _saved(setup: Setup, acme: Acme, islands: Islands) -> uuid.UUID:
    """The save pipeline's build of islands' checker run and its grading
    reported."""
    row = await _grading(setup, acme, islands)
    await _step(setup, acme, row, "save", "build-compare", _built())
    assert await _report(setup, row, "save") is GradingStatus.DONE
    return row


async def _early(
    setup: Setup, acme: Acme, row: uuid.UUID, solution: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """The early pipeline run to its report; each step's job."""
    jobs = {"solution": await _step(setup, acme, row, "early", "solution", _batch(solution))}
    jobs["compare"] = await _step(setup, acme, row, "early", "compare", _batch(score=True))
    await _report(setup, row, "early")
    return jobs


async def _row(setup: Setup, row: uuid.UUID) -> Grading:
    async with setup.unit_of_work() as ctx:
        found = await gradings.find(ctx, row)
        assert found is not None
        return found


async def _stored(setup: Setup, **where: Any) -> list[StepOutput]:
    async with setup.unit_of_work() as ctx:
        query = select(StepOutput).filter_by(**where).order_by(StepOutput.measured_at)
        return list((await ctx.db.execute(query)).scalars())


async def _to_run(
    setup: Setup, row: uuid.UUID, pipeline: PipelineKind, from_scratch: bool = False
) -> list[tuple[str, list[str]]]:
    async with setup.unit_of_work() as ctx:
        found = await gradings.find(ctx, row)
        assert found is not None
        left = await steps.pipeline_to_run(ctx, found, pipeline, from_scratch=from_scratch)
        return [(held.name, list(held.depends_on)) for held in left]


def _download(job: Mapping[str, Any], path: str) -> dict[str, Any]:
    return next(entry for entry in job["files"] if entry["path"] == path)


# Islands, step by step


async def test_islands_is_graded_through_the_door_step_by_step(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    saved = await _grading(setup, acme, islands)
    build = await _step(setup, acme, saved, "save", "build-compare", _built())
    assert await _report(setup, saved, "save") is GradingStatus.DONE
    row = await _grading(setup, acme, islands)

    solution = await _step(setup, acme, row, "early", "solution", _batch())
    access = await _mint(setup, row, "early", "compare")
    compare_job = await _job(setup, row, "compare", access)
    compare = compare_job["inputs"]["batch"][0]["inputs"]
    manifest = _download(compare_job, compare["manifest"]["file"])
    sha256 = manifest["url"].rpartition("/files/")[2]
    served = await steps.step_file(setup, row, _named(access), sha256, _bearer(access))
    await _finish(setup, acme, row, "compare", access, _batch(score=True)(compare_job))
    await _report(setup, row, "early")

    assert violation(build["inputs"], "primitive", "inputs_file") is None
    assert _runs(solution) == ["r1", "r2"] and solution["fail_on_non_accepted"] is False
    assert "secrets" not in solution
    users = solution["box_users"]
    assert users["last"] - users["first"] == BOX_BLOCK - 1
    async with setup.unit_of_work() as ctx:
        plan, _ = await steps.plan_of(ctx, await _row(setup, row))
    [planned] = [each for each in plan.pipeline("early") if each.name == "solution"]
    assert solution["step_memory_mb"] == planned.limits.memory_mb
    first = solution["inputs"]["batch"][0]["inputs"]
    stdin = _download(solution, first["stdin"]["file"])
    assert stdin["cached"] is True and stdin["path"].startswith("cache/")
    assert first["language"] == "python"
    source = _download(solution, first["source"]["folder"])
    assert source == {"path": first["source"]["folder"], "workspace": "files/code"}
    assert set(compare["files"]["files"]) == {"answer", "output"}
    answer = _download(compare_job, compare["files"]["files"]["answer"]["file"])
    assert answer["path"].endswith("/answer") and answer["cached"] is True
    assert manifest["url"] == f"{access.data_url}/files/{sha256}"
    assert hashlib.sha256(served).hexdigest() == sha256 and "reports" in json.loads(served)
    graded = await _row(setup, row)
    assert graded.status == GradingStatus.DONE and graded.error is None
    result = graded.result
    assert result is not None and violation(result, "result", "result_v6") is None
    assert [(run["id"], run["verdict"], run["passed"]) for run in result["runs"]] == [
        ("r1", "accepted", 1),
        ("r2", "accepted", 1),
    ]
    assert result["runs"][0]["reports"] == {"memory_kb": 1500, "score": 1, "time_ms": 12}
    [log] = await _stored(setup, grading_id=row, step="solution", run="r1")
    assert result["once"] == {
        "reports": {},
        "texts": {"log": {"output": str(log.id), "name": "log"}},
        "outcomes": {"solution": "accepted", "compare": "accepted"},
    }
    assert "log" not in result["runs"][0]["texts"]
    assert result["runs"][0]["message"]["name"] == "message"
    assert len(await _stored(setup, grading_id=row)) == 4


async def test_a_late_grading_does_only_the_late_runs_compiling_the_code_again(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    await _early(setup, acme, await _grading(setup, acme, islands))
    late = await _grading(setup, acme, islands)

    assert await _to_run(setup, late, "late") == [("solution", []), ("compare", ["solution"])]
    solution = await _step(setup, acme, late, "late", "solution", _batch())
    assert _runs(solution) == ["r3", "r4"]
    first = solution["inputs"]["batch"][0]["inputs"]
    assert _download(solution, first["source"]["folder"])["workspace"] == "files/code"
    with pytest.raises(Conflict):
        await _mint(setup, late, "early", "solution")
    await _step(setup, acme, late, "late", "compare", _batch(score=True))
    await _report(setup, late, "late")

    result = (await _row(setup, late)).result
    assert result is not None
    assert [(run["id"], run["verdict"]) for run in result["runs"]] == [
        ("r3", "accepted"),
        ("r4", "accepted"),
    ]
    assert result["once"]["outcomes"] == {"solution": "accepted", "compare": "accepted"}
    assert set(result["once"]["texts"]) == {"log"}


async def test_a_run_an_earlier_step_did_not_accept_is_left_out(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)

    jobs = await _early(setup, acme, row, solution={"r1": "wrong_answer"})

    assert _runs(jobs["compare"]) == ["r2"]
    graded = await _row(setup, row)
    assert graded.status == GradingStatus.DONE and graded.result is not None
    assert [(run["id"], run["verdict"], run["passed"]) for run in graded.result["runs"]] == [
        ("r1", "wrong_answer", 0),
        ("r2", "accepted", 1),
    ]


NOT_COMPILED = {"r1": "compile_error", "r2": "compile_error"}
"""What the solution's batch gives when the entrant's code does not compile:
every item the same compile error."""


async def test_a_compile_error_from_the_solutions_batch_is_every_runs_verdict_told_once(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    await _step(setup, acme, row, "early", "solution", _batch(NOT_COMPILED, log="main.py:1: no"))

    access = await _mint(setup, row, "early", "compare")
    assert await steps.step_job(setup, row, _named(access), _bearer(access)) is None
    assert await _report(setup, row, "early") is GradingStatus.DONE

    result = (await _row(setup, row)).result
    assert result is not None and result["error"] is None
    assert {run["verdict"] for run in result["runs"]} == {"compile_error"}
    assert result["once"]["outcomes"] == {"solution": "compile_error"}
    assert set(result["once"]["texts"]) == {"log"}
    assert all("log" not in run["texts"] for run in result["runs"])


async def test_a_stored_compile_error_leaves_no_step_to_run(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    first = await _grading(setup, acme, islands)
    await _step(setup, acme, first, "early", "solution", _batch(NOT_COMPILED))
    await _report(setup, first, "early")
    again = await _grading(setup, acme, islands)

    assert await _to_run(setup, again, "early") == []
    async with setup.unit_of_work() as ctx:
        row = await gradings.find(ctx, again)
        assert row is not None
        await steps.conclude(ctx, row, "early")
    result = (await _row(setup, again)).result
    assert result is not None and result["error"] is None
    assert {run["verdict"] for run in result["runs"]} == {"compile_error"}


async def test_a_step_another_grading_did_first_has_nothing_to_do_and_the_rest_run(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    late, early = await _grading(setup, acme, islands), await _grading(setup, acme, islands)
    assert [name for name, _ in await _to_run(setup, late, "early")] == ["solution", "compare"]
    await _step(setup, acme, early, "early", "solution", _batch())

    access = await _mint(setup, late, "early", "solution")
    assert await steps.step_job(setup, late, _named(access), _bearer(access)) is None
    assert await steps.step_job(setup, late, _named(access), _bearer(access)) is None
    await _step(setup, acme, late, "early", "compare", _batch(score=True))

    assert await _report(setup, late, "early") is GradingStatus.DONE
    result = (await _row(setup, late)).result
    assert result is not None
    assert [run["verdict"] for run in result["runs"]] == ["accepted", "accepted"]


async def test_a_malformed_outputs_json_is_a_system_error_and_kept_nowhere(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)

    await _step(setup, acme, row, "early", "solution", lambda job: {"outputs.json": b"{not json"})
    assert await _report(setup, row, "early") is GradingStatus.SYSTEM_ERROR

    graded = await _row(setup, row)
    assert graded.error == "The step solution faulted: outputs.json is not JSON."
    assert graded.result is not None and graded.result["error"] == graded.error
    assert {run["verdict"] for run in graded.result["runs"]} == {"system_error"}
    assert await _stored(setup, grading_id=row, step="solution") == []


async def test_an_error_the_step_wrote_is_a_system_error(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    failed = {"outputs.json": _outputs({"schema_version": 6, "error": "No isolate."})}

    await _step(setup, acme, row, "early", "solution", lambda job: failed)
    await _report(setup, row, "early")

    graded = await _row(setup, row)
    assert graded.status == GradingStatus.SYSTEM_ERROR
    assert graded.error == "The step solution faulted: The step failed: No isolate."


# The store


async def test_the_store_reuses_every_step_and_from_scratch_reuses_none_and_replaces_it(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    first = await _grading(setup, acme, islands)
    await _early(setup, acme, first)
    again = await _grading(setup, acme, islands)

    assert await _to_run(setup, again, "early") == []
    async with setup.unit_of_work() as ctx:
        row = await gradings.find(ctx, again)
        assert row is not None
        await steps.conclude(ctx, row, "early")
    reused = await _row(setup, again)
    assert reused.status == GradingStatus.DONE and reused.result is not None
    assert reused.result["runs"] == (await _row(setup, first)).result["runs"]  # type: ignore[index]

    scratch = await _grading(setup, acme, islands)
    assert [name for name, _ in await _to_run(setup, scratch, "early", True)] == [
        "solution",
        "compare",
    ]
    job = await _step(setup, acme, scratch, "early", "solution", _other_output, True)
    assert _runs(job) == ["r1", "r2"]
    assert job["inputs"]["batch"][0]["inputs"]["language"] == "python"
    mine = await _stored(setup, grading_id=scratch, step="solution")
    before = await _stored(setup, grading_id=first, step="solution")
    assert [each.reusable for each in mine] == [True, True]
    assert [each.reusable for each in before] == [False, False]

    later = await _grading(setup, acme, islands)
    assert await _to_run(setup, later, "early") == [("compare", [])]
    compare = await _job(setup, later, "compare", await _mint(setup, later, "early", "compare"))
    output = compare["inputs"]["batch"][0]["inputs"]["files"]["files"]["output"]["file"]
    assert acme.fake.objects.get(_download(compare, output)["url"]) == b"r1\nother\n"


async def test_a_checker_change_builds_it_again_and_runs_only_the_compare_steps(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    await _early(setup, acme, await _grading(setup, acme, islands))
    _, draft = await acme.fake.workflows.read_workflow_draft(
        PLATFORM, islands.workflow, "diff/diff.py"
    )
    await acme.fake.workflows.write_workflow_file(
        PLATFORM,
        islands.workflow,
        "diff/diff.py",
        draft.content + b"# stricter\n",
        expected=draft.token,
        message="Stricter",
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, islands.workflow, "v2")
    text = TASKS["islands"][0].replace("unicon/islands@v1", "unicon/islands@v2")
    published = await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
    changed = Islands(islands.task, published.publication, islands.workflow)

    save = await _grading(setup, acme, changed)
    assert await _to_run(setup, save, "save") == [("build-compare", [])]
    await _step(setup, acme, save, "save", "build-compare", _built())
    assert await _report(setup, save, "save") is GradingStatus.DONE
    regrade = await _grading(setup, acme, changed)

    assert await _to_run(setup, regrade, "early") == [("compare", [])]
    compare = await _step(setup, acme, regrade, "early", "compare", _batch(score=True))
    assert _runs(compare) == ["r1", "r2"]


async def test_an_output_is_never_reused_by_another_org_or_from_a_late_pipeline(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    first = await _grading(setup, acme, islands)
    await _step(setup, acme, first, "early", "solution", _batch())

    for changes in ({"org_id": "beta"}, {"org_id": "acme", "pipeline": "late"}):
        async with setup.unit_of_work() as ctx:
            await ctx.db.execute(
                update(StepOutput)
                .where(StepOutput.grading_id == first, StepOutput.step == "solution")
                .values(**changes)
            )
        row = await _grading(setup, acme, islands)
        assert (await _to_run(setup, row, "early"))[0][0] == "solution"
        job = await _job(setup, row, "solution", await _mint(setup, row, "early", "solution"))
        assert _runs(job) == ["r1", "r2"]


async def test_two_gradings_making_one_key_at_once_keep_one(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    rows = [await _grading(setup, acme, islands) for _ in range(2)]
    access = [await _mint(setup, row, "early", "solution") for row in rows]
    jobs = [
        await _job(setup, row, "solution", each) for row, each in zip(rows, access, strict=True)
    ]

    for row, each, job in zip(rows, access, jobs, strict=True):
        await _finish(setup, acme, row, "solution", each, _batch()(job))

    found = await _stored(setup, step="solution")
    kept: dict[str, list[bool]] = {}
    for output in found:
        kept.setdefault(output.key, []).append(output.reusable)
    assert list(kept.values()) == [[True, False], [True, False]]
    assert {row.grading_id for row in found} == set(rows)


async def test_a_step_holds_only_its_own_row_and_the_report_holds_every_steps(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    report = await _mint(setup, row, "early", "report")

    async with setup.unit_of_work() as ctx:
        assert await gradings.find(ctx, row, lock=True) is not None
        job = await asyncio.wait_for(steps.step_job(setup, row, _named(access), _bearer(access)), 5)
        assert job is not None
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            select(GradingStep)
            .where(GradingStep.grading_id == row, GradingStep.step == "solution")
            .with_for_update()
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                steps.step_report(setup, row, "report", _bearer(report), b"{}"), 1
            )
    assert (await _row(setup, row)).status == GradingStatus.DISPATCHED


# The door's rules


async def test_a_token_opens_only_its_own_step_of_its_own_grading(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row, other = await _grading(setup, acme, islands), await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    compare = await _mint(setup, row, "early", "compare")
    other_solution = await _mint(setup, other, "early", "solution")

    for grading, step, header in (
        (row, _named(compare), _bearer(access)),
        (other, _named(other_solution), _bearer(access)),
        (row, _named(access), None),
        (row, _named(access), f"Basic {access.token}"),
        (row, _named(access), _bearer(access.token[:-1])),
        (row, "report", _bearer(access)),
    ):
        with pytest.raises(InvalidToken):
            await steps.step_job(setup, grading, step, header)
    with pytest.raises(NotFound):
        await _mint(setup, row, "early", "compile")
    assert access.data_url.endswith(f"/api/v1/gradings/{row}/steps/s1")
    assert compare.data_url.endswith(f"/api/v1/gradings/{row}/steps/s2")


async def test_the_door_finds_a_step_by_its_woodpecker_name_and_an_older_row_by_its_own(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")

    assert await steps.step_job(setup, row, "s1", _bearer(access)) is not None
    with pytest.raises(InvalidToken):
        await steps.step_job(setup, row, "solution", _bearer(access))

    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(GradingStep)
            .where(GradingStep.grading_id == row, GradingStep.step == "solution")
            .values(woodpecker=None)
        )
    assert await steps.step_job(setup, row, "solution", _bearer(access)) is not None
    with pytest.raises(InvalidToken):
        await steps.step_job(setup, row, "s1", _bearer(access))
    again = await _mint(setup, row, "early", "solution")
    assert again.data_url.endswith("/steps/solution") and again.token == access.token


async def test_a_token_is_refused_once_its_step_is_done_or_it_has_expired(
    setup: Setup, acme: Acme, islands: Islands, clock: FakeClock
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    job = await _job(setup, row, "solution", access)
    links = await _finish(setup, acme, row, "solution", access, _batch()(job))

    with pytest.raises(InvalidToken):
        await steps.step_job(setup, row, _named(access), _bearer(access))
    with pytest.raises(InvalidToken):
        await steps.step_uploads(setup, row, _named(access), _bearer(access), b'{"files": []}')
    with pytest.raises(GradingClosed):
        await _mint(setup, row, "early", "solution")
    assert links

    compare = await _mint(setup, row, "early", "compare")
    clock.advance(TOKEN_LIFE)
    with pytest.raises(InvalidToken):
        await steps.step_job(setup, row, _named(compare), _bearer(compare))
    with pytest.raises(InvalidToken):
        await steps.step_done(setup, row, _named(compare), _bearer(compare), b"{}")


async def test_a_finished_grading_takes_no_more_work(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    async with setup.unit_of_work() as ctx:
        found = await gradings.find(ctx, row, lock=True)
        assert found is not None
        gradings.finish(ctx, found, GradingStatus.CANCELLED)

    with pytest.raises(GradingClosed):
        await steps.step_job(setup, row, _named(access), _bearer(access))
    with pytest.raises(GradingClosed):
        await _mint(setup, row, "early", "compare")


async def test_done_and_the_report_answer_the_same_when_said_again(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    job = await _job(setup, row, "solution", access)
    await _finish(setup, acme, row, "solution", access, _batch()(job))

    await steps.step_done(setup, row, _named(access), _bearer(access), b"{}")
    assert len(await _stored(setup, grading_id=row)) == 2
    report = await _mint(setup, row, "early", "report")
    first = await steps.step_report(setup, row, "report", _bearer(report), b"{}")
    result = (await _row(setup, row)).result

    again = await steps.step_report(setup, row, "report", _bearer(report), b"{}")
    await steps.step_done(setup, row, _named(access), _bearer(access), b"{}")

    assert first is again is GradingStatus.SYSTEM_ERROR
    assert (await _row(setup, row)).result == result
    with pytest.raises(InvalidToken):
        await steps.step_report(setup, row, "report", _bearer(access), b"{}")


@pytest.mark.parametrize(
    "path",
    [
        "../outputs.json",
        "out/../outputs.json",
        "out/./x",
        "out//x",
        "out/",
        "/out/x",
        "in/x",
        "cache/x/y",
        "out/a\nb",
        "outputs.json/x",
        "results/x",
    ],
)
async def test_a_step_uploads_only_where_it_may_write(
    setup: Setup, acme: Acme, islands: Islands, path: str
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    await _job(setup, row, "solution", access)
    listed = [
        {"path": "outputs.json", "sha256": "0" * 64, "size": 2},
        {"path": path, "sha256": "0" * 64, "size": 1},
    ]

    with pytest.raises(InvalidCallback):
        await steps.step_uploads(
            setup, row, _named(access), _bearer(access), json.dumps({"files": listed}).encode()
        )


async def test_uploads_are_bounded_listed_after_the_job_and_put_under_the_step(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    one = {"path": "outputs.json", "sha256": "0" * 64, "size": 2}
    body = json.dumps({"files": [one]}).encode()

    with pytest.raises(InvalidCallback):
        await steps.step_uploads(setup, row, _named(access), _bearer(access), body)
    await _job(setup, row, "solution", access)
    over = {"path": "out/r1/output", "sha256": "0" * 64, "size": 64 * 1024 * 1024 + 1}
    with pytest.raises(InvalidCallback):
        await steps.step_uploads(
            setup, row, _named(access), _bearer(access), json.dumps({"files": [one, over]}).encode()
        )
    with pytest.raises(InvalidCallback):
        await steps.step_done(setup, row, _named(access), _bearer(access), b"{}")
    links = await steps.step_uploads(setup, row, _named(access), _bearer(access), body)

    assert (
        f"/unicon-results/results/{row}/steps/solution/outputs.json?"
        in links["files"]["outputs.json"]
    )


@pytest.mark.parametrize(
    ("put", "fault"),
    [
        ({"out/r1/output", "out/r2/output"}, "outputs.json was not uploaded."),
        ({"outputs.json", "out/r1/output"}, "out/r2/output was not uploaded."),
    ],
)
async def test_a_file_listed_and_not_put_is_a_fault_and_a_put_writes_only_what_was_listed(
    setup: Setup, acme: Acme, islands: Islands, put: set[str], fault: str
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    written = _batch()(await _job(setup, row, "solution", access))
    listed = [
        {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for path, content in written.items()
    ]
    links = await steps.step_uploads(
        setup, row, _named(access), _bearer(access), json.dumps({"files": listed}).encode()
    )
    for path in written:
        with pytest.raises(Rejected):
            acme.fake.objects.put(links["files"][path], written[path] + b" ")
    for path in put:
        acme.fake.objects.put(links["files"][path], written[path])

    await steps.step_done(setup, row, _named(access), _bearer(access), b"{}")

    async with setup.unit_of_work() as ctx:
        held = await ctx.db.get(GradingStep, (row, "solution"))
        assert held is not None
    assert held.fault == fault
    assert await _stored(setup, grading_id=row) == []


async def test_a_nul_in_outputs_json_is_a_fault(setup: Setup, acme: Acme, islands: Islands) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    await _step(setup, acme, row, "early", "solution", _batch(NOT_COMPILED, log="bad \x00 byte"))
    assert await _report(setup, row, "early") is GradingStatus.SYSTEM_ERROR

    graded = await _row(setup, row)
    assert graded.error == "The step solution faulted: outputs.json holds a NUL character."
    assert await _stored(setup, grading_id=row) == []


async def test_a_fault_is_kept_without_a_nul(
    setup: Setup, acme: Acme, islands: Islands, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any) -> None:
        raise Fault("bad \x00 byte")

    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    monkeypatch.setattr(steps, "read_outputs", refuse)

    await _step(setup, acme, row, "early", "solution", _batch())

    async with setup.unit_of_work() as ctx:
        held = await ctx.db.get(GradingStep, (row, "solution"))
        assert held is not None
    assert held.fault == "bad  byte"


async def test_the_token_never_reaches_a_link_or_a_log(
    setup: Setup, acme: Acme, islands: Islands, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    tokens: list[str] = []
    jobs: list[dict[str, Any]] = []
    for step, write in (("solution", _batch()), ("compare", _batch(score=True))):
        access = await _mint(setup, row, "early", step)
        tokens.append(access.token)
        jobs.append(await _job(setup, row, step, access))
        links = await _finish(setup, acme, row, step, access, write(jobs[-1]))
        jobs.append(links)
    with pytest.raises(InvalidToken):
        await steps.step_job(setup, row, "s1", _bearer(tokens[0]))

    logged = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    for token in tokens:
        assert token not in json.dumps(jobs, default=str)
        assert token not in logged


async def test_a_link_lives_a_short_while_and_reads_one_object(
    setup: Setup, acme: Acme, islands: Islands, clock: FakeClock
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    await _step(setup, acme, row, "early", "solution", _batch())
    compare = await _job(setup, row, "compare", await _mint(setup, row, "early", "compare"))
    output = compare["inputs"]["batch"][0]["inputs"]["files"]["files"]["output"]["file"]
    link = _download(compare, output)["url"]

    assert f"/unicon-results/results/{row}/steps/solution/out/r1/output?" in link
    assert acme.fake.objects.get(link) == b"r1\n"
    with pytest.raises(Forbidden):
        acme.fake.objects.put(link, b"replaced")
    clock.advance(LINK_LIFE + timedelta(seconds=1))
    with pytest.raises(Forbidden):
        acme.fake.objects.get(link)


async def test_a_large_task_file_is_read_by_its_link_and_no_other_hash_gets_one(
    setup: Setup, acme: Acme, spring: str
) -> None:
    content = b"3\n" * 1000
    digest = hashlib.sha256(content).hexdigest()
    acme.fake.objects.lfs[digest] = content

    def upload(task: TaskId) -> None:
        pointer = pointer_text(digest, len(content))
        acme.fake.state.commit(_repo(acme, task), {"tests/3.in": pointer}, "Upload", None)

    islands = await _islands(setup, acme, before=upload)
    await _saved(setup, acme, islands)
    await _early(setup, acme, await _grading(setup, acme, islands))
    late = await _grading(setup, acme, islands)
    access = await _mint(setup, late, "late", "solution")
    job = await _job(setup, late, "solution", access)

    large = _download(job, job["inputs"]["batch"][0]["inputs"]["stdin"]["file"])
    assert (large["sha256"], large["size"]) == (digest, len(content))
    assert "/forgejo-lfs/lfs/" in large["url"] and acme.fake.objects.get(large["url"]) == content
    small = _download(job, job["inputs"]["batch"][1]["inputs"]["stdin"]["file"])
    sha256 = hashlib.sha256(ISLANDS_DATA["tests/4.in"]).hexdigest()
    assert small["url"] == f"{access.data_url}/files/{sha256}"
    assert await steps.step_file(setup, late, _named(access), sha256, _bearer(access)) == b"4\n"
    for unlisted in (digest, hashlib.sha256(ISLANDS_DATA["tests/3.ans"]).hexdigest()):
        with pytest.raises(NotFound):
            await steps.step_file(setup, late, _named(access), unlisted, _bearer(access))


@pytest.mark.parametrize(
    "files",
    [
        ["files/code/../../submission.json"],
        ["files/code/./main.py"],
        ["files/other/main.py"],
        ["files/code"],
        "files/code/main.py",
    ],
)
async def test_an_entrants_paths_never_leave_their_folder(
    setup: Setup, acme: Acme, islands: Islands, files: Any
) -> None:
    await _saved(setup, acme, islands)
    document = {
        "schema_version": 6,
        "inputs": {"code": {"files": files}, "language": {"value": "python"}},
    }
    row = await _grading(setup, acme, islands, document=document)
    access = await _mint(setup, row, "early", "solution")

    with pytest.raises(Misconfigured):
        await steps.step_job(setup, row, _named(access), _bearer(access))


async def test_a_boxed_step_holds_a_block_of_box_users_while_it_runs(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    rows = [await _grading(setup, acme, islands, code=f"print({n})".encode()) for n in range(2)]
    access = [await _mint(setup, row, "early", "solution") for row in rows]
    jobs = [
        await _job(setup, row, "solution", each) for row, each in zip(rows, access, strict=True)
    ]

    blocks = [range(job["box_users"]["first"], job["box_users"]["last"] + 1) for job in jobs]
    assert not set(blocks[0]) & set(blocks[1])
    assert (await _job(setup, rows[0], "solution", access[0]))["box_users"] == jobs[0]["box_users"]
    await _finish(setup, acme, rows[0], "solution", access[0], _batch()(jobs[0]))
    async with setup.unit_of_work() as ctx:
        held = await ctx.db.get(GradingStep, (rows[0], "solution"))
        assert held is not None and held.box_first is None


DUEL = """\
inputs:
  code: folder
  grid: file
steps:
  - id: game
    use: unicon/exec@v2
    with:
      programs:
        first:
          source: ${{ inputs.code }}
          language: python
          files: {grid: "${{ inputs.grid }}"}
          time_limit: 1
          memory_limit: 64
        second:
          source: ${{ inputs.code }}
          language: python
          files: {grid: "${{ inputs.grid }}"}
          time_limit: 1
          memory_limit: 64
      links: [first -> second, second -> first]
outputs:
  time: {from: "${{ steps.game.first.time_ms }}", unit: ms}
"""
DUEL_TASK = """\
name: Duel
workflow: unicon/duel@v1
inputs:
  code: entrant
  grid: grids/1.txt
scoring:
  values:
    time: {block: max, over: {runs: all}, of: {run: [all, time]}}
  show: {now: [time, all, grids/]}
"""


async def test_a_programs_forms_cached_files_are_each_in_its_own_item(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task = await make_task(setup, acme, "duel")
    workflow = await acme.fake.workflows.create_workflow(
        PLATFORM, "unicon", "duel", {"workflow.yaml": DUEL.encode()}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, workflow, "v1")
    await write_contest(acme.fake, DRAFT)
    contents = {"task.yaml": DUEL_TASK.encode(), "grids/1.txt": b"3 3\n"}
    duel = Islands(task, (await _save(setup, acme, task, contents)).publication, workflow)
    document = {"schema_version": 6, "inputs": {"code": {"files": ["files/code/main.py"]}}}
    row = await _grading(setup, acme, duel, document=document)

    access = await _mint(setup, row, "early", "game")
    job = await _job(setup, row, "game", access)

    programs = job["inputs"]["inputs"]["programs"]["programs"]
    grids = [programs[name]["files"]["files"]["grid"]["file"] for name in ("first", "second")]
    items = {grid.split("/")[1] for grid in grids}
    assert len(items) == 2 and all(grid.endswith("/grid") for grid in grids)
    assert all(_download(job, grid)["cached"] is True for grid in grids)
    assert job["box_users"]["last"] - job["box_users"]["first"] == BOX_BLOCK - 1
    measured: dict[str, Any] = {"time_ms": 3, "memory_kb": 1, "ended": "finished", "ended_at": 3}
    outputs: dict[str, Any] = {"outcome": "accepted", "facts": {"file": "out/facts"}}
    for name in ("first", "second"):
        outputs |= {f"{name}.{output}": value for output, value in measured.items()}
        outputs[f"{name}.log"] = "built"
    written = {"outputs.json": _outputs({"schema_version": 6, "outputs": outputs})}
    await _finish(setup, acme, row, "game", access, written | {"out/facts": b"{}"})
    assert await _report(setup, row, "early") is GradingStatus.DONE
    result = (await _row(setup, row)).result
    assert result is not None and result["once"]["reports"] == {"time": 3}
    assert [(run["id"], run["verdict"]) for run in result["runs"]] == [("r1", "accepted")]


async def test_an_entrants_folder_with_no_file_is_made_empty_by_the_job(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    document = {
        "schema_version": 6,
        "inputs": {"code": {"files": []}, "language": {"value": "python"}},
    }
    row = await _grading(setup, acme, islands, document=document)

    job = await _job(setup, row, "solution", await _mint(setup, row, "early", "solution"))

    source = job["inputs"]["batch"][0]["inputs"]["source"]["folder"]
    assert {"path": source, "folder": True} in job["files"]
    assert not [entry for entry in job["files"] if entry["path"].startswith(f"{source}/")]


async def test_a_file_given_to_a_folder_port_is_a_folder_holding_it_under_its_name(
    setup: Setup, acme: Acme, spring: str
) -> None:
    code_file = await _islands(
        setup,
        acme,
        workflow_text=lambda text: text.replace(b"code: {type: folder", b"code: {type: file"),
    )
    await _saved(setup, acme, code_file)
    row = await _grading(setup, acme, code_file)

    job = await _job(setup, row, "solution", await _mint(setup, row, "early", "solution"))

    source = job["inputs"]["batch"][0]["inputs"]["source"]["folder"]
    held = [entry for entry in job["files"] if entry["path"].startswith(f"{source}/")]
    assert [entry["path"] for entry in held] == [f"{source}/main.py"]


@pytest.mark.parametrize(
    ("replaced", "later_runs"),
    [
        (_batch(NOT_COMPILED), []),
        (_other_output, ["compare"]),
    ],
    ids=["a compile error", "another output"],
)
async def test_a_grading_under_way_keeps_what_it_reused_when_from_scratch_replaces_it(
    setup: Setup,
    acme: Acme,
    islands: Islands,
    replaced: Callable[..., Written],
    later_runs: list[str],
) -> None:
    await _saved(setup, acme, islands)
    await _step(setup, acme, await _grading(setup, acme, islands), "early", "solution", _batch())
    under_way = await _grading(setup, acme, islands)
    access = await _mint(setup, under_way, "early", "solution")
    assert await steps.step_job(setup, under_way, _named(access), _bearer(access)) is None

    scratch = await _grading(setup, acme, islands)
    await _step(setup, acme, scratch, "early", "solution", replaced, True)
    compare = await _step(setup, acme, under_way, "early", "compare", _batch(score=True))

    assert _runs(compare) == ["r1", "r2"]
    assert await _report(setup, under_way, "early") is GradingStatus.DONE
    result = (await _row(setup, under_way)).result
    assert result is not None
    assert [run["verdict"] for run in result["runs"]] == ["accepted", "accepted"]
    later = await _grading(setup, acme, islands)
    assert [name for name, _ in await _to_run(setup, later, "early")] == later_runs


async def test_an_earlier_step_that_gave_nothing_is_a_fault_not_nothing_to_do(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "compare")

    with pytest.raises(Misconfigured, match="The step compare reads a step"):
        await steps.step_job(setup, row, _named(access), _bearer(access))


async def test_a_step_keeps_to_the_first_upload_list_it_sent(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    written = _batch()(await _job(setup, row, "solution", access))

    def listing(files: Written) -> bytes:
        return json.dumps(
            {
                "files": [
                    {
                        "path": path,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                    for path, content in files.items()
                ]
            }
        ).encode()

    first = await steps.step_uploads(setup, row, _named(access), _bearer(access), listing(written))
    again = await steps.step_uploads(setup, row, _named(access), _bearer(access), listing(written))
    tampered = {**written, "out/r1/output": b"tampered\n"}
    with pytest.raises(InvalidCallback, match="listed other uploads"):
        await steps.step_uploads(setup, row, _named(access), _bearer(access), listing(tampered))

    assert set(first["files"]) == set(again["files"])
    for path, link in again["files"].items():
        acme.fake.objects.put(link, written[path])
    await steps.step_done(setup, row, _named(access), _bearer(access), b"{}")
    with pytest.raises(Rejected):
        acme.fake.objects.put(first["files"]["out/r1/output"], tampered["out/r1/output"])
    [kept] = await _stored(setup, grading_id=row, step="solution", run="r1")
    assert kept.files["out/r1/output"]["sha256"] == hashlib.sha256(b"r1\n").hexdigest()


@pytest.mark.parametrize("name", ["a\x00b", "../solution", "Solution", "solution.r1/x", "x" * 200])
async def test_a_step_name_no_plan_gives_is_refused_before_anything_is_read(
    setup: Setup, acme: Acme, islands: Islands, name: str
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    header = _bearer("anything")

    for call in (
        steps.step_job(setup, row, name, header),
        steps.step_admits(setup, row, name, header),
        steps.step_uploads(setup, row, name, header, b"{}"),
        steps.step_done(setup, row, name, header, b"{}"),
        steps.step_file(setup, row, name, "0" * 64, header),
    ):
        with pytest.raises(InvalidToken):
            await call


async def test_a_token_is_admitted_before_a_body_is_read(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")

    await steps.step_admits(setup, row, _named(access), _bearer(access))
    with pytest.raises(InvalidToken):
        await steps.step_admits(setup, row, _named(access), _bearer(access.token[:-1]))


async def _done_with(
    setup: Setup, acme: Acme, islands: Islands, head: Callable[[str], Any]
) -> uuid.UUID:
    """The solution batch said done while the store answers each file's
    question with `head`."""
    await _saved(setup, acme, islands)
    row = await _grading(setup, acme, islands)
    access = await _mint(setup, row, "early", "solution")
    written = _batch()(await _job(setup, row, "solution", access))
    listed = [
        {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for path, content in written.items()
    ]
    links = await steps.step_uploads(
        setup, row, _named(access), _bearer(access), json.dumps({"files": listed}).encode()
    )
    for path, link in links["files"].items():
        acme.fake.objects.put(link, written[path])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(acme.fake.objects, "head", head)
        await steps.step_done(setup, row, _named(access), _bearer(access), b"{}")
    return row


async def test_a_store_that_refuses_the_platform_is_not_taken_for_an_outage(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    async def refused(key: str) -> Any:
        raise Misconfigured("the store refused the platform's key")

    with pytest.raises(Misconfigured):
        await _done_with(setup, acme, islands, refused)


async def test_done_stops_asking_the_store_at_the_first_fault(
    setup: Setup, acme: Acme, islands: Islands
) -> None:
    async def missing_or_hung(key: str) -> Any:
        if key.endswith("/out/r1/output"):
            raise NotFound(key)
        await asyncio.Event().wait()

    row = await _done_with(setup, acme, islands, missing_or_hung)
    async with setup.unit_of_work() as ctx:
        held = await ctx.db.get(GradingStep, (row, "solution"))
        assert held is not None
    assert held.fault == "out/r1/output was not uploaded."


async def test_done_waits_for_the_store_a_bounded_time(
    setup: Setup, acme: Acme, islands: Islands, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hung(key: str) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(steps, "STORE_WAIT", timedelta(milliseconds=50))
    with pytest.raises(Unavailable, match="in time"):
        await _done_with(setup, acme, islands, hung)
