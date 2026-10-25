import uuid
from collections.abc import Collection, Iterable, Sequence
from typing import cast

from sqlalchemy import func, select

from forge.db.tables import Grading
from forge.domain.grading import GradingStatus
from forge.domain.ids import PublicationId, TaskId, WorkspaceId
from forge.runtime.context import Context
from forge.services.gradings.records import GradingRecord, Listed
from forge.services.gradings.row_state import HAS_RESULT, has_result, overdue_of
from forge.services.gradings.starting import new_row


def record(
    ctx: Context,
    row: Grading,
    lost: Collection[uuid.UUID] = (),
    *,
    latest: bool,
    last_good: int | None = None,
    retry_limit: int | None = None,
) -> GradingRecord:
    """The grading as an organiser reads it, `latest` saying whether it is
    its submission's latest attempt and `last_good` the latest earlier one
    that finished with a result, kept only on a latest attempt that has not
    finished with one itself, since that is when it counts; and
    `retry_limit` the `submissions.max` a retry of it now would end over.
    """
    late = overdue_of(ctx, row, lost)
    status = GradingStatus.SYSTEM_ERROR if late is not None else GradingStatus(row.status)
    return GradingRecord(
        id=row.id,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission_number=row.submission_number,
        submitted_at=row.submitted_at,
        publication=PublicationId(row.publication_id),
        attempt=row.attempt,
        latest=latest,
        submitted_by=row.submitted_by,
        status=status,
        error=late or row.error,
        result=row.result,
        queued_at=row.queued_at,
        dispatched_at=row.dispatched_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        deadline_at=row.deadline_at,
        cancel_reason=row.cancel_reason,
        automatic=row.automatic,
        last_good=last_good if latest and not has_result(row) else None,
        limit=row.submission_limit if status is GradingStatus.OVER_LIMIT else None,
        retry_limit=retry_limit if latest else None,
        regrade=row.regrade_id,
        pipeline=cast(Listed, row.pipeline),
        early=row.early_id,
    )


async def latest_of(ctx: Context, rows: Iterable[Grading]) -> frozenset[uuid.UUID]:
    """The gradings among `rows`, none a save row, that are the latest
    attempt on their line, worked out over every attempt of it, read or not.
    """
    listed = list(rows)
    submissions = sorted({row.submission_id for row in listed})
    if not submissions:
        return frozenset()
    found = await ctx.db.execute(
        select(Grading.submission_id, Grading.pipeline, func.max(Grading.attempt))
        .where(Grading.submission_id.in_(submissions))
        .group_by(Grading.submission_id, Grading.pipeline)
    )
    last = {(submission, pipeline): attempt for submission, pipeline, attempt in found.all()}
    return frozenset(
        row.id for row in listed if row.attempt == last.get((row.submission_id, row.pipeline))
    )


async def last_goods(ctx: Context, rows: Iterable[Grading]) -> dict[uuid.UUID, int]:
    """For each grading among `rows`, none a save row, the latest earlier
    attempt on its line that finished with a result, worked out over every
    attempt of it, read or not; a grading with none is left out.
    """
    listed = list(rows)
    submissions = sorted({row.submission_id for row in listed})
    if not submissions:
        return {}
    found = await ctx.db.execute(
        select(Grading.submission_id, Grading.pipeline, Grading.attempt).where(
            Grading.submission_id.in_(submissions), HAS_RESULT
        )
    )
    done: dict[tuple[str, str], list[int]] = {}
    for submission, pipeline, attempt in found.all():
        done.setdefault((submission, pipeline), []).append(attempt)
    goods: dict[uuid.UUID, int] = {}
    for row in listed:
        line = (row.submission_id, row.pipeline)
        earlier = [attempt for attempt in done.get(line, ()) if attempt < row.attempt]
        if earlier:
            goods[row.id] = max(earlier)
    return goods


def _next_attempt(
    ctx: Context,
    row: Grading,
    publication: PublicationId,
    attempts: Sequence[Grading],
    *,
    automatic: bool = False,
    regrade: uuid.UUID | None = None,
    over_limit: int | None = None,
    released_split: bool | None = None,
) -> Grading:
    """The next attempt on `row`'s line against `publication`, in the
    regrade `regrade` when given and otherwise in the one `row` belongs to,
    ended at once over the limit with `over_limit`, taking the split
    `released_split` says, and with none, the one `row` took, as a retry
    does.
    """
    return new_row(
        ctx,
        task=TaskId(row.task_id),
        workspace=row.workspace_id,
        submission=row.submission_id,
        number=row.submission_number,
        version=row.submission_version,
        submitted_at=row.submitted_at,
        publication=publication,
        attempt=max(other.attempt for other in attempts) + 1,
        key=None,
        submitted_by=row.submitted_by,
        automatic=automatic,
        regrade=regrade or row.regrade_id,
        over_limit=over_limit,
        pipeline=row.pipeline,
        early=row.early_id,
        released_split=row.released_split if released_split is None else released_split,
    )
