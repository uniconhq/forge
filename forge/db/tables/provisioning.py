"""The record of making something at the forge. Making an org, a contest, a
task or a workspace takes several calls and can fail halfway; the forge only
knows whether a thing exists, so the last completed step lives here and a
rerun starts after it.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

KINDS = ("org", "contest", "task", "workspace")
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
