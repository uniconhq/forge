"""Teams and their members, one contest at a time. A team is not a forge role
and has no object at the forge: its workspace is named by its id
(`TeamOwner`), and who may reach it is each member's own access, given and
taken away as the membership changes. Leaders, join requests and pending
members have nothing at the forge either, which is why the rows exist.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id
from forge.domain.teams import MemberStatus

STATUSES = tuple(status.value for status in MemberStatus)


class Team(Base, Timestamped):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    contest_id: Mapped[str]
    name: Mapped[str]
    leader_user_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_teams_contest_id_name", "contest_id", text("lower(name)"), unique=True),
    )


class TeamMember(Base, Timestamped):
    __tablename__ = "team_members"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    contest_id: Mapped[str]
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str]
    joined_at: Mapped[datetime | None]
    """When they became a member, the order a team's next leader is chosen
    in."""
    left_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"status in {STATUSES}", name="status"),
        Index(
            "ix_team_members_one_team",
            "contest_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'member'"),
        ),
        Index(
            "ix_team_members_once_a_team",
            "team_id",
            "user_id",
            unique=True,
            postgresql_where=text("status <> 'left'"),
        ),
        Index("ix_team_members_contest_id_user_id", "contest_id", "user_id"),
    )
