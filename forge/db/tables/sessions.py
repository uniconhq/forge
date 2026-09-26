"""The platform's own sign-in sessions. Each row carries the user's credential
at the forge as ciphertext, so a copy of the table hands out no access.
"""

import uuid
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from sqlalchemy import BigInteger, Index
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base
from forge.domain.ids import new_id


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str]

    credential: Mapped[bytes]
    credential_expires_at: Mapped[datetime]

    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]

    ip: Mapped[IPv4Address | IPv6Address | None] = mapped_column(INET)
    user_agent: Mapped[str | None]

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )
