"""Contest teams and their members. A contest team is not a role at the forge:
leaders, join requests and pending members have no object there.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

MEMBER_STATUSES = ("requested", "active", "left", "removed")


class Team(Base, Timestamped):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    contest_id: Mapped[str]
    name: Mapped[str]
    slug: Mapped[str]
    leader_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger)
    deleted_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("contest_id", "slug"),)


class TeamMember(Base, Timestamped):
    __tablename__ = "team_members"

    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contest_id: Mapped[str]
    status: Mapped[str]
    requested_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    workspace_synced_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"status in {MEMBER_STATUSES}", name="status"),
        Index(
            "uq_team_members_contest_id_user_id_active",
            "contest_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
