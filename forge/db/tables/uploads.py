"""Files a browser uploaded to the object store that are not yet part of a
submission. The expiry lets a sweeper remove what was never used.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

PURPOSES = ("submission", "asset")
STATUSES = ("presigned", "uploaded", "verified", "consumed", "rejected", "expired")


class Upload(Base, Timestamped):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    purpose: Mapped[str]
    task_id: Mapped[str | None]
    input_id: Mapped[str | None]
    object_key: Mapped[str] = mapped_column(unique=True)
    filename: Mapped[str]
    content_type: Mapped[str | None]
    declared_size: Mapped[int] = mapped_column(BigInteger)
    actual_size: Mapped[int | None] = mapped_column(BigInteger)
    digest: Mapped[bytes | None]
    status: Mapped[str]
    multipart_upload_id: Mapped[str | None]
    consumed_by: Mapped[str | None]
    expires_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint(f"purpose in {PURPOSES}", name="purpose"),
        CheckConstraint(f"status in {STATUSES}", name="status"),
        Index("ix_uploads_owner_user_id_status", "owner_user_id", "status"),
        Index("ix_uploads_expires_at", "expires_at"),
    )
