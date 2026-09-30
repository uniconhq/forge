"""The gradings of submissions: the rows a submit makes, and what a run of
one is in the port's words.

A grading is one row per submission, stage and attempt. Each row is
inserted `queued` with the SHA-256 of its first run's callback token, so the
token itself is never stored.
"""

import builtins
import uuid
from datetime import datetime

from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.definitions import TaskDefinition, Trigger
from forge.domain.errors import NotFound
from forge.domain.grading import (
    PLATFORM_POOL,
    GradingRun,
    GradingStatus,
    callback_token,
    envelope_key,
    token_hash,
)
from forge.domain.ids import (
    OrgName,
    PublicationId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
    new_id,
)
from forge.domain.publications import Publication
from forge.domain.roles import task_scope
from forge.domain.submissions import Submitted
from forge.runtime.context import Context

NO_SUCH_GRADING = "There is no such grading."

CI_CONFIG_PATH = "/api/v1/ci/config"
"""Where the CI asks what a run is, the configuration extension, under the
platform's internal URL."""
ENVELOPE_PATH = "/api/v1/gradings/{grading}/envelope"
"""Where a run fetches its envelope, under the machine URL, with the envelope
key as `?key=`."""
CALLBACK_PATH = "/api/v1/gradings/{grading}/callback"
"""Where a run reports, under the machine URL."""


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
