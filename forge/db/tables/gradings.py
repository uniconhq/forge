"""One grading of one submission at one stage. Everything about it is in this
row: what ran, where, its status, and the verdict as it was returned. Nothing
in the row names a forge object but by the opaque ids the port hands out.

`task_id` and `workspace_id` say whose submission of which task it is, and
`submission_number` and `submission_version` which one and the exact version
its files went in with, so the CI's run can be pinned to it and checked
against it. `submitted_at` is when the submit was taken, by the package's
clock, which is what a task's rate is counted by. `idempotency_key` is the
key the submit that made the row was sent with, on the rows a submit makes
and on no retry or rejudge, unique for a workspace, task and stage.
`wait_reason` is a short line an organiser reads to see why a grading is not
moving. `callback_token_hash` is the SHA-256 of the one token its run
reports back with, stored at the insert.

`queued_at` is when the grading last entered the queue, at its insert or at
its requeue, and a run carrying its id started before then is not its run.
`start_failures` counts the starts that failed since, and `retry_at` is when
a grading whose start failed is next tried. `run_id` is its run at the CI,
`dispatched_at` when that run was started and `deadline_at` its deadline:
the start's, until the harness fetches the envelope and it becomes the
run's own. `requeues` counts the times a run of it ended without a verdict
and it went back to the queue, which happens once. `progress` is the last
progress its run reported, `{"step", "done", "total"}`, and `error` a line
for staff about a grading that ended in `system_error`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.grading import UNFINISHED, GradingStatus
from forge.domain.ids import new_id

STATUSES = tuple(status.value for status in GradingStatus)
UNFINISHED_STATUSES = tuple(status.value for status in UNFINISHED)


class Grading(Base, Timestamped):
    __tablename__ = "gradings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str]
    workspace_id: Mapped[str]
    submission_id: Mapped[str]
    submission_number: Mapped[int]
    submission_version: Mapped[str]
    submitted_at: Mapped[datetime]
    publication_id: Mapped[str]
    stage: Mapped[str]
    attempt: Mapped[int] = mapped_column(server_default=text("1"))
    idempotency_key: Mapped[str | None]

    status: Mapped[str]
    wait_reason: Mapped[str | None]
    queued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    start_failures: Mapped[int] = mapped_column(server_default=text("0"))
    retry_at: Mapped[datetime | None]
    requeues: Mapped[int] = mapped_column(server_default=text("0"))
    progress: Mapped[dict[str, Any] | None]
    verdict: Mapped[dict[str, Any] | None]
    log_key: Mapped[str | None]
    run_id: Mapped[str | None]
    compute_id: Mapped[uuid.UUID | None]
    callback_token_hash: Mapped[bytes | None]
    selected_at: Mapped[datetime | None]

    dispatched_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    deadline_at: Mapped[datetime | None]
    error: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(f"status in {STATUSES}", name="status"),
        UniqueConstraint("submission_id", "stage", "attempt"),
        Index("ix_gradings_workspace_id", "workspace_id"),
        Index("ix_gradings_task_id_workspace_id", "task_id", "workspace_id"),
        Index(
            "ix_gradings_idempotency_key",
            "workspace_id",
            "task_id",
            "idempotency_key",
            "stage",
            unique=True,
            postgresql_where=text("idempotency_key is not null"),
        ),
        Index(
            "ix_gradings_status_unfinished",
            "status",
            postgresql_where=text(f"status in {UNFINISHED_STATUSES}"),
        ),
    )
