"""One grading of one submission at one stage. Everything about it is in this
row: what ran, where, its status, and the verdict as it was returned. Nothing
in the row names a forge object; the ids are the opaque ones the port hands
out.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

STATUSES = ("queued", "dispatching", "dispatched", "running", "done", "failed", "cancelled")
UNFINISHED = ("queued", "dispatching", "dispatched", "running")
WAIT_REASONS = ("waiting_for_compute", "ci_unavailable")


class Grading(Base, Timestamped):
    __tablename__ = "gradings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    workspace_id: Mapped[str]
    submission_id: Mapped[str]
    publication_id: Mapped[str]
    stage: Mapped[str]
    attempt: Mapped[int] = mapped_column(server_default=text("1"))

    status: Mapped[str]
    wait_reason: Mapped[str | None]
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
        CheckConstraint(
            f"wait_reason is null or wait_reason in {WAIT_REASONS}", name="wait_reason"
        ),
        UniqueConstraint("submission_id", "stage", "attempt"),
        Index("ix_gradings_workspace_id", "workspace_id"),
        Index(
            "ix_gradings_status_unfinished",
            "status",
            postgresql_where=text(f"status in {UNFINISHED}"),
        ),
    )
