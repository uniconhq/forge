"""An organiser's resumable transfer, before Forgejo verifies its bytes."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id
from forge.domain.uploads import PartUploadStatus


class PartUpload(Base, Timestamped):
    __tablename__ = "part_uploads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str | None]
    workflow_id: Mapped[str | None]
    path: Mapped[str]
    digest: Mapped[str]
    size: Mapped[int] = mapped_column(BigInteger)
    part_size: Mapped[int] = mapped_column(BigInteger)
    upload_id: Mapped[str | None]
    status: Mapped[PartUploadStatus] = mapped_column(Text)
    reason: Mapped[str | None]
    deadline_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint("(task_id is null) <> (workflow_id is null)", name="place"),
        CheckConstraint(
            "status in ('open','finishing','stored','refused','aborted')", name="status"
        ),
        Index("ix_part_uploads_owner_user_id_status", "owner_user_id", "status"),
        Index("ix_part_uploads_deadline_at", "deadline_at"),
    )
