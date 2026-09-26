"""An invitation to a scope. It may name an email address with no account yet,
so it cannot live at the forge.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

SCOPE_KINDS = ("org", "contest", "task", "team")
STATUSES = ("pending", "accepted", "declined", "revoked", "expired")


class Invite(Base, Timestamped):
    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    scope_kind: Mapped[str]
    scope_id: Mapped[str]
    target_email: Mapped[str | None]
    target_user_id: Mapped[int | None] = mapped_column(BigInteger)
    grants: Mapped[dict[str, Any]]
    token_hash: Mapped[bytes] = mapped_column(unique=True)
    invited_by_user_id: Mapped[int] = mapped_column(BigInteger)
    message: Mapped[str | None]
    expires_at: Mapped[datetime]
    status: Mapped[str]
    decided_at: Mapped[datetime | None]
    accepted_by_user_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint(f"scope_kind in {SCOPE_KINDS}", name="scope_kind"),
        CheckConstraint(f"status in {STATUSES}", name="status"),
        CheckConstraint(
            "target_email is not null or target_user_id is not null", name="has_target"
        ),
        Index("ix_invites_target_email", "target_email"),
        Index("ix_invites_target_user_id", "target_user_id"),
        Index("ix_invites_scope_kind_scope_id_status", "scope_kind", "scope_id", "status"),
    )
