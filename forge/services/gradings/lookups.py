import uuid

from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.gradings import EARLY, SAVE
from forge.domain.errors import NotFound
from forge.domain.grading import (
    PLATFORM_POOL,
    GradingRun,
    GradingStatus,
    RunSpec,
    callback_token,
    envelope_key,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import OrgId, PublicationId, SubmissionId, TaskId, VersionId, WorkspaceId
from forge.domain.live import Nudge, NudgeKind
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.plans import Plan
from forge.domain.publication_record import Publication
from forge.domain.roles import task_scope
from forge.runtime.context import Context
from forge.services import pipelines
from forge.services.gradings.records import CALLBACK_PATH, ENVELOPE_PATH


def callback_token_of(ctx: Context, row: Grading) -> str:
    """The token the grading's run reports back with."""
    return callback_token(ctx.settings.token_encryption_key_bytes, row.id)


def envelope_key_of(ctx: Context, row: Grading) -> str:
    """The key the URL of the envelope of the grading's run carries."""
    return envelope_key(ctx.settings.token_encryption_key_bytes, row.id)


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


async def plan_of(ctx: Context, run: GradingRun) -> Plan:
    """The plan of the publication `run` grades against, as it froze it.
    `NotFound` or `Forbidden` when the file cannot be read, and pydantic's
    `ValidationError` when it is not a plan.
    """
    found = await ctx.forge.content.read_file(
        PLATFORM, run.task, PLAN_PATH, at=run.publication_version
    )
    return Plan.from_bytes(found.content)


def spec_of(ctx: Context, plan: Plan) -> RunSpec:
    """What a run of `plan` runs: the harness image it names and the
    platform's clone image. `ImageWithdrawn` when a step of the plan runs
    an image the platform has withdrawn, which the harness would run.
    """
    for step in plan.steps:
        if ctx.settings.withdraws(step.image):
            raise pipelines.ImageWithdrawn(pipelines.WITHDRAWN.format(image=step.image))
    return RunSpec(harness_image=plan.harness_image, clone_image=ctx.settings.clone_image)


def org_of(row: Grading) -> OrgId:
    return OrgId(task_scope(TaskId(row.task_id)).org)


def finish(ctx: Context, row: Grading, status: GradingStatus, *, error: str | None = None) -> None:
    """End the grading with `status`, waiting for nothing more. A save row
    also tells the task's organisers that its checks ended, naming the
    publication, with no entrant and no contest.
    """
    row.status = status
    row.finished_at = ctx.now
    row.error = error
    changed(ctx, row)
    if row.pipeline == SAVE:
        ctx.nudge(
            Nudge(
                NudgeKind.CHECKS,
                row.publication_id,
                scope=task_scope(TaskId(row.task_id)),
            )
        )


def changed(ctx: Context, row: Grading, *, organisers_only: bool = False) -> None:
    """Nudge whoever may hear of the grading that it moved, once the unit of
    work commits: the members of the entrant whose workspace it grades, and the
    organisers who observe its task. Every write of a grading's status or
    progress calls this, which is what moves a contestant's submissions
    while they watch; a grading is the platform's own and no push from the
    forge will ever name one. Progress goes to the organisers alone
    (`organisers_only`): a per-test step reports it once a test, hidden ones
    too, so a nudge to the row on each would count and time a hidden
    group's tests (TASK-FORMAT.md section 3.5). So does every move of a run
    task's late or save row, which no entrant is told of (11.8), but for a
    late row's end once its task has revealed (`told_late`).
    """
    owner = (
        None
        if organisers_only or row.pipeline != EARLY
        else ctx.forge.workspaces.owner_of(WorkspaceId(row.workspace_id))
    )
    ctx.nudge(
        Nudge(
            NudgeKind.GRADING,
            str(row.id),
            entrant=str(owner.entrant_id) if owner is not None else None,
            scope=task_scope(TaskId(row.task_id)),
        )
    )


def told_late(ctx: Context, late: Grading) -> None:
    """Nudge the members of the entrant whose submission a late row grades
    that their grading moved, by its early row, the grading they know, once
    the unit of work commits. Only once the task has revealed, or while
    its results are released for a grading made before the release, which
    the caller checks: their result then reads the late row
    (`submissions`), and otherwise no entrant hears of one (`changed`). A
    grading made while released ran what a release shows early, so its
    late row's end shows nothing new, and a nudge would only tell when the
    hidden runs ended (stage 5 section 6 item 37).
    """
    owner = ctx.forge.workspaces.owner_of(WorkspaceId(late.workspace_id))
    ctx.nudge(
        Nudge(
            NudgeKind.GRADING,
            str(late.early_id),
            entrant=str(owner.entrant_id),
            scope=task_scope(TaskId(late.task_id)),
        )
    )


def staff_cancelled(row: Grading) -> bool:
    """Whether staff ended the grading's submission by cancelling it, which,
    while it is the latest attempt, is final: a retry, a rejudge and a
    save's regrade grade it no more, and, unless an earlier attempt of it
    finished with a result, it is left out of what a task's
    `submissions.max` counts.
    """
    return row.status == GradingStatus.CANCELLED and row.cancel_reason is not None
