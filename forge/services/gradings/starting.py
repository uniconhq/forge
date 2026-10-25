import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, and_, select

from forge.db.tables import Grading
from forge.db.tables.gradings import EARLY, SAVE
from forge.domain.errors import (
    Conflict,
    Forbidden,
    Misconfigured,
    NotFound,
    PortError,
    Rejected,
)
from forge.domain.grading import GradingStatus, PipelineRun
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import PublicationId, RunId, TaskId, WorkspaceId, new_id
from forge.domain.publication_record import Publication
from forge.domain.submissions import Submitted
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import org_accounts, pipelines
from forge.services.credentials import CannotDecrypt
from forge.services.gradings.lookups import changed, find, finish, org_of
from forge.services.gradings.records import (
    ACCOUNT_NOT_READY,
    DISPATCH_UNKNOWN,
    NO_ANSWER,
    NO_RUN,
    PUBLICATION_GONE,
    REFUSED,
)

log = get_logger(__name__)


def new_row(
    ctx: Context,
    *,
    task: TaskId,
    workspace: str | None,
    submission: str | None,
    number: int | None,
    version: str | None,
    submitted_at: datetime | None,
    publication: PublicationId,
    attempt: int,
    key: str | None,
    submitted_by: int | None,
    automatic: bool = False,
    regrade: uuid.UUID | None = None,
    over_limit: int | None = None,
    pipeline: str = EARLY,
    early: uuid.UUID | None = None,
    unwaited: bool = False,
    released_split: bool = False,
) -> Grading:
    """A new `queued` grading, added to the unit of work, whose run starts once the unit of work
    commits, in the background when it is `unwaited`, since nobody asking
    waits on it; a start that never runs leaves the row `queued`, which a
    read finds overdue and retries (`notice`). `automatic` when the platform
    makes it by itself, retrying the
    attempt before it, and `regrade` naming the regrade of many it belongs
    to. With `over_limit`, the task's `submissions.max` its entrant has no
    room left under, it is ended `over_limit` at once instead, and nothing
    runs. A task's late row names the `early` row it follows, and a
    save row has no submission. `released_split` when the grading takes
    its plan's released split, fixed for the row (`RunPlan.for_split`).
    """
    row = Grading(
        id=new_id(),
        task_id=task,
        workspace_id=workspace,
        submitted_by=submitted_by,
        submission_id=submission,
        submission_number=number,
        submission_version=version,
        submitted_at=submitted_at,
        publication_id=publication,
        attempt=attempt,
        idempotency_key=key,
        status=GradingStatus.QUEUED,
        queued_at=ctx.now,
        automatic=automatic,
        regrade_id=regrade,
        pipeline=pipeline,
        early_id=early,
        released_split=released_split,
    )
    ctx.db.add(row)
    if over_limit is not None:
        row.submission_limit = over_limit
        finish(ctx, row, GradingStatus.OVER_LIMIT)
        return row
    changed(ctx, row)
    grading = row.id

    async def start_it(later: Context) -> None:
        await start(later, grading)

    if unwaited:
        ctx.in_background(start_it)
    else:
        ctx.after_commit(start_it)
    return row


async def start(ctx: Context, grading: uuid.UUID) -> None:
    """Start the run of a grading that is still `queued`, as its org's
    account, and record it: the grading is then `dispatched`, waiting for a
    machine. A start that fails ends the grading in `system_error`, saying
    why in the platform's words; what the CI said goes to the log.

    The call to the CI holds neither a connection nor a lock, since a CI
    that asks may ask the platform about the grading while the start is
    under way, and a rush of starts must leave it a connection to answer on. So what the
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
    await _start_pipeline(ctx, grading, row, account)


async def _superseded(ctx: Context, row: Grading) -> bool:
    """Whether a later attempt on the row's line exists."""
    later = await ctx.db.scalar(
        select(Grading.id).where(_on_line(row), Grading.attempt > row.attempt).limit(1)
    )
    return later is not None


def _on_line(row: Grading) -> ColumnElement[bool]:
    """The rows on `row`'s line: its submission's of its pipeline, or a save
    row's publication's."""
    if row.pipeline == SAVE:
        return and_(Grading.pipeline == SAVE, Grading.publication_id == row.publication_id)
    return and_(Grading.pipeline == row.pipeline, Grading.submission_id == row.submission_id)


async def _start_pipeline(
    ctx: Context, grading: uuid.UUID, row: Grading, account: AsOrgAccount
) -> None:
    """`start` once the org's account is read: the row's steps' settings
    minted, a user taken for it and committed, then its pipeline started on
    the org's dispatch repository, which is made and activated the first time a
    start finds it missing. A pipeline with nothing left to run is not
    started: the row is concluded at once from what the store holds, the
    platform's images needed or not.
    """
    try:
        run = await pipelines.prepare(ctx, row)
    except (pipelines.NoImage, pipelines.ImageWithdrawn) as exc:
        await _not_started(ctx, grading, exc, str(exc))
        return
    except NotFound as exc:
        await _not_started(ctx, grading, exc, PUBLICATION_GONE)
        return
    except (Misconfigured, Forbidden, Conflict) as exc:
        await _not_started(ctx, grading, exc, NO_RUN)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    if run is None:
        return
    await ctx.db.commit()
    try:
        found = await _start_on_dispatch(ctx, account, run)
    except (PortError, CannotDecrypt) as exc:
        reason = _start_failure(exc) if isinstance(exc, PortError) else ACCOUNT_NOT_READY
        await _not_started(ctx, grading, exc, reason)
        return
    held = await find(ctx, grading, lock=True)
    if held is None or held.status != GradingStatus.QUEUED or await _superseded(ctx, held):
        log.info("gradings.start_overtaken", grading=str(grading), run=found)
        await _cancel_quietly(ctx, found)
        return
    held.status = GradingStatus.DISPATCHED
    held.run_id = found
    held.dispatched_at = ctx.now
    changed(ctx, held)
    log.info("gradings.dispatched", grading=str(held.id), run=found, pipeline=held.pipeline)


async def _start_on_dispatch(ctx: Context, account: AsOrgAccount, run: PipelineRun) -> RunId:
    """Start the pipeline, making and activating the org's dispatch
    repository first when the CI does not know it, and signing the org's
    account in again once when the CI refuses it, so a login the CI lost
    heals at the next start. Nothing is held while the CI is called.
    """
    try:
        return await _start_signed_in(ctx, account, run)
    except NotFound:
        log.info("gradings.dispatch_made", org=run.org)
        await ctx.forge.content.create_dispatch(run.org)
        try:
            await ctx.forge.grading.activate_dispatch(account, run.org)
        except Forbidden:
            account = await org_accounts.renew(ctx, account)
            await ctx.db.commit()
            await ctx.forge.grading.activate_dispatch(account, run.org)
        return await _start_signed_in(ctx, account, run)


async def _start_signed_in(ctx: Context, account: AsOrgAccount, run: PipelineRun) -> RunId:
    try:
        return await ctx.forge.grading.start_pipeline(account, run)
    except Forbidden:
        account = await org_accounts.renew(ctx, account)
        await ctx.db.commit()
        return await ctx.forge.grading.start_pipeline(account, run)


async def _not_started(ctx: Context, grading: uuid.UUID, exc: Exception, reason: str) -> None:
    """End the grading in `system_error` with `reason`, unless it moved on
    meanwhile. Its retry by itself is left to the first read that finds it
    `START_RETRY_WAIT` after (`notice`).
    """
    log.warning(
        "gradings.start_failed", grading=str(grading), error=type(exc).__name__, detail=str(exc)
    )
    row = await find(ctx, grading, lock=True)
    if row is not None and row.status == GradingStatus.QUEUED:
        finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=reason)


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
            return DISPATCH_UNKNOWN
    return NO_ANSWER


def queue_submission(
    ctx: Context,
    *,
    task: TaskId,
    workspace: WorkspaceId,
    submission: Submitted,
    publication: Publication,
    key: str | None,
    at: datetime,
    submitted_by: int | None,
    released_split: bool,
) -> Grading:
    """The submission's queued grading, attempt 1, against `publication`,
    made by the member `submitted_by`, none when that is not known, taking
    the released split when the task's results are released now
    (`released_split`).
    """
    return new_row(
        ctx,
        task=task,
        workspace=workspace,
        submission=submission.id,
        number=submission.number,
        version=submission.version,
        submitted_at=at,
        publication=publication.id,
        attempt=1,
        key=key,
        submitted_by=submitted_by,
        released_split=released_split,
    )
