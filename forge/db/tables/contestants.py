"""One row per person per contest, from the registration request to removal.
A registration before approval has no object at the forge, which is why the
row exists.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id
from forge.domain.registration import Status

STATUSES = tuple(status.value for status in Status)


class Contestant(Base, Timestamped):
    __tablename__ = "contestants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    contest_id: Mapped[str]
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str]
    registered_at: Mapped[datetime]
    eligibility: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    decided_at: Mapped[datetime | None]
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None]
    time_extension_seconds: Mapped[int] = mapped_column(server_default=text("0"))

    __table_args__ = (
        CheckConstraint(f"status in {STATUSES}", name="status"),
        UniqueConstraint("contest_id", "user_id"),
        Index("ix_contestants_contest_id_status", "contest_id", "status"),
    )
