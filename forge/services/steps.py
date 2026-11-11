"""The door a task's Woodpecker steps ask for their work through, and
the store of what they did (SIMPLER-CORE.md 11.6, 11.9, phase 4.8): each
step's job, the links it uploads by, its done, the pipeline's report, and
the files the forge serves a step itself.

A step proves itself with a token of its own (`grading.step_token`), named
for the grading, its attempt and the step, minted when phase 4.9 writes
the step's settings (`mint`): `data_url`, the door, and `data_token`. The
token's SHA-256 is kept on the step's row, compared in constant time, and
the token is never logged nor put in any link. It works until the step
says it is done, the pipeline's report ends every token of the grading, or
`MACHINE_WAIT` and `RUN_TIMEOUT` after it was minted pass; a grading that
has finished takes none. The first job a grading's steps ask for is its
pipeline beginning: the grading becomes `running`, its deadline its
pipeline's steps' time limits summed (`wall_of`) from then. A grading runs
one pipeline, so a step's job is built from that pipeline's step and no
other's. No grading reuses what another did (#625): a step reads what its
own grading's steps made.

A step's call holds its own row only, never the grading's, so no step
waits on another's reads of the forge or the store; the report holds the
grading's row and every step's before it builds the result, so a done
said at the same moment is either in it or answered as already ended.

**The job** (`step_job`) is built when the step asks, from the plan of the
publication the grading grades against and its submission, none for a
save: the runs of the step still to do, each with its inputs placed, every
file a download by a presigned link that lives `LINK_LIFE` and reads one
object, or a copy from the submission the clone step checked out under
`submission/`, and a
folder with nothing in it made empty. An entrant's upload is in the
checkout whole, since the submission lists it as a large file and the clone
step fetches large files (`lfs: true`), and is copied from there checked by
the SHA-256 and size its pointer names, only for an upload the submission
took (`uploads`). A run is left out when its
part was not given, and when an earlier step it reads stopped it, with an
outcome not accepted or no such output. An earlier step that has given
nothing at all is a fault of the grading, `Misconfigured`, never nothing to
do. A step left with no run is answered that it has nothing to do (`step_job`
gives none), which its wrapper ends on with success, so the later steps
still run. A task or workflow file that is a large-file object of the
publication's (`publication_objects`) is read from the forge's store by
link; any other is served through the door (`step_file`), as is a
`program.yaml` on a boxed primitive's manifest port, which the primitive
reads as JSON. A task file of a step whose primitive takes the cache is
placed under the item's cache folder, one per run, or for exec's programs form per
program. A boxed step holds a block of box users while it runs. A boxed
step a later step of its pipeline reads is told to keep its outputs in the
workspace (`keep_of`, #623), and that later step is given each such file
from the workspace by its stored SHA-256 and size, with no link; an output
of a step that is not boxed is a link.

**Uploads and done.** The step lists what it will upload, and is handed a
presigned PUT per file under `results/<grading>/steps/<step>/`, each
signed for the SHA-256 it listed, so it writes those bytes or nothing,
then and when said again; once done, the door reads its `outputs.json`,
checked against what was listed, the contract and the step, asks the
store that every file an output names is there as listed, and keeps each
item in `step_outputs`; a step that faulted keeps nothing. A done said
again is answered the same.

**The report** builds result version 6 and finishes the grading: `done`,
or `system_error` when a run's verdict is one, then does what follows
(`pipelines.concluded`): the retry by itself, or a save's publication
taking over. It is answered the same when said again. The run
log is not kept yet.
"""

import asyncio
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Grading, GradingStep, PublicationObjects, StepOutput
from forge.db.tables import Upload as UploadRow
from forge.domain import exact_json
from forge.domain.checks import VERDICT, keep_runs
from forge.domain.content import UploadInfo, named_by
from forge.domain.contracts import violation
from forge.domain.definitions import TASK_FILE, parse_task
from forge.domain.environment import PIECES
from forge.domain.errors import (
    Conflict,
    GradingClosed,
    InvalidCallback,
    InvalidToken,
    Misconfigured,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
)
from forge.domain.grading import (
    CHECKOUT,
    FINISHED,
    MACHINE_WAIT,
    RUN_TIMEOUT,
    GradingStatus,
    run_deadline,
    step_token,
    token_hash,
    wall_of,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import SubmissionId, TaskId, VersionId, WorkflowId
from forge.domain.plan_basics import ERROR_LIMIT, PLAN_PATH, RESULT_MAX
from forge.domain.port_values import scalar_to_text
from forge.domain.primitives import MANIFEST, PROGRAMS
from forge.domain.program_manifest import parse_program
from forge.domain.publication_record import Publication
from forge.domain.run_plan import (
    BOXED,
    PIPELINES,
    RUN_ENTRY_KB,
    PinnedWorkflow,
    PipelineKind,
    PipelineStep,
    RunPlan,
    RunStep,
)
from forge.domain.run_results import check_failing, compilers
from forge.domain.run_task import RUNS_PATH, RunsFile
from forge.domain.step_data import (
    BOX_BLOCK,
    BOX_BLOCKS,
    BOX_FIRST,
    CONTRACT_VERSION,
    JOB_VERSION,
    LINK_LIFE,
    NAME,
    OUTPUTS_FILE,
    REPORT_STEP,
    Fault,
    Faulted,
    Found,
    Item,
    ItemOutputs,
    Upload,
    chain_of,
    is_batch,
    is_step_name,
    keep_of,
    output_type,
    program_alone,
    read_outputs,
    read_uploads,
    result_of,
    results_key,
    runs_file,
    skipped_runs,
    step_order,
    without,
)
from forge.domain.submissions import FILES_FOLDER, SUBMISSION_FILE
from forge.domain.uploads import POINTER_MOST, UploadStatus, may_be_pointer, upload_info
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import gradings, pipelines, runs

log = get_logger(__name__)

STEP_PATH = "/api/v1/gradings/{grading}/steps/{step}"
"""A step's door, under the machine URL: GET is its job, POST the report
step's report."""
UPLOADS_PATH = STEP_PATH + "/uploads"
DONE_PATH = STEP_PATH + "/done"
FILE_PATH = STEP_PATH + "/files/{sha256}"
"""A file the forge serves a step itself, by its SHA-256."""
TOKEN_LIFE = MACHINE_WAIT + RUN_TIMEOUT
"""How long a step's token works after it is minted at the latest: the
longest a pipeline waits for a machine, and the longest it runs."""
SUBMISSION_MAX = 1024 * 1024
BOX_TRIES = 32
HEADS = 16
"""How many of a step's files the door asks the store about at once."""
STORE_WAIT = timedelta(seconds=60)
"""The longest done waits for the store to say every file is there."""
WRONG_TOKEN = "The token is not this step's, or no longer works."
NOT_TAKING = "The grading takes no more of its steps' work."
SECRET_PLANNED = "The plan names a secret, which a save refuses until org secrets exist."
MISSING = object()
"""What an input is when an earlier step it reads has given nothing yet."""
RUNS = "runs"
"""The kind of file the door serves that is a value's runs file."""
STOPPED = object()
"""What an input is when an earlier step it reads gave what stops the
run: an outcome not accepted, or no such output."""


@dataclass(frozen=True, slots=True)
class StepAccess:
    """What a step's settings carry: its door and its token."""

    data_url: str
    token: str


async def mint(
    ctx: Context,
    row: Grading,
    *,
    pipeline: PipelineKind,
    step: str,
) -> StepAccess:
    """The settings of one Woodpecker step of the grading's `pipeline`, or
    of its `report` step, kept so its door takes the token. Minting again
    gives the same token. `NotFound` for a step the pipeline does not
    hold, `Conflict` for a grading already running another pipeline, and
    `GradingClosed` for a grading that has finished or a step that is
    done.
    """
    if GradingStatus(row.status) in FINISHED:
        raise GradingClosed(NOT_TAKING)
    plan, _ = await plan_of(ctx, row)
    if step != REPORT_STEP and step not in {held.name for held in plan.pipeline(pipeline)}:
        raise NotFound(f"The {pipeline} pipeline has no step {step}.")
    kept = {
        found.step: found
        for found in (
            await ctx.db.execute(select(GradingStep).where(GradingStep.grading_id == row.id))
        ).scalars()
    }
    if any(found.pipeline != pipeline for found in kept.values()):
        raise Conflict("A grading runs one pipeline.")
    found = kept.get(step)
    # A row already minted keeps the name its door was given. One minted
    # before the column existed has none, and is still found by its own name.
    woodpecker = found.woodpecker or step if found is not None else plan.woodpecker_name(step)
    given = access(ctx, row, step, woodpecker)
    if found is None:
        ctx.db.add(
            GradingStep(
                grading_id=row.id,
                step=step,
                woodpecker=woodpecker,
                pipeline=pipeline,
                token_hash=token_hash(given.token),
                expires_at=ctx.now + TOKEN_LIFE,
            )
        )
        await ctx.db.flush()
    elif found.done_at is not None:
        raise GradingClosed(NOT_TAKING)
    return given


def access(ctx: Context, row: Grading, step: str, woodpecker: str) -> StepAccess:
    """A step's door and token as `mint` gives them, worked out again and
    kept nowhere, for the answer to a CI that asks what the pipeline is.
    The door's URL carries `woodpecker`, the only name the running step
    knows; the token is over `step`, the step's name inside forge."""
    token = step_token(ctx.settings.token_encryption_key_bytes, row.id, row.attempt, step)
    return StepAccess(data_url=_door(ctx, row.id, woodpecker), token=token)


@action
async def step_job(
    ctx: Context,
    grading: uuid.UUID,
    step: str,
    authorization: str | None,
    *,
    platform: str | None = None,
) -> bytes | None:
    """The step's job, runner's `job.schema.json`, as JSON, or none when
    nothing is left for the step to do: every run of it left out, which
    the door answers 204. `InvalidToken` for a token that is not the
    step's or no longer works, `GradingClosed` for a grading that has
    finished.
    """
    row, held = await _open(ctx, grading, step, authorization)
    if held.step == REPORT_STEP:
        raise NotFound("The report step has no job.")
    plan, publication = await plan_of(ctx, row)
    await _begin(ctx, row, plan, _kind(held))
    reader = await _Reader.open(ctx, row, _kind(held), plan)
    built = await _Job(ctx, reader, held, publication, platform=platform).build()
    if built is None:
        log.info("steps.nothing_left", grading=str(row.id), step=held.step)
        return None
    log.info("steps.job", grading=str(row.id), step=held.step, items=len(held.items or []))
    return built


@action
async def step_admits(
    ctx: Context, grading: uuid.UUID, step: str, authorization: str | None
) -> None:
    """Nothing, once the token is the step's and still works, so a door
    can check it before it reads a large body, which the token is not
    over. It holds nothing. `InvalidToken` otherwise, and `GradingClosed`
    for a grading that has finished.
    """
    await _open(ctx, grading, step, authorization, lock=False)


@action
async def step_uploads(
    ctx: Context, grading: uuid.UUID, step: str, authorization: str | None, body: bytes
) -> dict[str, Any]:
    """A presigned PUT for each file the step lists, `{"files": {<path>:
    <link>}}`, each signed for the file's SHA-256, which the step sends in
    base64 in the header `x-amz-checksum-sha256`; the list kept for its
    done. The same list sent again is answered again, with links for the
    same bytes. `InvalidCallback` for a list naming a path the step may not
    write, over what it may write, sent before the step asked for its job,
    or other than the list it sent before, since a link for other bytes
    would let an earlier link write over what done then reads.
    """
    row, held = await _open(ctx, grading, step, authorization)
    if held.job_at is None or held.step == REPORT_STEP:
        raise InvalidCallback("The step uploads after it has asked for its job.")
    plan, _ = await plan_of(ctx, row)
    listed = read_uploads(
        body,
        item_mb=_held_step(plan, held).limits.output_mb,
        items=len(held.items or []),
    )
    kept = {path: {"sha256": up.sha256, "size": up.size} for path, up in listed.items()}
    if held.uploads is not None and held.uploads != kept:
        raise InvalidCallback("The step listed other uploads before, and keeps to that list.")
    held.uploads = kept
    objects = ctx.forge.objects

    def signed() -> dict[str, str]:
        return {
            path: objects.put_url(
                results_key(row.id, held.step, path), expires_in=LINK_LIFE, sha256=up.sha256
            )
            for path, up in listed.items()
        }

    return {"files": await asyncio.to_thread(signed)}


@action
async def step_done(
    ctx: Context, grading: uuid.UUID, step: str, authorization: str | None, body: bytes
) -> None:
    """The step's word that its uploads are in: its `outputs.json` read and
    its items kept, or its fault. Its token works no more. Said again, it
    is answered the same.
    """
    row, held = await _proven(ctx, grading, step, authorization)
    if held.done_at is not None:
        return
    _refuse_closed(ctx, row, held)
    if held.step == REPORT_STEP or held.uploads is None:
        raise InvalidCallback("The step lists its uploads before it is done.")
    plan, _ = await plan_of(ctx, row)
    pipeline_step = _held_step(plan, held)
    run_step = plan.steps[pipeline_step.step]
    declared = {path: (entry["sha256"], entry["size"]) for path, entry in held.uploads.items()}
    try:
        content = await _outputs_file(ctx, row.id, held.step, declared[OUTPUTS_FILE])
        listed = {path: Upload(sha256, size) for path, (sha256, size) in declared.items()}
        written = read_outputs(
            content, run_step, list(held.items or []), is_batch(pipeline_step), listed
        )
        named = {path: up for item in written for path, up in item.files.items()}
        await _check_stored(ctx, row.id, held.step, named)
        _program_answered(run_step, written)
    except Fault as fault:
        held.fault = str(fault).replace("\x00", "")[:ERROR_LIMIT]
        log.warning("steps.faulted", grading=str(row.id), step=held.step, fault=held.fault)
    else:
        for item in written:
            ctx.db.add(
                StepOutput(
                    id=uuid.uuid4(),
                    pipeline=held.pipeline,
                    grading_id=row.id,
                    step=held.step,
                    run=item.run,
                    outputs=item.outputs,
                    files={
                        path: {
                            "object": results_key(row.id, held.step, path),
                            "sha256": up.sha256,
                            "size": up.size,
                        }
                        for path, up in item.files.items()
                    },
                    measured_at=ctx.now,
                )
            )
    held.done_at = ctx.now
    held.box_first = None
    await ctx.db.flush()
    log.info("steps.done", grading=str(row.id), step=held.step, faulted=held.fault is not None)


@action
async def step_report(
    ctx: Context, grading: uuid.UUID, step: str, authorization: str | None, body: bytes
) -> GradingStatus:
    """The report step's word that the pipeline has ended: the result
    built and the grading finished, every token of it ended. Said again,
    it is answered the same.
    """
    if step != REPORT_STEP:
        raise NotFound("Only the report step reports.")
    row, held = await _proven(ctx, grading, step, authorization)
    if held.done_at is None:
        _refuse_closed(ctx, row, held)
        held_row = await gradings.find(ctx, grading, lock=True)
        assert held_row is not None
        row = held_row
        await ctx.db.execute(
            select(GradingStep)
            .where(GradingStep.grading_id == row.id)
            .order_by(GradingStep.step)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        try:
            document = exact_json.loads(body)
        except UnicodeDecodeError, ValueError, RecursionError:
            document = None
        if not isinstance(document, dict):
            raise InvalidCallback("The report is a JSON object.")
        await conclude(ctx, row, _kind(held))
        await pipelines.concluded(ctx, row)
        await ctx.db.execute(
            update(GradingStep)
            .where(GradingStep.grading_id == row.id)
            .values(done_at=ctx.now, box_first=None)
            .execution_options(synchronize_session=False)
        )
        held.done_at = ctx.now
        await ctx.db.flush()
    return GradingStatus(row.status)


@action
async def step_file(
    ctx: Context, grading: uuid.UUID, step: str, sha256: str, authorization: str | None
) -> bytes:
    """A file the step's job names that the forge serves itself, by its
    SHA-256. `NotFound` for one its job does not name. It holds no row, so
    a step's files are served side by side."""
    row, held = await _open(ctx, grading, step, authorization, lock=False)
    source = (held.served or {}).get(sha256)
    if source is None:
        raise NotFound("The step's job names no such file.")
    content = await _served(ctx, row, source)
    if hashlib.sha256(content).hexdigest() != sha256:
        raise Misconfigured("A file the forge serves a step is not what its plan pinned.")
    return content


async def pipeline_to_run(
    ctx: Context, row: Grading, pipeline: PipelineKind
) -> tuple[PipelineStep, ...]:
    """The Woodpecker steps of the grading's `pipeline` left to run, in
    their order: each whose items are all done taken out. An item is done
    when its part was not given, and when an earlier step it reads gave
    nothing accepted, as its job leaves it out. None left means the pipeline
    need not run; `conclude` builds its result.
    """
    plan, _ = await plan_of(ctx, row)
    steps = plan.pipeline(pipeline)
    reader = await _Reader.open(ctx, row, pipeline, plan)
    done: list[str] = []
    for held in steps:
        runs = held.runs if held.runs is not None else (None,)
        for run in runs:
            if run in reader.skipped or await reader.stopped(held.step, run):
                continue
            if await reader.output_of(held.step, run) is None:
                break
        else:
            done.append(held.name)
    return without(steps, done)


async def conclude(ctx: Context, row: Grading, pipeline: PipelineKind) -> None:
    """Build result version 6 of the grading's `pipeline` from what its
    steps hold, keep it and finish the grading: `done`, or `system_error`
    with the first fault, or saying the result is over its bound. A grading
    whose only fault is a value's program's ends `system_error` too, so the
    platform's retry grades it again; when that retry faults the same way,
    or no retry comes since `retry_failed` is off, it is `done` with the
    fault kept as its error.
    A grading that has finished is left as it is.
    """
    if GradingStatus(row.status) in FINISHED:
        return
    plan, publication = await plan_of(ctx, row)
    reader = await _Reader.open(ctx, row, pipeline, plan)
    faults = {
        held.step: held.fault
        for held in (
            await ctx.db.execute(select(GradingStep).where(GradingStep.grading_id == row.id))
        ).scalars()
        if held.fault is not None
    }
    graded = {run.id for run in plan.runs} - reader.skipped
    found: dict[Item, Found | Faulted] = {}
    for step in step_order(plan, pipeline):
        held_runs = plan.steps[step].items or {}
        for run in [None] if plan.steps[step].once else sorted(graded.intersection(held_runs)):
            name = _holding(plan, pipeline, step, run)
            if name is not None and name in faults:
                found[(step, run)] = Faulted(f"The step {name} faulted: {faults[name]}")
                continue
            got = await reader.output_of(step, run)
            if got is not None:
                found[(step, run)] = Found(str(got.id), got.outputs)
    result = result_of(plan, pipeline, reader.skipped, found)
    faulted = await _checked(ctx, row, plan, publication, result)
    problem = _result_problem(result)
    if problem is not None:
        gradings.finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=problem)
    elif (
        result["error"] is not None
        and pipeline == "grading"
        and program_alone(result)
        and (row.automatic or not ctx.settings.retry_failed)
    ):
        # Only a value's program failed, and its retry by itself is spent:
        # the grading is done, and only that value waits for staff, the
        # reason kept for them (stage 5 section 6 item 54).
        row.result = result
        gradings.finish(ctx, row, GradingStatus.DONE, error=str(result["error"])[:ERROR_LIMIT])
    elif result["error"] is not None or faulted is not None:
        row.result = result
        gradings.finish(
            ctx,
            row,
            GradingStatus.SYSTEM_ERROR,
            error=str(result["error"] or faulted)[:ERROR_LIMIT],
        )
    else:
        row.result = result
        gradings.finish(ctx, row, GradingStatus.DONE)
    log.info("steps.concluded", grading=str(row.id), status=row.status)


async def _checked(
    ctx: Context, row: Grading, plan: RunPlan, publication: Publication, result: dict[str, Any]
) -> str | None:
    """Fill in whether each check of a save's `result` got what it expects,
    and the sentences of what it did not, from the publication's
    `task.yaml` and runs, each keeping only the runs worth reading
    (`checks.keep_runs`); the first fault of a check's steps, for the row
    to end on and be tried again, none when there is none. A check that
    faulted got nothing. A publication whose settings or runs do not read
    fails every check, saying so."""
    entries: list[dict[str, Any]] = result.get("checks") or []
    if not entries:
        return None
    task = TaskId(row.task_id)
    try:
        found = await ctx.forge.content.read_file(PLATFORM, task, TASK_FILE, at=publication.version)
        definition = parse_task(found.content)
        held = await ctx.forge.content.read_file(PLATFORM, task, RUNS_PATH, at=publication.version)
        runs = RunsFile.from_bytes(held.content).runs
    except (NotFound, InvalidDefinition, ValueError) as exc:
        log.warning("steps.checks_unread", grading=str(row.id), error=type(exc).__name__)
        for entry in entries:
            entry["failing"] = [CHECKS_UNREAD]
            keep_runs(entry, None)
        return None
    scoring = definition.scoring
    columns = {name: entry.column() for name, entry in plan.reports.items()}
    faulted: str | None = None
    for index, entry in zip(plan.checks(), entries, strict=True):
        expect = definition.checks[index - 1].expect if index <= len(definition.checks) else {}
        verdict = expect.get(VERDICT)
        if entry["error"] is not None:
            faulted = faulted or f"Check {index}: {entry['error']}"
            entry["failing"] = [f"Its grading faulted: {entry['error']}"[:ERROR_LIMIT]]
        elif not expect:
            entry["failing"] = [CHECKS_UNREAD]
        else:
            failing = check_failing(scoring, runs, columns, compilers(plan), entry, expect)
            entry["passed"] = not failing
            entry["failing"] = failing
        keep_runs(entry, verdict if isinstance(verdict, str) else None)
    return faulted


CHECKS_UNREAD = "The task's settings or runs at this publication do not read."


async def _begin(ctx: Context, row: Grading, plan: RunPlan, pipeline: PipelineKind) -> None:
    """Mark a `dispatched` grading `running` at its first job, its deadline
    its pipeline's steps' time limits summed from now; the row is written
    only while it still says `dispatched`, so steps asking side by side
    write it once, and only when nothing else holds it, so a step never
    waits on its grading: a later job writes it then."""
    if row.status != GradingStatus.DISPATCHED:
        return
    deadline = run_deadline(
        ctx.now, wall_of([held.limits.time_ms for held in plan.pipeline(pipeline)])
    )
    free = (
        select(Grading.id)
        .where(Grading.id == row.id, Grading.status == GradingStatus.DISPATCHED)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    began = await ctx.db.execute(
        update(Grading)
        .where(Grading.id == free)
        .values(status=GradingStatus.RUNNING, started_at=ctx.now, deadline_at=deadline)
        .execution_options(synchronize_session=False)
    )
    if cast(Any, began).rowcount:
        gradings.changed(ctx, row)
        log.info("steps.began", grading=str(row.id))


# Who asks


async def _proven(
    ctx: Context, grading: uuid.UUID, step: str, authorization: str | None, *, lock: bool = True
) -> tuple[Grading, GradingStep]:
    """The grading, as read, and its step, held unless not to `lock`, once
    the token proves it the step's: checked on the row as read, then again
    once held. The token is compared by its SHA-256 in constant time. A
    step name no plan could give is refused before anything is read.
    `InvalidToken` otherwise.
    """
    token = runs.bearer(authorization) if is_step_name(step) else None
    if token is not None:
        given = token_hash(token)
        found = await _step(ctx, grading, step)
        if found is not None and hmac.compare_digest(found.token_hash, given):
            row = await gradings.find(ctx, grading)
            held = await _step(ctx, grading, step, lock=True) if lock else found
            if row is not None and held is not None and hmac.compare_digest(held.token_hash, given):
                return row, held
    log.info("steps.refused", grading=str(grading), reason="token")
    raise InvalidToken(WRONG_TOKEN)


async def _open(
    ctx: Context, grading: uuid.UUID, step: str, authorization: str | None, *, lock: bool = True
) -> tuple[Grading, GradingStep]:
    """`_proven`, for a step still at work."""
    row, held = await _proven(ctx, grading, step, authorization, lock=lock)
    if held.done_at is not None:
        log.info("steps.refused", grading=str(grading), reason="done")
        raise InvalidToken(WRONG_TOKEN)
    _refuse_closed(ctx, row, held)
    return row, held


def _refuse_closed(ctx: Context, row: Grading, held: GradingStep) -> None:
    if ctx.now >= held.expires_at:
        log.info("steps.refused", grading=str(row.id), reason="expired")
        raise InvalidToken(WRONG_TOKEN)
    if GradingStatus(row.status) in FINISHED:
        log.info("steps.refused", grading=str(row.id), reason="closed")
        raise GradingClosed(NOT_TAKING)


async def _step(
    ctx: Context, grading: uuid.UUID, step: str, *, lock: bool = False
) -> GradingStep | None:
    """The grading's step the door named `step`: the row whose Woodpecker
    name is `step`, or, where that was never written, the row whose own
    name is `step`."""
    by_woodpecker = select(GradingStep).where(
        GradingStep.grading_id == grading, GradingStep.woodpecker == step
    )
    by_own_name = select(GradingStep).where(
        GradingStep.grading_id == grading,
        GradingStep.step == step,
        GradingStep.woodpecker.is_(None),
    )
    for query in (by_woodpecker, by_own_name):
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        found = (await ctx.db.execute(query)).scalar_one_or_none()
        if found is not None:
            return found
    return None


def _door(ctx: Context, grading: uuid.UUID, step: str) -> str:
    return (
        f"{str(ctx.settings.machine_url).rstrip('/')}{STEP_PATH.format(grading=grading, step=step)}"
    )


# What the grading reads


async def plan_of(ctx: Context, row: Grading) -> tuple[RunPlan, Publication]:
    """The run plan of the publication the grading grades against, as the
    split the grading was made with lays it out (`RunPlan.for_split`).
    """
    try:
        publication = await gradings.publication_of(ctx, row)
        found = await ctx.forge.content.read_file(
            PLATFORM, TaskId(row.task_id), PLAN_PATH, at=publication.version
        )
        plan = RunPlan.from_bytes(found.content).for_split(row.released_split)
        return plan, publication
    except (NotFound, ValidationError) as exc:
        raise Misconfigured(f"The grading has no run plan: {type(exc).__name__}") from None


def _held_step(plan: RunPlan, held: GradingStep) -> PipelineStep:
    for found in plan.pipeline(_kind(held)):
        if found.name == held.step:
            return found
    raise Misconfigured(f"The plan's {held.pipeline} pipeline has no step {held.step}.")


def _kind(held: GradingStep) -> PipelineKind:
    """The pipeline a step runs in, which its row's check keeps one."""
    return cast(PipelineKind, held.pipeline)


def _holding(plan: RunPlan, pipeline: PipelineKind, step: str, run: str | None) -> str | None:
    """The Woodpecker step of `pipeline` that does `step` for `run`."""
    for held in plan.pipeline(pipeline):
        if held.step == step and (held.runs is None or run in held.runs):
            return held.name
    return None


class _Reader:
    """What one grading's items read, each worked out once: the submission's
    inputs, the runs it skips, whether each item's inputs are all there,
    and each item's outputs, by its step and run. Those are its own
    grading's alone. No other grading's are read, a save row's neither: only
    the planner's builds and the checks' copies run at the save, and no
    grading reads them (#625, #655).
    """

    def __init__(
        self,
        ctx: Context,
        row: Grading,
        pipeline: PipelineKind,
        plan: RunPlan,
        inputs: Mapping[str, Any],
    ) -> None:
        self.ctx = ctx
        self.row = row
        self.pipeline = pipeline
        self.plan = plan
        self.inputs = inputs
        # A save has no submission, so it gives no part and skips no run.
        self.skipped = frozenset() if pipeline == "save" else skipped_runs(plan, inputs)
        self._read: dict[Item, Any] = {}
        self._hashes: dict[str, str] = {}
        self._uploaded: set[tuple[str, int]] | None = None
        self.uploads: dict[str, UploadInfo] = {}
        """Each entrant file read so far that is an upload, by its path."""
        self._own: dict[Item, StepOutput] = {}
        self.runs_files: dict[str, bytes] = {}
        """Each runs file a value's program reads, built so far, by its
        SHA-256."""

    @classmethod
    async def open(
        cls, ctx: Context, row: Grading, pipeline: PipelineKind, plan: RunPlan
    ) -> _Reader:
        document: Any = {"schema_version": 6, "inputs": {}}
        if row.submission_id is not None:
            try:
                content = await ctx.forge.workspaces.read_submission_file(
                    PLATFORM,
                    SubmissionId(row.submission_id),
                    SUBMISSION_FILE,
                    max_size=SUBMISSION_MAX,
                )
                document = exact_json.loads(content)
            except (NotFound, Rejected, ValueError) as exc:
                raise Misconfigured(f"The submission does not read: {type(exc).__name__}") from None
        if not isinstance(document, dict) or document.get("schema_version") != 6:
            raise Misconfigured("The submission is not written against version 6.")
        reader = cls(ctx, row, pipeline, plan, document.get("inputs") or {})
        steps = {(kind, held.name): held.step for kind in PIPELINES for held in plan.pipeline(kind)}
        mine = select(StepOutput).where(StepOutput.grading_id == row.id)
        for own in (await ctx.db.execute(mine)).scalars():
            step = steps.get((cast(PipelineKind, own.pipeline), own.step))
            if step is None:
                raise Misconfigured(f"The plan holds no step {own.step} that made an output.")
            reader._own.setdefault((step, own.run), own)
        return reader

    async def ready(self, step: str, run: str | None) -> bool:
        """Whether every earlier step's output `step`'s item for `run` reads
        is there and accepted."""
        item = (step, None if self.plan.steps[step].once else run)
        if item not in self._read:
            run_step = self.plan.steps[step]
            if run_step.once or run is None:
                inputs = run_step.inputs or {}
            else:
                inputs = (run_step.items or {})[run].inputs
            check = run_step.check.index if run_step.check is not None else None
            self._read[item] = await self._ports(inputs, item[1], check)
        return self._read[item] is not MISSING and self._read[item] is not STOPPED

    async def file_name(self, value: Mapping[str, Any], run: str | None) -> str:
        """The name of the file `value` is: an entrant's as they uploaded
        it, a task's or a workflow's by its path, an earlier step's as it
        wrote it."""
        if "entrant" in value:
            files = self.entrant_files(value)
            if len(files) != 1:
                raise Misconfigured(f"The submission gives no one file for {value['entrant']}.")
            path = files[0]
        elif "task" in value:
            path = value["task"]
        elif "workflow" in value:
            path = value["workflow"]
        elif "step" in value:
            found = await self.output_of(value["step"], run)
            if found is None:
                raise Misconfigured(f"The step {value['step']} gave nothing for a run it reads.")
            path = found.outputs[value["output"]]["file"]
        else:
            raise Misconfigured(
                f"The plan gives a folder port a value with no file: {sorted(value)}"
            )
        return str(path).rsplit("/", 1)[-1]

    async def stopped(self, step: str, run: str | None) -> bool:
        """Whether `step`'s item for `run` will never be done: an earlier
        step it reads gave what stops the run."""
        await self.ready(step, run)
        return self._read[(step, None if self.plan.steps[step].once else run)] is STOPPED

    async def output_of(self, step: str, run: str | None) -> StepOutput | None:
        """`step`'s item for `run` as this grading holds it."""
        return self._own.get((step, None if self.plan.steps[step].once else run))

    async def _ports(
        self, values: Mapping[str, Any], run: str | None, check: int | None = None
    ) -> Any:
        """Each of `values` by `canon`: `STOPPED` when one is, else
        `MISSING` when one is."""
        found: dict[str, Any] = {}
        for name, value in values.items():
            found[name] = await self.canon(value, run, check)
            if found[name] is STOPPED:
                return STOPPED
        return MISSING if any(value is MISSING for value in found.values()) else found

    async def canon(self, value: Any, run: str | None, check: int | None = None) -> Any:
        """`value` with every file by its content; `MISSING` when an
        earlier step it reads has given nothing, `STOPPED` when what it
        gave stops the run. A check's copy reads that check's runs."""
        if not isinstance(value, dict):
            return value
        if "files" in value:
            files = await self._ports(value["files"], run, check)
            return files if files is MISSING or files is STOPPED else {"files": files}
        if PROGRAMS in value:
            programs: dict[str, Any] = {}
            for program, inputs in value[PROGRAMS].items():
                programs[program] = await self._ports(inputs, run, check)
                if programs[program] is STOPPED:
                    return STOPPED
            if any(found is MISSING for found in programs.values()):
                return MISSING
            return {PROGRAMS: programs}
        if "environment" in value:
            content = exact_json.dumps(value["environment"], sort_keys=True).encode()
            return {"file": hashlib.sha256(content).hexdigest()}
        if set(value) == {"value"} or "links" in value or "secret" in value:
            return value
        if set(value) == {"runs"}:
            return await self._runs(value["runs"], check)
        if "task" in value:
            return _facts(self.plan.task_files, value["task"])
        if "workflow" in value:
            return _facts(self.plan.pinned(value.get("pin", 0)).files, value["workflow"])
        if "template" in value:
            parts = [self.entrant_value(part) for part in value["parts"]]
            return {"value": value["template"].format(*[scalar_to_text(part) for part in parts])}
        if "entrant" in value:
            return await self._entrant(value)
        if "step" in value:
            return await self._output(value, run)
        raise Misconfigured(f"The plan holds a value this forge cannot read: {sorted(value)}")

    async def _runs(self, ids: list[str], check: int | None = None) -> Any:
        """The runs file a value's program reads (`runs_file`), by its
        content, kept to place; `MISSING` while an item one of the runs
        goes through has given nothing. A check's copy of the program reads
        that check's runs."""
        order = step_order(self.plan, self.pipeline)
        found: dict[Item, Found] = {}
        for run in ids:
            if run in self.skipped:
                continue
            for item in chain_of(self.plan, order, run, check):
                got = await self.output_of(*item)
                if got is None:
                    return MISSING
                found[item] = Found(str(got.id), got.outputs)
                if got.outputs.get("outcome") != "accepted":
                    break
        content = runs_file(self.plan, self.pipeline, ids, self.skipped, found, check)
        if content is None:
            return MISSING
        if len(content) > RUN_ENTRY_KB * 1024 * len(ids):
            raise Misconfigured("A value's runs file is larger than its plan allows.")
        sha256 = hashlib.sha256(content).hexdigest()
        self.runs_files[sha256] = content
        return {"file": sha256}

    def given(self, value: Mapping[str, Any]) -> Any:
        """What the submission gives for an entrant value, or its part."""
        given = self.inputs.get(value["entrant"])
        if "part" in value and isinstance(given, dict):
            given = (given.get("parts") or {}).get(str(value["part"]))
        if not isinstance(given, dict):
            raise Misconfigured(f"The submission gives nothing for {value['entrant']}.")
        return given

    def entrant_value(self, value: Mapping[str, Any]) -> Any:
        given = self.given(value)
        if "value" not in given:
            raise Misconfigured(f"The submission gives no value for {value['entrant']}.")
        return given["value"]

    def entrant_root(self, value: Mapping[str, Any]) -> str:
        """The folder of the submission an entrant input's files are in."""
        root = f"{FILES_FOLDER}/{value['entrant']}/"
        return f"{root}{value['part']}/" if "part" in value else root

    def entrant_files(self, value: Mapping[str, Any]) -> list[str]:
        given = self.given(value)
        root = self.entrant_root(value)
        files = given.get("files")
        if not isinstance(files, list) or not all(
            isinstance(path, str)
            and path.startswith(root)
            and all(segment not in ("", ".", "..") for segment in path.split("/"))
            for path in files
        ):
            raise Misconfigured(f"The submission's files for {value['entrant']} do not read.")
        return files

    async def _entrant(self, value: Mapping[str, Any]) -> Any:
        given = self.given(value)
        if "value" in given:
            return {"value": given["value"]}
        entry = self.plan.entrant[value["entrant"]]
        root = self.entrant_root(value)
        hashes = {
            path.removeprefix(root): await self._hash(path, entry.max_size)
            for path in self.entrant_files(value)
        }
        if entry.type == "file":
            return {"file": next(iter(hashes.values()), "")}
        return {"folder": hashes}

    async def _hash(self, path: str, max_size: int | None) -> str:
        """An entrant file's SHA-256: an upload's own, from its pointer,
        which must be one of the uploads the submission took, so a job names
        no other large file."""
        if path not in self._hashes:
            content = await self.ctx.forge.workspaces.read_submission_blob(
                PLATFORM,
                SubmissionId(self.row.submission_id),
                path,
                max_size=max(max_size or 0, POINTER_MOST),
            )
            info = upload_info(content)
            if info is not None:
                if (info.digest, info.size) not in await self._taken():
                    raise Misconfigured(f"The submission's {path} is no upload it took.")
                self.uploads[path] = info
            self._hashes[path] = info.digest if info else hashlib.sha256(content).hexdigest()
        return self._hashes[path]

    async def _taken(self) -> set[tuple[str, int]]:
        """The SHA-256 and size of each upload the submission took."""
        if self._uploaded is None:
            rows = await self.ctx.db.execute(
                select(UploadRow.digest, UploadRow.size).where(
                    UploadRow.consumed_by == self.row.submission_id,
                    UploadRow.status == UploadStatus.CONSUMED,
                )
            )
            self._uploaded = {(digest, size) for digest, size in rows.all()}
        return self._uploaded

    async def _output(self, value: Mapping[str, Any], run: str | None) -> Any:
        step, name = value["step"], value["output"]
        found = await self.output_of(step, run)
        if found is None:
            return STOPPED if await self.stopped(step, run) else MISSING
        if found.outputs.get("outcome") != "accepted" or name not in found.outputs:
            return STOPPED
        given = found.outputs[name]
        declared = output_type(self.plan.steps[step], name)
        if declared is not None and declared[0] == "file":
            return {"file": found.files[given["file"]]["sha256"]}
        if declared is not None and declared[0] == "folder":
            under = given["folder"].rstrip("/") + "/"
            return {
                "folder": {
                    path.removeprefix(under): fact["sha256"]
                    for path, fact in found.files.items()
                    if path.startswith(under)
                }
            }
        return {"value": given}


def _facts(files: Mapping[str, Any], entry: str) -> dict[str, Any]:
    if entry.endswith("/"):
        return {
            "folder": {
                path.removeprefix(entry): files[path].sha256
                for path in sorted(named_by(files, entry))
            }
        }
    return {"file": files[entry].sha256}


# The job


class _CacheItem:
    """One item folder of the step's cache: random, made when first used,
    each name in it once."""

    def __init__(self) -> None:
        self.folder: str | None = None
        self.names: set[str] = set()

    def path(self, name: str | None) -> str:
        if self.folder is None:
            self.folder = secrets.token_hex(8)
        if name is None or name in self.names or not NAME.fullmatch(name):
            name = secrets.token_hex(8)
        self.names.add(name)
        return f"cache/{self.folder}/{name}"


class _Job:
    """One step's job, built from the plan's items for its runs."""

    def __init__(
        self,
        ctx: Context,
        reader: _Reader,
        held: GradingStep,
        publication: Publication,
        *,
        platform: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.platform = platform
        self.reader = reader
        self.held = held
        self.plan = reader.plan
        self.publication = publication
        self.pipeline_step = _held_step(self.plan, held)
        self.step = self.plan.steps[self.pipeline_step.step]
        self.boxed = self.step.primitive.partition("@")[0] in BOXED
        # The running step knows only its Woodpecker name. A row minted
        # before that name was stored has none, and its door uses its own.
        self.door = _door(ctx, reader.row.id, held.woodpecker or held.step)
        self.files: list[dict[str, Any]] = []
        self.placed: dict[tuple[str, ...], str] = {}
        self.served: dict[str, dict[str, str]] = {}
        """Each file the door serves the step, by SHA-256: where it is read
        from and the SHA-256 the plan pinned it by, so serving it reads no
        plan."""
        self.objects: frozenset[str] = frozenset()
        self.kept: dict[str, str | None] = {}
        """Where each step of the row whose output the job reads kept it,
        by its plan name (`_kept`)."""

    async def build(self) -> bytes | None:
        """The job as JSON, none when no run of the step is left to do."""
        reader, held = self.reader, self.held
        items: list[tuple[str | None, Mapping[str, Any]]] = []
        for run, inputs in self.plan.items_of(self.pipeline_step):
            if run in reader.skipped:
                continue
            if not await reader.ready(self.pipeline_step.step, run):
                if await reader.stopped(self.pipeline_step.step, run):
                    continue
                raise Misconfigured(
                    f"The step {held.step} reads a step that has given nothing"
                    + (f" for the run {run}." if run else ".")
                )
            if await reader.output_of(self.pipeline_step.step, run):
                continue
            items.append((run, inputs))
        if not items:
            return None
        self.objects = await _objects(self.ctx, reader.row, self.plan, self.publication)
        placed = [(run, await self._inputs(inputs, run)) for run, inputs in items]
        if is_batch(self.pipeline_step):
            given: dict[str, Any] = {
                "schema_version": CONTRACT_VERSION,
                "batch": [{"run": run, "inputs": inputs} for run, inputs in placed],
            }
        else:
            given = {"schema_version": CONTRACT_VERSION, "inputs": placed[0][1]}
        document: dict[str, Any] = {
            "schema_version": JOB_VERSION,
            "inputs": given,
            "files": self.files,
            "uploads": f"{self.door}/uploads",
            "done": f"{self.door}/done",
            "time_limit": Decimal(self.pipeline_step.limits.time_ms) / 1000,
            # A check's copy that stops its runs leaves the next check to run.
            "fail_on_non_accepted": self.step.once and self.step.check is None,
        }
        if self.boxed:
            first = await _box_users(self.ctx, held)
            document["box_users"] = {"first": first, "last": first + BOX_BLOCK - 1}
            document["step_memory_mb"] = self.pipeline_step.limits.memory_mb
        keep = keep_of(self.plan, reader.pipeline, self.pipeline_step)
        if keep is not None:
            document["keep"] = keep
        held.items = [run for run, _ in items]
        held.served = self.served
        held.job_at = held.job_at or self.ctx.now
        await self.ctx.db.flush()
        return exact_json.dumps(document).encode()

    async def _inputs(self, inputs: Mapping[str, Any], run: str | None) -> dict[str, Any]:
        item = _CacheItem()
        placed = {
            port: await self._value(value, run, item, manifest=port == MANIFEST)
            for port, value in inputs.items()
            if port != "environment"
        }
        if "environment" in inputs:
            placed["environment"] = await self._value(
                inputs["environment"], run, item, languages=_chosen(placed)
            )
        for port in self.step.folders or ():
            given = placed.get(port)
            if isinstance(given, dict) and set(given) == {"file"}:
                name = await self.reader.file_name(inputs[port], run)
                placed[port] = self._folder_of_one(given["file"], name)
        return placed

    def _folder_of_one(self, path: str, name: str) -> dict[str, str]:
        """The file placed at `path`, given to a folder port: placed again
        as the one file of a folder of its own, under its name
        (TASK-FORMAT.md 1.3)."""
        folder = self._folder()
        placed = next(entry for entry in self.files if entry["path"] == path)
        self.files.append({**placed, "path": _under(folder, name)})
        return {"folder": folder}

    async def _value(
        self,
        value: Mapping[str, Any],
        run: str | None,
        item: _CacheItem,
        *,
        name: str | None = None,
        manifest: bool = False,
        languages: set[str] | None = None,
    ) -> Any:
        if "environment" in value:
            lock = value["environment"]
            if (
                self.platform not in ("linux-64", "linux-aarch64")
                or self.platform not in lock["platforms"]
            ):
                raise InvalidCallback(
                    "The job must name a locked platform: linux-64 or linux-aarch64."
                )
            locked_languages = lock["platforms"][self.platform]
            if languages is not None and not languages <= locked_languages.keys():
                raise Misconfigured("The task's lock does not cover the step's chosen language.")
            selected = {
                name: entry
                for name, entry in locked_languages.items()
                if languages is None or name in languages
            }
            pieces = {}
            for entry in selected.values():
                for piece in (each for source in PIECES for each in entry.get(source, [])):
                    digest = piece["sha256"]
                    path = f"cache/pieces/{digest}"
                    pieces[digest] = path
                    if not any(each["path"] == path for each in self.files):
                        self.files.append(
                            {
                                "path": path,
                                "url": self.ctx.forge.objects.lfs_get_url(
                                    digest, expires_in=LINK_LIFE
                                ),
                                "sha256": digest,
                                "size": piece["size"],
                                "cached": True,
                            }
                        )
            content = exact_json.dumps(
                {
                    "version": lock["version"],
                    "prefix": lock["prefix"],
                    "platform": self.platform,
                    "languages": selected,
                    "pieces": pieces,
                },
                sort_keys=True,
            ).encode()
            digest = hashlib.sha256(content).hexdigest()
            self.served[digest] = {"kind": RUNS, "content": content.decode(), "sha256": digest}
            return {
                "file": self._download(
                    ("environment", digest),
                    lambda: f"{self.door}/files/{digest}",
                    digest,
                    len(content),
                )
            }
        if set(value) == {"value"}:
            return value["value"]
        if "links" in value:
            return value["links"]
        if "secret" in value:
            raise Misconfigured(SECRET_PLANNED)
        if "files" in value:
            return {
                "files": {
                    key: await self._value(each, run, item, name=key)
                    for key, each in value["files"].items()
                }
            }
        if PROGRAMS in value:
            programs: dict[str, Any] = {}
            for program, inputs in value[PROGRAMS].items():
                own = _CacheItem()
                programs[program] = {
                    port: await self._value(each, run, own, manifest=port == MANIFEST)
                    for port, each in inputs.items()
                }
            return {PROGRAMS: programs}
        if "template" in value:
            parts = [self.reader.entrant_value(part) for part in value["parts"]]
            return value["template"].format(*[scalar_to_text(part) for part in parts])
        if set(value) == {"runs"}:
            check = self.step.check.index if self.step.check is not None else None
            return self._runs(await self.reader.canon(value, run, check))
        if manifest and self.boxed and ("task" in value or "workflow" in value):
            return {"file": await self._manifest(value)}
        if "task" in value:
            return self._task_file(value["task"], item, name)
        if "workflow" in value:
            pin = value.get("pin", 0)
            held = self.plan.pinned(pin)
            return self._pinned("workflow", value["workflow"], held.files, held if pin else None)
        if "entrant" in value:
            return self._entrant(value)
        if "step" in value:
            return await self._output(value, run)
        raise Misconfigured(f"The plan holds a value this forge cannot place: {sorted(value)}")

    def _task_file(self, path: str, item: _CacheItem, name: str | None) -> dict[str, str]:
        if not self.step.cache or path.endswith("/"):
            return self._pinned("task", path, self.plan.task_files)
        fact = self.plan.task_files[path]
        placed = item.path(name)
        self.files.append(
            {
                "path": placed,
                "url": self._link("task", path, fact.sha256),
                "sha256": fact.sha256,
                "size": fact.size,
                "cached": True,
            }
        )
        return {"file": placed}

    def _pinned(
        self,
        kind: str,
        entry: str,
        files: Mapping[str, Any],
        held: PinnedWorkflow | None = None,
    ) -> dict[str, str]:
        """A task's or workflow's file, or folder, placed under `in/` once
        however many items read it. `held` is an inner pin's workflow."""
        source = (kind, entry) if held is None else (kind, held.id, entry)
        if not entry.endswith("/"):
            return {
                "file": self._download(
                    source,
                    lambda: self._link(kind, entry, files[entry].sha256, held),
                    files[entry].sha256,
                    files[entry].size,
                )
            }
        folder = self._folder()
        for path in sorted(named_by(files, entry)):
            fact = files[path]
            self.files.append(
                {
                    "path": _under(folder, path.removeprefix(entry)),
                    "url": self._link(kind, path, fact.sha256, held),
                    "sha256": fact.sha256,
                    "size": fact.size,
                }
            )
        return self._filled(folder)

    def _download(self, source: tuple[str, ...], link: Any, sha256: str, size: int) -> str:
        return self._place(source, lambda: {"url": link(), "sha256": sha256, "size": size})

    def _place(self, source: tuple[str, ...], entry: Callable[[], dict[str, Any]]) -> str:
        """The path `source` is placed at under `in/`, once however many
        items read it, by the file entry `entry` makes but its path."""
        if source not in self.placed:
            path = self._folder()
            self.files.append({"path": path, **entry()})
            self.placed[source] = path
        return self.placed[source]

    def _link(self, kind: str, path: str, sha256: str, held: PinnedWorkflow | None = None) -> str:
        """A task's or workflow's file by link: from the forge's large-file
        store when it is one of the publication's objects, else through the
        door."""
        if sha256 in self.objects:
            return self.ctx.forge.objects.lfs_get_url(sha256, expires_in=LINK_LIFE)
        self.served[sha256] = self._source(kind, path, sha256, held)
        return f"{self.door}/files/{sha256}"

    def _source(
        self, kind: str, path: str, sha256: str, held: PinnedWorkflow | None = None
    ) -> dict[str, str]:
        """Where the door reads a file it serves: a task's at the
        publication's version, a workflow's at the plan's commit. `held` is
        an inner pin, read at its own commit."""
        source = {"kind": kind, "path": path, "sha256": sha256}
        if kind.endswith("task"):
            return {**source, "at": self.publication.version}
        workflow = held or self.plan.workflow
        return {**source, "workflow": workflow.id, "commit": workflow.commit}

    def _runs(self, canonical: Any) -> dict[str, str]:
        """A value's runs file, served through the door from what the job
        keeps of it, since no store holds it."""
        if not isinstance(canonical, dict):
            raise Misconfigured("A value's program reads runs that have not all given.")
        sha256 = canonical["file"]
        content = self.reader.runs_files[sha256]
        self.served[sha256] = {"kind": RUNS, "content": content.decode(), "sha256": sha256}
        return {
            "file": self._download(
                (RUNS, sha256), lambda: f"{self.door}/files/{sha256}", sha256, len(content)
            )
        }

    async def _manifest(self, value: Mapping[str, Any]) -> str:
        """A `program.yaml` on a manifest port, served as the JSON the
        primitive reads."""
        if "task" in value:
            kind, path, files, held = "task", value["task"], self.plan.task_files, None
        else:
            pin = value.get("pin", 0)
            held_pin = self.plan.pinned(pin)
            kind, path, files = "workflow", value["workflow"], held_pin.files
            held = held_pin if pin else None
        source = self._source(f"manifest_{kind}", path, files[path].sha256, held)
        content = await _served(self.ctx, self.reader.row, source)
        sha256 = hashlib.sha256(content).hexdigest()
        self.served[sha256] = source
        return self._download(
            ("manifest", sha256), lambda: f"{self.door}/files/{sha256}", sha256, len(content)
        )

    def _entrant(self, value: Mapping[str, Any]) -> Any:
        given = self.reader.given(value)
        if "value" in given:
            return given["value"]
        files = self.reader.entrant_files(value)
        if self.plan.entrant[value["entrant"]].type == "file":
            if len(files) != 1:
                raise Misconfigured(f"The submission gives no one file for {value['entrant']}.")
            workspace = files[0]
            kind = "file"
        else:
            workspace = self.reader.entrant_root(value).rstrip("/")
            kind = "folder"
        source = ("workspace", workspace)
        if source not in self.placed:
            path = self._folder()
            upload = self.reader.uploads.get(workspace)
            if upload is not None:
                self.files.append(_upload(path, workspace, upload))
            elif files:
                self.files.append({"path": path, "workspace": f"{CHECKOUT}/{workspace}"})
                root = self.reader.entrant_root(value)
                for each in files:
                    if (found := self.reader.uploads.get(each)) is not None:
                        placed = _under(path, each.removeprefix(root))
                        self.files.append(_upload(placed, each, found))
            else:
                self.files.append({"path": path, "folder": True})
            self.placed[source] = path
        return {kind: self.placed[source]}

    async def _output(self, value: Mapping[str, Any], run: str | None) -> Any:
        found = await self.reader.output_of(value["step"], run)
        if found is None:
            raise Misconfigured(f"The step {value['step']} gave nothing for a run it reads.")
        given = found.outputs[value["output"]]
        declared = output_type(self.plan.steps[value["step"]], value["output"])
        if declared is None or declared[0] not in ("file", "folder"):
            return given
        kept = self._kept(found)
        objects = self.ctx.forge.objects

        def entry(path: str, fact: Mapping[str, Any]) -> dict[str, Any]:
            if kept is not None:
                where = {"workspace": f"{kept}/{path}"}
            else:
                where = {"url": objects.get_url(fact["object"], expires_in=LINK_LIFE)}
            return {**where, "sha256": fact["sha256"], "size": fact["size"]}

        if declared[0] == "file":
            fact = found.files[given["file"]]
            return {
                "file": self._place(("object", fact["object"]), lambda: entry(given["file"], fact))
            }
        under = given["folder"].rstrip("/") + "/"
        folder = self._folder()
        for path, fact in sorted(found.files.items()):
            if path.startswith(under):
                self.files.append(
                    {"path": _under(folder, path.removeprefix(under)), **entry(path, fact)}
                )
        return self._filled(folder)

    def _kept(self, found: StepOutput) -> str | None:
        """Where in the workspace the step that made `found` kept it, none
        when it is read by link, made by a step that keeps nothing
        (`keep_of`). An output names the Woodpecker step that made it by its
        plan name, `<step>.<run>` for a step per run."""
        if found.step not in self.kept:
            made = next(
                (
                    held
                    for held in self.plan.pipeline(self.reader.pipeline)
                    if held.name == found.step
                ),
                None,
            )
            if made is None:
                raise Misconfigured(f"The pipeline holds no step {found.step} that gave an output.")
            self.kept[found.step] = keep_of(self.plan, self.reader.pipeline, made)
        return self.kept[found.step]

    def _folder(self) -> str:
        return f"in/{secrets.token_hex(8)}"

    def _filled(self, folder: str) -> dict[str, str]:
        """A folder an input names, made empty by the job when no file
        was placed in it, so its path is there."""
        if not any(entry["path"].startswith(f"{folder}/") for entry in self.files):
            self.files.append({"path": folder, "folder": True})
        return {"folder": folder}


def _upload(path: str, workspace: str, upload: UploadInfo) -> dict[str, Any]:
    """An entrant's upload placed at `path` from the checkout, which fetched
    it whole since the submission lists it as a large file, checked by its
    SHA-256 and size; for a file of a folder, again over the folder's copy."""
    return {
        "path": path,
        "workspace": f"{CHECKOUT}/{workspace}",
        "sha256": upload.digest,
        "size": upload.size,
    }


def _under(folder: str, relative: str) -> str:
    """A file of a folder placed under `folder`, refused when a name in its
    path is not one a job may place."""
    segments = relative.split("/")
    if not all(NAME.fullmatch(segment) for segment in segments):
        raise Misconfigured(f"A folder a step reads holds a name a job cannot place: {relative!r}")
    return "/".join([folder, *segments])


async def _box_users(ctx: Context, held: GradingStep) -> int:
    """The first of the step's block of box users, held until it is done
    or its token expires: a block no step still at work holds."""
    if held.box_first is not None:
        return held.box_first
    ended = (
        select(GradingStep.grading_id, GradingStep.step)
        .where(
            GradingStep.box_first.is_not(None),
            (GradingStep.done_at.is_not(None)) | (GradingStep.expires_at <= ctx.now),
        )
        .with_for_update(skip_locked=True)
    )
    await ctx.db.execute(
        update(GradingStep)
        .where(tuple_(GradingStep.grading_id, GradingStep.step).in_(ended))
        .values(box_first=None)
        .execution_options(synchronize_session=False)
    )
    for _ in range(BOX_TRIES):
        first = BOX_FIRST + secrets.randbelow(BOX_BLOCKS) * BOX_BLOCK
        try:
            async with ctx.db.begin_nested():
                held.box_first = first
                await ctx.db.flush()
            return first
        except IntegrityError:
            held.box_first = None
    raise Unavailable("No block of box users is free. Try again in a moment.")


async def _objects(
    ctx: Context, row: Grading, plan: RunPlan, publication: Publication
) -> frozenset[str]:
    """The SHA-256 of every large-file object among the publication's planned
    files, the task's and its workflow's, read once and kept."""
    kept = await ctx.db.get(PublicationObjects, row.publication_id)
    if kept is not None:
        return frozenset(kept.objects)
    found: dict[str, int] = {}
    for step in plan.steps.values():
        inputs = (
            [step.inputs]
            if step.inputs is not None
            else [item.inputs for item in (step.items or {}).values()]
        )
        for ports in inputs:
            for languages in (
                ports.get("environment", {}).get("environment", {}).get("platforms", {}).values()
            ):
                for entry in languages.values():
                    for piece in (each for source in PIECES for each in entry.get(source, [])):
                        found[piece["sha256"]] = piece["size"]
    task = TaskId(row.task_id)
    listing = await ctx.forge.content.list_files(PLATFORM, task, at=publication.version)
    small = [
        path
        for path in plan.task_files
        if path in listing.tokens and may_be_pointer(listing.sizes.get(path))
    ]
    blobs = await ctx.forge.content.read_blobs(
        PLATFORM, task, {listing.tokens[path] for path in small}
    )
    for path in small:
        info = upload_info(blobs.get(listing.tokens[path], b""))
        if info is not None and info.digest == plan.task_files[path].sha256:
            found[info.digest] = info.size
    workflow, commit = WorkflowId(plan.workflow.id), VersionId(plan.workflow.commit)
    pinned = await ctx.forge.workflows.workflow_files(PLATFORM, workflow, commit)
    for path, fact in plan.workflow.files.items():
        if not may_be_pointer(pinned.sizes.get(path)):
            continue
        info = upload_info(
            (await ctx.forge.workflows.read_workflow_at(PLATFORM, workflow, commit, path)).content
        )
        if info is not None and info.digest == fact.sha256:
            found[info.digest] = info.size
    for inner in plan.inner_workflows:
        inner_id, inner_at = WorkflowId(inner.id), VersionId(inner.commit)
        listed = await ctx.forge.workflows.workflow_files(PLATFORM, inner_id, inner_at)
        for path, fact in inner.files.items():
            if not may_be_pointer(listed.sizes.get(path)):
                continue
            info = upload_info(
                (
                    await ctx.forge.workflows.read_workflow_at(PLATFORM, inner_id, inner_at, path)
                ).content
            )
            if info is not None and info.digest == fact.sha256:
                found[info.digest] = info.size
    await ctx.db.execute(
        insert(PublicationObjects)
        .values(
            publication_id=row.publication_id, task_id=row.task_id, objects=found, read_at=ctx.now
        )
        .on_conflict_do_nothing(index_elements=["publication_id"])
    )
    return frozenset(found)


async def _served(ctx: Context, row: Grading, source: Mapping[str, str]) -> bytes:
    """What the door serves a step for `source` (`_Job._source`): a task's
    or workflow's file, or a manifest's JSON from one, its YAML checked
    against the SHA-256 the plan pinned."""
    kind = source["kind"]
    if kind == RUNS:
        content = source["content"].encode()
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise Misconfigured("A file the forge serves a step is not what its job named.")
        return content
    path = source["path"]
    if kind.endswith("task"):
        content = (
            await ctx.forge.content.read_file(
                PLATFORM, TaskId(row.task_id), path, at=VersionId(source["at"])
            )
        ).content
    else:
        content = (
            await ctx.forge.workflows.read_workflow_at(
                PLATFORM, WorkflowId(source["workflow"]), VersionId(source["commit"]), path
            )
        ).content
    if hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise Misconfigured("A file the forge serves a step is not what its plan pinned.")
    if kind.startswith("manifest_"):
        return parse_program(content, path, own_names=frozenset()).as_json()
    return content


# What a step wrote


async def _outputs_file(
    ctx: Context, grading: uuid.UUID, step: str, declared: tuple[str, int]
) -> bytes:
    sha256, size = declared
    try:
        content = await ctx.forge.objects.read(
            results_key(grading, step, OUTPUTS_FILE), max_size=size
        )
    except NotFound:
        raise Fault("outputs.json was not uploaded.") from None
    except Misconfigured:
        raise
    except Rejected:
        raise Fault("outputs.json is larger than the step listed.") from None
    except PortError as exc:
        raise Unavailable(f"The results store did not answer: {type(exc).__name__}") from None
    if len(content) != size or hashlib.sha256(content).hexdigest() != sha256:
        raise Fault("outputs.json is not what the step listed.")
    return content


async def _check_stored(
    ctx: Context, grading: uuid.UUID, step: str, named: Mapping[str, Upload]
) -> None:
    """`Fault` unless every file an output names is in the store as the
    step listed it: one listed and never put is not. The first fault ends
    the checks still under way, and a store that has not answered them all
    in `STORE_WAIT` is `Unavailable`."""
    limit = asyncio.Semaphore(HEADS)

    async def check(path: str, listed: Upload) -> None:
        async with limit:
            try:
                found = await ctx.forge.objects.head(results_key(grading, step, path))
            except NotFound:
                raise Fault(f"{path} was not uploaded.") from None
            except Misconfigured:
                raise
            except PortError as exc:
                raise Unavailable(
                    f"The results store did not answer: {type(exc).__name__}"
                ) from None
        if found.size != listed.size or found.sha256 != listed.sha256:
            raise Fault(f"{path} is not what the step listed.")

    try:
        async with asyncio.timeout(STORE_WAIT.total_seconds()), asyncio.TaskGroup() as group:
            for path, listed in sorted(named.items()):
                group.create_task(check(path, listed))
    except* TimeoutError:
        raise Unavailable("The results store did not answer in time.") from None
    except* (Fault, PortError) as found:
        raise found.exceptions[0] from None


def _program_answered(step: RunStep, written: Sequence[ItemOutputs]) -> None:
    """`Fault` for a value's program that ended other than accepted, or
    without its report: never the entrant's verdict, and nothing kept, so
    the grading's retry runs it again."""
    value = step.value
    if value is None or value.report is None:
        return
    for item in written:
        outcome = item.outputs.get("outcome")
        if outcome != "accepted":
            raise Fault(f"The program of {value.name} ended {outcome}.")
        if value.report not in item.outputs:
            raise Fault(f"The program of {value.name} reported no {value.report}.")


def _result_problem(result: Mapping[str, Any]) -> str | None:
    size = len(exact_json.dumps(result).encode())
    if size > RESULT_MAX:
        return f"The result is {size} bytes, more than the {RESULT_MAX} a result may be."
    broken = violation(result, "result", "result_v6")
    if broken is not None:
        return f"The result does not match result.schema.json {broken}"[:ERROR_LIMIT]
    return None


def _chosen(ports: Mapping[str, Any]) -> set[str] | None:
    """The languages a job's placed inputs choose, or none when a language
    is not a plain value yet, such as one wired from another step's output:
    the job then lists every language the lock holds, as the plan locked
    them all (`environment_plan.languages_in`).
    """
    found: set[str] = set()
    if "language" in ports:
        if not isinstance(ports["language"], str):
            return None
        found.add(ports["language"])
    for program in ports.get(PROGRAMS, {}).get(PROGRAMS, {}).values():
        inner = _chosen(program)
        if inner is None:
            return None
        found |= inner
    return {"python" if language == "ipynb" else language for language in found}
