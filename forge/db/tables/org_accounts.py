"""One row per org: its service account at the forge and at the CI. The
two credentials and the secret the org's event push is signed with are
AES-256-GCM ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, the way a
session's credential is, so a copy of the table hands out no access. The
account's password at the forge is never written: the package sets a fresh
one with its administrator rights whenever it has to sign the account in
again, and throws it away.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id


class OrgAccount(Base, Timestamped):
    __tablename__ = "org_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    org_name: Mapped[str]
    forge_user_id: Mapped[int | None] = mapped_column(BigInteger)
    username: Mapped[str]
    forge_token: Mapped[bytes]
    ci_token: Mapped[bytes]
    ci_user_id: Mapped[int | None] = mapped_column(BigInteger)
    event_secret: Mapped[bytes]
    last_kept_alive_at: Mapped[datetime | None]
    keepalive_error: Mapped[str | None]

    __table_args__ = (UniqueConstraint("org_name"),)
