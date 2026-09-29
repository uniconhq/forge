"""The record of making something at the forge. Making an org, a contest, a
task, a contestant's workspace or the place they submit one task to takes
several calls and can fail halfway; the forge only knows whether a thing
exists, so the last completed step lives here and a rerun starts after it; a
failed row names the step it stopped at in `failed_step` and the reason in
`error`. A task's activation at the CI is recorded the same way.
`payload` is the request that started the job, what the steps need that the
forge does not hold: the description, who asked.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

KINDS = ("org", "contest", "task", "workspace", "submission_place", "activation")
STATUSES = ("pending", "running", "ready", "failed")


class Provisioning(Base, Timestamped):
    __tablename__ = "provisioning"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str]
    target_id: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    last_step: Mapped[str | None]
    error: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    ready_at: Mapped[datetime | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    retry_at: Mapped[datetime | None]
    failed_step: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(f"kind in {KINDS}", name="kind"),
        CheckConstraint(f"status in {STATUSES}", name="status"),
        UniqueConstraint("kind", "target_id"),
        Index(
            "ix_provisioning_status_waiting",
            "status",
            postgresql_where=text("status in ('pending', 'failed')"),
        ),
    )
