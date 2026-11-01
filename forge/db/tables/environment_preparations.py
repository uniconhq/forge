"""A saved task waiting for its immutable package environment."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id


class EnvironmentPreparation(Base, Timestamped):
    __tablename__ = "environment_preparations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str]
    version: Mapped[str]
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str]
    reason: Mapped[str | None]
    deadline_at: Mapped[datetime]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("status in ('preparing','ready','refused','replaced')", name="status"),
        Index("ix_environment_preparations_task_id_created_at", "task_id", "created_at"),
    )
