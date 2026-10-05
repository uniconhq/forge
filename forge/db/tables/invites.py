"""One row per invite, from the moment an organiser makes it until the person
accepts or declines it or the organisers withdraw it. An invite may name an
email address nobody has an account for yet, which the forge has nothing to
hold, which is why the row exists. `user_id` is who the invite is for once
that is known: at once for a username, and for an address when its owner
first lists their invites or opens the link its mail carried, matched
against the addresses the forge has confirmed for them.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id
from forge.domain.invites import Grant, InviteStatus, MailStatus

GRANTS = tuple(grant.value for grant in Grant)
STATUSES = tuple(status.value for status in InviteStatus)
MAIL_STATUSES = tuple(status.value for status in MailStatus)


class Invite(Base, Timestamped):
    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    scope: Mapped[str]
    """The org, contest or task the invite is to, by its keys joined
    (`Scope.path`)."""
    grants: Mapped[str]
    username: Mapped[str | None]
    email: Mapped[str | None]
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    invited_by_user_id: Mapped[int] = mapped_column(BigInteger)
    token_hash: Mapped[bytes] = mapped_column(unique=True)
    status: Mapped[str]
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    mail_status: Mapped[str]
    mailed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"grants in {GRANTS}", name="grants"),
        CheckConstraint(f"status in {STATUSES}", name="status"),
        CheckConstraint(f"mail_status in {MAIL_STATUSES}", name="mail_status"),
        CheckConstraint("(username IS NULL) <> (email IS NULL)", name="target"),
        Index("ix_invites_scope_created_at", "scope", "created_at"),
        Index("ix_invites_user_id_status", "user_id", "status"),
        Index(
            "ix_invites_email_unattached",
            "email",
            postgresql_where=text("user_id IS NULL AND status = 'pending'"),
        ),
    )
