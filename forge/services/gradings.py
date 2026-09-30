"""The gradings of submissions: the rows a submit, a retry, a rejudge and the
reconcile pass make, what a run of one is in the port's words, and the
organiser's controls over them.

A grading is one row per submission, stage and attempt, and nothing about
one is ever edited into another: a retry and a rejudge make new attempts,
each a new row with a new id and so new secrets, and the old rows stay as
they were, so what was graded when stays readable. Each row is inserted
`queued` with the SHA-256 of its first run's callback token, and a grading
that goes back to the queue after a run died is given its next run's
(`renew_token`).

An organiser managing the task reads its gradings and acts on one:

- `cancel` stops a grading that is not finished, at the CI too when a run
  of it is there, so a run nobody will look at does not hold a machine;
- `retry` makes a new attempt of a finished one, against the publication
  the old attempt graded against, unless another attempt of it is still
  being graded;
- `rejudge` makes a new attempt of every submission's latest attempt at
  every stage the current publication still has, against that publication.
  A latest attempt still being graded against an older publication is
  cancelled first; one being graded against the current one is left to
  finish.

A grading is named by its id, which is no access control: an organiser who
does not observe the task is told there is no such grading. `task_of` gives
the task a grading is of, so the host's guard can check the organiser there
before a control whose route names only the grading.
"""

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_

from forge.db.tables import Grading
from forge.domain.definitions import TaskDefinition, Trigger
from forge.domain.errors import Conflict, NotFound, PortError, Unavailable, WrongStatus
from forge.domain.grading import (
    AT_THE_CI,
    ENDED,
    FIND_MARGIN,
    FINISHED,
    PLATFORM_POOL,
    UNFINISHED,
    GradingRun,
    GradingStatus,
    callback_token,
    envelope_key,
    token_hash,
)
from forge.domain.ids import (
    OrgName,
    PublicationId,
    RunId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
    new_id,
)
from forge.domain.publications import Publication
from forge.domain.roles import Role, holds, task_scope
from forge.domain.submissions import Submitted
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import org_accounts, published
from forge.services.access import Organiser, require
from forge.services.credentials import CannotDecrypt

log = get_logger(__name__)

NO_SUCH_GRADING = "There is no such grading."
LIST_LIMIT = 500

CI_CONFIG_PATH = "/api/v1/ci/config"
"""Where the CI asks what a run is, the configuration extension, under the
platform's internal URL."""
ENVELOPE_PATH = "/api/v1/gradings/{grading}/envelope"
"""Where a run fetches its envelope, under the machine URL, with the envelope
key as `?key=`."""
CALLBACK_PATH = "/api/v1/gradings/{grading}/callback"
"""Where a run reports, under the machine URL."""


@dataclass(frozen=True, slots=True)
class GradingRecord:
    """One grading as an organiser reads it: which submission, stage and
    attempt, against which publication, where it stands and why it waits,
    the verdict as it came back, whether its log was written, the last
    progress its run reported, and its times.
    """

    id: uuid.UUID
    task: TaskId
    workspace: WorkspaceId
    submission_number: int
    submitted_at: datetime
    publication: PublicationId
    stage: str
    attempt: int
    status: GradingStatus
    wait_reason: str | None
    error: str | None
    verdict: dict[str, Any] | None
    log: bool
    progress: dict[str, Any] | None
    requeues: int
    queued_at: datetime
    retry_at: datetime | None
    dispatched_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime | None


@dataclass(frozen=True, slots=True)
class Rejudged:
    """What a rejudge did: the publication the new attempts grade against,
    how many it queued, how many unfinished attempts against an older
    publication it cancelled first, how many it left to finish against the
    current one, and how many it passed over because the current publication
    no longer has their stage.
    """

    task: TaskId
    publication: PublicationId
    queued: int
    cancelled: int
    left_running: int
    passed_over: int


def new_row(
    ctx: Context,
    *,
    task: TaskId,
    workspace: WorkspaceId,
    submission: SubmissionId,
    number: int,
    version: VersionId,
    submitted_at: datetime,
    publication: PublicationId,
    stage: str,
    attempt: int,
    key: str | None,
) -> Grading:
    """A new `queued` grading, added to the unit of work, with the hash of
    its first run's callback token.
    """
    row = Grading(
        id=new_id(),
        task_id=task,
        workspace_id=workspace,
        submission_id=submission,
        submission_number=number,
        submission_version=version,
        submitted_at=submitted_at,
        publication_id=publication,
        stage=stage,
        attempt=attempt,
        idempotency_key=key,
        status=GradingStatus.QUEUED,
        queued_at=ctx.now,
        requeues=0,
    )
    renew_token(ctx, row)
    ctx.db.add(row)
    return row


def renew_token(ctx: Context, row: Grading) -> None:
    """Keep on the row the SHA-256 of the callback token of its run to come,
    the one numbered by how often it went back to the queue.
    """
    token = callback_token(ctx.settings.token_encryption_key_bytes, row.id, run=row.requeues)
    row.callback_token_hash = token_hash(token)


def callback_token_of(ctx: Context, row: Grading) -> str:
    """The token the grading's current run reports back with."""
    return callback_token(ctx.settings.token_encryption_key_bytes, row.id, run=row.requeues)


def envelope_key_of(ctx: Context, row: Grading) -> str:
    """The key the URL of the envelope of the grading's current run carries."""
    return envelope_key(ctx.settings.token_encryption_key_bytes, row.id, run=row.requeues)


def queue_submission(
    ctx: Context,
    *,
    task: TaskId,
    workspace: WorkspaceId,
    submission: Submitted,
    publication: Publication,
    definition: TaskDefinition,
    key: str | None,
    at: datetime,
) -> builtins.list[Grading]:
    """One queued grading of the submission for each stage the task grades
    on submit, attempt 1, against `publication`.
    """
    return [
        new_row(
            ctx,
            task=task,
            workspace=workspace,
            submission=submission.id,
            number=submission.number,
            version=submission.version,
            submitted_at=at,
            publication=publication.id,
            stage=stage.id,
            attempt=1,
            key=key,
        )
        for stage in definition.stages_resolved()
        if stage.trigger is Trigger.ON_SUBMIT
    ]


async def find(ctx: Context, grading: uuid.UUID, *, lock: bool = False) -> Grading | None:
    """The grading by its id, held until the unit of work ends when `lock`,
    and then read afresh.
    """
    query = select(Grading).where(Grading.id == grading)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await ctx.db.execute(query)).scalar_one_or_none()


def envelope_url(ctx: Context, row: Grading) -> str:
    """Where the grading's current run fetches its envelope, key included."""
    path = ENVELOPE_PATH.format(grading=row.id)
    return f"{_machine_url(ctx)}{path}?key={envelope_key_of(ctx, row)}"


def callback_url(ctx: Context, grading: uuid.UUID) -> str:
    """Where the grading's run reports."""
    return f"{_machine_url(ctx)}{CALLBACK_PATH.format(grading=grading)}"


def _machine_url(ctx: Context) -> str:
    return str(ctx.settings.machine_url).rstrip("/")


async def publication_of(ctx: Context, row: Grading) -> Publication:
    """The publication the grading grades against. `NotFound` when the task
    no longer has it.
    """
    for found in await ctx.forge.workspaces.list_publications(TaskId(row.task_id)):
        if found.id == row.publication_id:
            return found
    raise NotFound(f"{row.task_id} has no publication {row.publication_id}")


async def run_of(ctx: Context, row: Grading) -> GradingRun:
    """What a run of the grading is, in the port's words."""
    publication = await publication_of(ctx, row)
    return GradingRun(
        grading=row.id,
        task=TaskId(row.task_id),
        publication=PublicationId(row.publication_id),
        publication_version=publication.version,
        submission=SubmissionId(row.submission_id),
        submission_version=VersionId(row.submission_version),
        envelope_url=envelope_url(ctx, row),
        compute=PLATFORM_POOL,
    )


def org_of(row: Grading) -> OrgName:
    return OrgName(task_scope(TaskId(row.task_id)).org)


def finish(row: Grading, status: GradingStatus, now: datetime, *, error: str | None = None) -> None:
    """End the grading with `status`, waiting for nothing more."""
    row.status = status
    row.finished_at = now
    row.wait_reason = None
    row.retry_at = None
    row.error = error


@action
async def task_of(ctx: Context, grading: uuid.UUID) -> TaskId:
    """The task a grading is of, the scope the host's guard checks an
    organiser at before a control that names only the grading. `NotFound`
    for no such grading.
    """
    row = await find(ctx, grading)
    if row is None:
        raise NotFound(NO_SUCH_GRADING)
    return TaskId(row.task_id)


@action
async def cancel(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> GradingRecord:
    """Stop a grading that is not finished, at the CI too when a run of it is
    there. `WrongStatus` for one that is finished.
    """
    row = await _managed(ctx, organiser, grading)
    status = GradingStatus(row.status)
    if status in FINISHED:
        raise WrongStatus(f"The grading is {status.value} already.", current=status.value)
    if status in AT_THE_CI and row.run_id is not None:
        await _cancel_run(ctx, RunId(row.run_id))
    elif status is GradingStatus.DISPATCHING:
        await _cancel_lost_start(ctx, row)
    finish(row, GradingStatus.CANCELLED, ctx.now)
    await ctx.db.flush()
    log.info("gradings.cancelled", grading=str(row.id), user_id=organiser.user.id)
    return record(row)


@action
async def retry(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> GradingRecord:
    """A new attempt of a finished grading, against the publication it graded
    against, the old attempt kept as it is. `WrongStatus` for one that is
    not finished, and `Conflict` while another attempt of it is graded.
    """
    found = await _managed(ctx, organiser, grading, lock=False)
    attempts = await _attempts(ctx, [(found.submission_id, found.stage)])
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = GradingStatus(row.status)
    if status not in FINISHED:
        raise WrongStatus(f"The grading is {status.value}, not finished.", current=status.value)
    if any(GradingStatus(other.status) in UNFINISHED for other in attempts):
        raise Conflict("Another attempt of this grading is still being graded.")
    made = _next_attempt(ctx, row, PublicationId(row.publication_id), attempts)
    await ctx.db.flush()
    log.info("gradings.retried", grading=str(row.id), attempt=made.attempt)
    return record(made)


@action
async def rejudge(ctx: Context, organiser: Organiser, task: TaskId) -> Rejudged:
    """A new attempt of every submission's latest attempt at each stage the
    task's current publication has, against that publication. `NotFound`
    for a task with no publication.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    current = await published.task(ctx, task)
    if current is None:
        raise NotFound("The task has no publication to grade against.")
    stages = {stage.id for stage in current.definition.stages_resolved()}
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task)
                .order_by(Grading.submission_id, Grading.stage, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[tuple[str, str], Grading] = {}
    attempts: dict[tuple[str, str], builtins.list[Grading]] = {}
    for row in rows:
        latest[(row.submission_id, row.stage)] = row
        attempts.setdefault((row.submission_id, row.stage), []).append(row)
    queued = cancelled = left_running = passed_over = 0
    for pair, row in latest.items():
        if row.stage not in stages:
            passed_over += 1
            continue
        if GradingStatus(row.status) in UNFINISHED:
            if row.publication_id == current.publication.id:
                left_running += 1
                continue
            await _stop_quietly(ctx, row)
            cancelled += 1
        _next_attempt(ctx, row, current.publication.id, attempts[pair])
        queued += 1
    await ctx.db.flush()
    log.info(
        "gradings.rejudged",
        task=task,
        publication=current.publication.id,
        queued=queued,
        cancelled=cancelled,
        left_running=left_running,
        passed_over=passed_over,
    )
    return Rejudged(task, current.publication.id, queued, cancelled, left_running, passed_over)


@action
async def list(
    ctx: Context, organiser: Organiser, task: TaskId, *, limit: int = 100
) -> tuple[GradingRecord, ...]:
    """The task's gradings, newest first, at most `limit` of them and never
    more than 500, for an organiser observing the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task)
                .order_by(Grading.created_at.desc(), Grading.id.desc())
                .limit(max(1, min(limit, LIST_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    return tuple(record(row) for row in rows)


def record(row: Grading) -> GradingRecord:
    return GradingRecord(
        id=row.id,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission_number=row.submission_number,
        submitted_at=row.submitted_at,
        publication=PublicationId(row.publication_id),
        stage=row.stage,
        attempt=row.attempt,
        status=GradingStatus(row.status),
        wait_reason=row.wait_reason,
        error=row.error,
        verdict=row.verdict,
        log=row.log_key is not None,
        progress=row.progress,
        requeues=row.requeues,
        queued_at=row.queued_at,
        retry_at=row.retry_at,
        dispatched_at=row.dispatched_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        deadline_at=row.deadline_at,
    )


async def _managed(
    ctx: Context, organiser: Organiser, grading: uuid.UUID, *, lock: bool = True
) -> Grading:
    """The grading, held until the unit of work ends when `lock`, for an
    organiser who manages its task. `NotFound` for one whose task they do
    not observe.
    """
    row = await find(ctx, grading, lock=lock)
    if row is None:
        raise NotFound(NO_SUCH_GRADING)
    scope = task_scope(TaskId(row.task_id))
    if not holds(organiser.grants, scope, Role.OBSERVER):
        raise NotFound(NO_SUCH_GRADING)
    require(organiser, scope, Role.MANAGER)
    return row


async def _attempts(ctx: Context, pairs: Sequence[tuple[str, str]]) -> builtins.list[Grading]:
    """Every attempt of the submissions at the stages, held until the unit of
    work ends and read afresh, in the order a rejudge takes them, so a retry
    and a rejudge never wait on each other in a circle.
    """
    return builtins.list(
        (
            await ctx.db.execute(
                select(Grading)
                .where(tuple_(Grading.submission_id, Grading.stage).in_(builtins.list(pairs)))
                .order_by(Grading.submission_id, Grading.stage, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _next_attempt(
    ctx: Context, row: Grading, publication: PublicationId, attempts: Sequence[Grading]
) -> Grading:
    return new_row(
        ctx,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission=SubmissionId(row.submission_id),
        number=row.submission_number,
        version=VersionId(row.submission_version),
        submitted_at=row.submitted_at,
        publication=publication,
        stage=row.stage,
        attempt=max(other.attempt for other in attempts) + 1,
        key=None,
    )


async def _cancel_run(ctx: Context, run: RunId) -> None:
    """Stop the run at the CI; one the CI no longer has is stopped already."""
    try:
        await ctx.forge.grading.cancel_run(run)
    except NotFound:
        return
    except PortError as exc:
        log.warning("gradings.cancel_failed", run=run, error=type(exc).__name__, detail=exc.detail)
        raise Unavailable("The CI did not stop the grading's run; try again.") from exc


async def _cancel_lost_start(ctx: Context, row: Grading) -> None:
    """Stop the run a start whose answer was lost may have made, when the CI
    has one. Nothing is refused for it: a run of a cancelled grading is
    refused its envelope, and so grades nothing.
    """
    try:
        account = await org_accounts.identity(ctx, org_of(row))
        run = await ctx.forge.grading.find_run(
            account, await run_of(ctx, row), since=row.queued_at - FIND_MARGIN
        )
        if run is not None and run.status not in ENDED:
            await ctx.forge.grading.cancel_run(run.id)
    except (PortError, CannotDecrypt) as exc:
        log.warning(
            "gradings.lost_start_not_stopped", grading=str(row.id), error=type(exc).__name__
        )


async def _stop_quietly(ctx: Context, row: Grading) -> None:
    """Cancel an unfinished grading a rejudge replaces, its run at the CI
    too when there is one; a run the CI does not stop is refused its
    reports, since the grading is cancelled.
    """
    try:
        if GradingStatus(row.status) in AT_THE_CI and row.run_id is not None:
            await ctx.forge.grading.cancel_run(RunId(row.run_id))
        elif GradingStatus(row.status) is GradingStatus.DISPATCHING:
            await _cancel_lost_start(ctx, row)
    except PortError as exc:
        log.warning(
            "gradings.replaced_run_not_stopped", grading=str(row.id), error=type(exc).__name__
        )
    finish(row, GradingStatus.CANCELLED, ctx.now)
