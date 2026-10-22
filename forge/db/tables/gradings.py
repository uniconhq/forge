"""One grading of one submission. Everything about it is in this row: what
ran, where, its status, and the result as it was returned. Nothing
in the row names a forge object but by the opaque ids the port hands out.

`task_id` and `workspace_id` say whose submission of which task it is, the
entrant's whose workspace it is, and `submitted_by` which of its members
submitted it, none when a reconcile found it at the forge; and
`submission_number` and `submission_version` which one and the exact version
its files went in with, so the CI's run can be pinned to it and checked
against it. `submitted_at` is when the submit was taken, by the package's
clock, which is what a task's rate is counted by. `idempotency_key` is the
key the submit that made the row was sent with, on the row a submit makes
and on no retry or rejudge, unique for a workspace and task.
`callback_token_hash` is the SHA-256 of the one token its run reports back
with, stored at the insert.

`queued_at` is when the row was made, by the package's clock. `run_id` is
its run at the CI, `dispatched_at` when that run was started,
`started_at` when its harness fetched the envelope and `deadline_at` the
run's deadline from then. `progress` is the last progress its run reported,
`{"step", "done", "total"}`, and `error` a line for staff about a grading
that ended in `system_error`, kept when staff then cancel it with
`cancel_reason`, the sentence its contestant reads. `automatic` marks the
attempt the platform made by itself when the one before it ended in
`system_error`; a failure of an attempt so marked is not tried again by
itself, but waits for staff. `submission_limit` is the task's
`submissions.max` an `over_limit` attempt ended against, none on any other.
`regrade_id` names the regrade of many (`Regrade`) the attempt belongs to:
one it made, or a retry of one of those; none on any other.

A run task's grading is a row per pipeline (`pipeline`): `early`, which
every grading of today's tasks is too, `late`, with `early_id` naming the
early row it follows, none on a lone late row, and `save`, a
publication's own checking, which has no submission, so the submission's
columns are empty on it and on no other. A submission's attempts are
counted per pipeline (its line), a publication's save attempts once each.
`run_user` is the user the row's steps run as, while it is not finished
unique among the rows not finished. `taken_over_at` is when a save row that
passed let its publication take over, the task graded again when that
changed how it grades; none while that waits. `released_split` says the
grading takes its plan's released split (`RunPlan.for_split`): its early
row was made while the task's results were released, and its late row and
retries keep what their grading was made with.
"""

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    UniqueConstraint,
    and_,
    func,
    literal_column,
    not_,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.grading import GradingStatus
from forge.domain.ids import new_id

STATUSES = tuple(status.value for status in GradingStatus)
EARLY: Final = "early"
LATE: Final = "late"
SAVE: Final = "save"
PIPELINES = f"pipeline in ('{SAVE}', '{EARLY}', '{LATE}')"
SUBMISSION = (
    f"(pipeline = '{SAVE}') = (submission_id is null) and (pipeline = '{SAVE}' or ("
    "workspace_id is not null and submission_number is not null "
    "and submission_version is not null and submitted_at is not null))"
)


class Grading(Base, Timestamped):
    __tablename__ = "gradings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str]
    # Empty on a save row only, which the check `submission` keeps. Typed as
    # set, since every reader of them reads rows that are not a save's.
    workspace_id: Mapped[str] = mapped_column(nullable=True)
    submitted_by: Mapped[int | None] = mapped_column(BigInteger)
    submission_id: Mapped[str] = mapped_column(nullable=True)
    submission_number: Mapped[int] = mapped_column(nullable=True)
    submission_version: Mapped[str] = mapped_column(nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(nullable=True)
    publication_id: Mapped[str]
    pipeline: Mapped[str] = mapped_column(server_default=text(f"'{EARLY}'"))
    early_id: Mapped[uuid.UUID | None]
    released_split: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    run_user: Mapped[int | None]
    taken_over_at: Mapped[datetime | None]
    attempt: Mapped[int] = mapped_column(server_default=text("1"))
    idempotency_key: Mapped[str | None]

    status: Mapped[str]
    queued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    progress: Mapped[dict[str, Any] | None]
    result: Mapped[dict[str, Any] | None]
    log_key: Mapped[str | None]
    run_id: Mapped[str | None]
    callback_token_hash: Mapped[bytes | None]

    dispatched_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    deadline_at: Mapped[datetime | None]
    error: Mapped[str | None]
    cancel_reason: Mapped[str | None]
    automatic: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    submission_limit: Mapped[int | None]
    regrade_id: Mapped[uuid.UUID | None]

    __table_args__ = (
        CheckConstraint(f"status in {STATUSES}", name="status"),
        CheckConstraint(PIPELINES, name="pipeline"),
        CheckConstraint(SUBMISSION, name="submission"),
        UniqueConstraint("submission_id", "pipeline", "attempt"),
        Index(
            "ix_gradings_publication_id_attempt",
            "publication_id",
            "attempt",
            unique=True,
            postgresql_where=text(f"pipeline = '{SAVE}'"),
        ),
        Index(
            "ix_gradings_run_user",
            "run_user",
            unique=True,
            postgresql_where=text(
                "run_user is not null and status in ('queued', 'dispatched', 'running')"
            ),
        ),
        Index("ix_gradings_workspace_id", "workspace_id"),
        Index("ix_gradings_task_id_workspace_id", "task_id", "workspace_id"),
        Index("ix_gradings_regrade_id", "regrade_id"),
        Index(
            "ix_gradings_idempotency_key",
            "workspace_id",
            "task_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key is not null"),
        ),
    )


FRONT = Grading.pipeline == EARLY
"""The rows that are a submission's grading as its entrant knows it: every
row of today's tasks, and a run task's early row; not a late row, which
follows one, nor a save row, which has no submission."""

PASSED = and_(
    Grading.status == GradingStatus.DONE,
    not_(
        func.jsonb_path_exists(
            Grading.result,
            literal_column(
                """'$.runs[*] ? (@.verdict != "accepted" && @.verdict != "skipped")'::jsonpath"""
            ),
        )
    ),
)
"""The rows `done` whose result accepts every run, a skipped one among
them, as `step_data.all_accepted` tells it: a save row that lets its
publication take over."""
