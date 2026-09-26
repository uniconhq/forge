"""Notebook servers the platform started for a contestant, and which task
input each one serves.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

STATUSES = ("spawning", "running", "stopped", "failed")


class JupyterSession(Base, Timestamped):
    __tablename__ = "jupyter_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str]
    input_id: Mapped[str]
    pool_id: Mapped[uuid.UUID | None]
    server_name: Mapped[str]
    status: Mapped[str]
    spawned_at: Mapped[datetime]
    last_activity_at: Mapped[datetime | None]
    stopped_at: Mapped[datetime | None]
    stop_reason: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(f"status in {STATUSES}", name="status"),
        UniqueConstraint("user_id", "task_id", "input_id"),
    )
