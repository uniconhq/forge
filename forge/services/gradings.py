"""The gradings of submissions: the rows a submit, a retry, a rejudge and the
operator's reconcile make, starting each one's run at the CI, what a run of
one is in the port's words, and the organiser's controls over them.

A grading is one row per submission, stage and attempt, and nothing about
one is ever edited into another: a retry and a rejudge make new attempts,
each a new row with a new id and so new secrets, and the old rows stay as
they were, so what was graded when stays readable. Each row is inserted
`queued` with the SHA-256 of its run's callback token, and its run is
started as soon as the unit of work that made it commits (`start`), since
the CI asks the platform about the grading while the start is under way. A
start the CI refuses, or does not answer, ends the grading in
`system_error` saying why: whoever reads it sees that at once and tries
again, a contestant by submitting, an organiser with `retry`.

A grading whose run the CI has lost reads as a system error saying so,
like an overdue one, and nothing is written when it is read: `lost` asks
the CI where the runs of `dispatched` gradings are, once they have waited
long enough for that to mean something (`grading.worth_asking`), at most
once every `RUN_STATE_KEPT` for each run however many people are watching
it, and with the reader's connection let go of first.

An organiser managing the task reads its gradings and acts on one:

- `cancel` stops a grading that is not finished, at the CI too when a run
  of it is there, so a run nobody will look at does not hold a machine,
  including one that reads as a system error because it is overdue or lost
  while its row still waits;
- `retry` makes a new attempt of a finished one, against the publication
  the old attempt graded against, unless another attempt of it is still
  being graded. One that reads as finished only because it is overdue or
  lost is ended first, with the reason written on its row, and the old
  run is cancelled at the CI once the retry has committed, so it does not
  keep a machine's containers going;
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
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, tuple_

from forge.db.tables import Grading
from forge.domain.definitions import TaskDefinition, Trigger
from forge.domain.errors import (
    Conflict,
    Forbidden,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
    WrongStatus,
)
from forge.domain.grading import (
    AT_THE_CI,
    FINISHED,
    PLATFORM_POOL,
    UNFINISHED,
    GradingRun,
    GradingStatus,
    RunState,
    callback_token,
    envelope_key,
    overdue,
    token_hash,
    worth_asking,
)
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import (
    OrgId,
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
RUN_STATE_KEPT = timedelta(seconds=15)
"""How long this process keeps the CI's answer about where one run is, so a
contestant's page polling every two seconds asks the CI a few times a
minute at most."""

ACCOUNT_NOT_READY = "The org's grading account is not ready."
NOT_ACTIVATED = "The task is not taken for grading at the CI."
REFUSED = "The CI refused the org's grading account."
NO_RUN = "The CI answered without starting a run."
NO_ANSWER = "The CI did not answer."
PUBLICATION_GONE = "The publication it grades against is gone."

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
    attempt, against which publication, where it stands, the verdict as it
    came back, whether its log was written, the last progress its run
    reported, and its times.
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
    error: str | None
    verdict: dict[str, Any] | None
    log: bool
    progress: dict[str, Any] | None
    queued_at: datetime
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
    its run's callback token, whose run starts once the unit of work
    commits.
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
    )
    row.callback_token_hash = token_hash(callback_token_of(ctx, row))
    ctx.db.add(row)
    grading = row.id

    async def start_it(later: Context) -> None:
        await start(later, grading)

    ctx.after_commit(start_it)
    return row


def callback_token_of(ctx: Context, row: Grading) -> str:
    """The token the grading's run reports back with."""
    return callback_token(ctx.settings.token_encryption_key_bytes, row.id)


def envelope_key_of(ctx: Context, row: Grading) -> str:
    """The key the URL of the envelope of the grading's run carries."""
    return envelope_key(ctx.settings.token_encryption_key_bytes, row.id)


async def start(ctx: Context, grading: uuid.UUID) -> None:
    """Start the run of a grading that is still `queued`, as its org's
    account, and record it: the grading is then `dispatched`, waiting for a
    machine. A start that fails ends the grading in `system_error`, saying
    why in the platform's words; what the CI said goes to the log.

    The call to the CI holds neither a connection nor a lock, since the CI
    asks the platform about the grading while the start is under way, and a
    rush of starts must leave it a connection to answer on. So what the
    start needs is read and that much committed, the run is started, and
    only then is the row taken and the run recorded, or the run cancelled
    when the grading moved on meanwhile. The work after a commit owns its
    unit of work, which is what lets it commit partway.

    A grading of a long batch, a rejudge or a reconcile pass, may already
    read as overdue by the time its turn comes; it is started all the same,
    since its row still says `queued`, unless an organiser has made a later
    attempt of it meanwhile.
    """
    row = await find(ctx, grading)
    if row is None or row.status != GradingStatus.QUEUED:
        return
    if await _superseded(ctx, row):
        log.info("gradings.start_superseded", grading=str(grading))
        return
    try:
        account = await org_accounts.identity(ctx, org_of(row))
    except (NotFound, CannotDecrypt) as exc:
        await _not_started(ctx, grading, exc, ACCOUNT_NOT_READY)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    try:
        run = await run_of(ctx, row)
    except NotFound as exc:
        await _not_started(ctx, grading, exc, PUBLICATION_GONE)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    await ctx.db.commit()
    try:
        found = await _start_run(ctx, account, run)
    except (PortError, CannotDecrypt) as exc:
        reason = _start_failure(exc) if isinstance(exc, PortError) else ACCOUNT_NOT_READY
        await _not_started(ctx, grading, exc, reason)
        return
    row = await find(ctx, grading, lock=True)
    if row is None or row.status != GradingStatus.QUEUED or await _superseded(ctx, row):
        log.info("gradings.start_overtaken", grading=str(grading), run=found)
        await _cancel_quietly(ctx, found)
        return
    row.status = GradingStatus.DISPATCHED
    row.run_id = found
    row.dispatched_at = ctx.now
    log.info("gradings.dispatched", grading=str(row.id), run=found)


async def _superseded(ctx: Context, row: Grading) -> bool:
    """Whether a later attempt of the same submission and stage exists."""
    later = await ctx.db.scalar(
        select(Grading.id)
        .where(
            Grading.submission_id == row.submission_id,
            Grading.stage == row.stage,
            Grading.attempt > row.attempt,
        )
        .limit(1)
    )
    return later is not None


async def _start_run(ctx: Context, account: AsOrgAccount, run: GradingRun) -> RunId:
    """Start the run, and when the CI refuses the org's account, sign it in
    again and try once more, so a login the CI lost heals at the next start.
    Nothing is held while the CI is called.
    """
    try:
        return await ctx.forge.grading.start_run(account, run)
    except Forbidden:
        account = await org_accounts.renew(ctx, account)
        await ctx.db.commit()
        return await ctx.forge.grading.start_run(account, run)


async def _not_started(ctx: Context, grading: uuid.UUID, exc: Exception, reason: str) -> None:
    """End the grading in `system_error` with `reason`, unless it moved on
    meanwhile.
    """
    log.warning(
        "gradings.start_failed", grading=str(grading), error=type(exc).__name__, detail=str(exc)
    )
    row = await find(ctx, grading, lock=True)
    if row is not None and row.status == GradingStatus.QUEUED:
        finish(row, GradingStatus.SYSTEM_ERROR, ctx.now, error=reason)


async def _cancel_quietly(ctx: Context, run: RunId) -> None:
    try:
        await ctx.forge.grading.cancel_run(run)
    except PortError as exc:
        log.warning("gradings.cancel_failed", run=run, error=type(exc).__name__)


def _start_failure(exc: PortError) -> str:
    match exc:
        case Forbidden():
            return REFUSED
        case Rejected():
            return NO_RUN
        case NotFound():
            return NOT_ACTIVATED
    return NO_ANSWER


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


def org_of(row: Grading) -> OrgId:
    return OrgId(task_scope(TaskId(row.task_id)).org)


def finish(row: Grading, status: GradingStatus, now: datetime, *, error: str | None = None) -> None:
    """End the grading with `status`, waiting for nothing more."""
    row.status = status
    row.finished_at = now
    row.error = error


def overdue_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> str | None:
    """Why the grading is past what its state may take, or none. `lost`
    names the gradings whose runs the CI was found to have lost.
    """
    return overdue(
        GradingStatus(row.status),
        created_at=row.queued_at,
        dispatched_at=row.dispatched_at,
        deadline=row.deadline_at,
        now=ctx.now,
        lost=row.id in lost,
    )


def status_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> GradingStatus:
    """Where the grading stands now: one past what its state may take, or
    whose run the CI has lost, is a system error (`grading.overdue`).
    """
    if overdue_of(ctx, row, lost) is not None:
        return GradingStatus.SYSTEM_ERROR
    return GradingStatus(row.status)


async def lost(ctx: Context, rows: Iterable[Grading]) -> frozenset[uuid.UUID]:
    """The gradings among `rows` whose run the CI no longer holds. Only a
    grading worth asking about is asked about, each run at most once every
    `RUN_STATE_KEPT` by this process, and the connection is let go of before
    the CI is asked. A CI that does not answer loses nothing: the grading
    reads as its row says.
    """
    asking = [
        (row.id, RunId(row.run_id))
        for row in rows
        if row.run_id is not None
        and worth_asking(GradingStatus(row.status), row.dispatched_at, ctx.now)
    ]
    if not asking:
        return frozenset()
    await ctx.let_go()
    found: set[uuid.UUID] = set()
    for grading, run in asking:
        if await _run_state(ctx, grading, run) is RunState.LOST:
            found.add(grading)
    return frozenset(found)


async def _run_state(ctx: Context, grading: uuid.UUID, run: RunId) -> RunState | None:
    async def ask() -> RunState | None:
        try:
            state = await ctx.forge.grading.run_state(run)
        except PortError as exc:
            log.warning("gradings.run_state_unanswered", run=run, error=type(exc).__name__)
            return None
        if state is RunState.LOST:
            log.warning("gradings.run_lost", grading=str(grading), run=run)
        return state

    return await ctx.memo.remembered(f"gradings.run_state.{run}", RUN_STATE_KEPT, ask)


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
    stored = GradingStatus(row.status)
    if stored in FINISHED:
        raise WrongStatus(f"The grading is {stored.value} already.", current=stored.value)
    if stored in AT_THE_CI and row.run_id is not None:
        await _cancel_run(ctx, RunId(row.run_id))
    finish(row, GradingStatus.CANCELLED, ctx.now)
    await ctx.db.flush()
    log.info("gradings.cancelled", grading=str(row.id), user_id=organiser.user.id)
    return record(ctx, row)


@action
async def retry(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> GradingRecord:
    """A new attempt of a finished grading, against the publication it graded
    against, the old attempt kept as it is. `WrongStatus` for one that is
    not finished, and `Conflict` while another attempt of it is graded.
    """
    found = await _managed(ctx, organiser, grading, lock=False)
    gone = await lost(ctx, [found])
    attempts = await _attempts(ctx, [(found.submission_id, found.stage)])
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status not in FINISHED:
        raise WrongStatus(f"The grading is {status.value}, not finished.", current=status.value)
    if any(status_of(ctx, other, gone) in UNFINISHED for other in attempts):
        raise Conflict("Another attempt of this grading is still being graded.")
    stored = GradingStatus(row.status)
    if stored in UNFINISHED:
        finish(row, GradingStatus.SYSTEM_ERROR, ctx.now, error=overdue_of(ctx, row, gone))
    if row.run_id is not None and stored not in (GradingStatus.DONE, GradingStatus.CANCELLED):
        _cancel_after_commit(ctx, RunId(row.run_id))
    made = _next_attempt(ctx, row, PublicationId(row.publication_id), attempts)
    await ctx.db.flush()
    log.info("gradings.retried", grading=str(row.id), attempt=made.attempt)
    return record(ctx, made)


def _cancel_after_commit(ctx: Context, run: RunId) -> None:
    """Cancel the run at the CI once the unit of work has committed, holding
    nothing while the CI is called; a run the CI does not stop is refused its
    reports, since its grading is finished.
    """

    async def cancel(after: Context) -> None:
        await _cancel_quietly(after, run)

    ctx.after_commit(cancel)


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
        if status_of(ctx, row) in UNFINISHED:
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
    gone = await lost(ctx, rows)
    return tuple(record(ctx, row, gone) for row in rows)


def record(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> GradingRecord:
    late = overdue_of(ctx, row, lost)
    return GradingRecord(
        id=row.id,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission_number=row.submission_number,
        submitted_at=row.submitted_at,
        publication=PublicationId(row.publication_id),
        stage=row.stage,
        attempt=row.attempt,
        status=GradingStatus.SYSTEM_ERROR if late is not None else GradingStatus(row.status),
        error=late or row.error,
        verdict=row.verdict,
        log=row.log_key is not None,
        progress=row.progress,
        queued_at=row.queued_at,
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


async def _stop_quietly(ctx: Context, row: Grading) -> None:
    """Cancel an unfinished grading a rejudge replaces, its run at the CI
    too when there is one; a run the CI does not stop is refused its
    reports, since the grading is cancelled.
    """
    try:
        if GradingStatus(row.status) in AT_THE_CI and row.run_id is not None:
            await ctx.forge.grading.cancel_run(RunId(row.run_id))
    except PortError as exc:
        log.warning(
            "gradings.replaced_run_not_stopped", grading=str(row.id), error=type(exc).__name__
        )
    finish(row, GradingStatus.CANCELLED, ctx.now)
