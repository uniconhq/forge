"""Teams arrive with feature 14: a `teams` row per team, with its leader, and
a `team_members` row per person asked in, asking, in, or gone. A person is a
member of one team in a contest at most, and has one live row per team, each
held by the database. The tables are new, so there is nothing to carry
across; going back drops them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06 15:00:00
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("leader_user_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
    )
    op.create_index(
        "ix_teams_contest_id_name", "teams", ["contest_id", sa.text("lower(name)")], unique=True
    )
    op.create_table(
        "team_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("left_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('invited', 'requested', 'member', 'left')",
            name=op.f("ck_team_members_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_members")),
    )
    op.create_index(
        "ix_team_members_one_team",
        "team_members",
        ["contest_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'member'"),
    )
    op.create_index(
        "ix_team_members_once_a_team",
        "team_members",
        ["team_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'left'"),
    )
    op.create_index("ix_team_members_contest_id_user_id", "team_members", ["contest_id", "user_id"])


def downgrade() -> None:
    op.drop_table("team_members")
    op.drop_table("teams")
