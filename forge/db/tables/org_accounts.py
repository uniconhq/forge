"""One row per org, by its id: its service account at the forge, and what
the account holds at the CI, which only the CI's implementation reads. The
forge credential, the CI state and the secret the org's event push is
signed with are AES-256-GCM ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`,
the way a session's credential is, so a copy of the table hands out no
access. The account's password at the forge is never written.
"""

import uuid

from sqlalchemy import BigInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id


class OrgAccount(Base, Timestamped):
    __tablename__ = "org_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    org_id: Mapped[str]
    forge_user_id: Mapped[int | None] = mapped_column(BigInteger)
    username: Mapped[str]
    forge_token: Mapped[bytes]
    ci_state: Mapped[bytes]
    event_secret: Mapped[bytes]

    __table_args__ = (UniqueConstraint("org_id"),)
