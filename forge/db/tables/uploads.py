"""Files on their way into a repository at the forge, and the record of the
ones a commit took. The bytes are never here and never pass through the
platform: the row says what was declared, and the forge says whether it
holds it (`forge.services.uploads`).

A row whose upload no commit took is removed once the expiry passes, the
next time its owner asks for a slot; the bytes it named are the forge's to
collect once nothing points at them.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

PURPOSES = ("submission", "task_file")
STATUSES = ("waiting", "verified", "consumed")


class Upload(Base, Timestamped):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    purpose: Mapped[str]
    task_id: Mapped[str | None]
    input_id: Mapped[str | None]
    repo_path: Mapped[str | None]
    """Where in the task's own repository an organiser's file goes. None for
    a submission's file, whose path comes from its input and name."""
    filename: Mapped[str]
    content_type: Mapped[str | None]
    size: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str]
    """The file's SHA-256 in lowercase hex, as the browser declared it and
    as the forge checked it when the bytes arrived."""
    status: Mapped[str]
    consumed_by: Mapped[str | None]
    expires_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint(f"purpose in {PURPOSES}", name="purpose"),
        CheckConstraint(f"status in {STATUSES}", name="status"),
        Index("ix_uploads_owner_user_id_status", "owner_user_id", "status"),
        Index("ix_uploads_expires_at", "expires_at"),
    )
