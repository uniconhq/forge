"""The gradings of submissions: the rows a submit makes.

A grading is one row per submission, stage and attempt. Each row is
inserted `queued` with the SHA-256 of its first run's callback token, so the
token itself is never stored.
"""

import builtins
from datetime import datetime

from forge.db.tables import Grading
from forge.domain.definitions import TaskDefinition, Trigger
from forge.domain.grading import GradingStatus, callback_token, token_hash
from forge.domain.ids import (
    PublicationId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
    new_id,
)
from forge.domain.publications import Publication
from forge.domain.submissions import Submitted
from forge.runtime.context import Context


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
    )
    token = callback_token(ctx.settings.token_encryption_key_bytes, row.id, run=0)
    row.callback_token_hash = token_hash(token)
    ctx.db.add(row)
    return row


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
