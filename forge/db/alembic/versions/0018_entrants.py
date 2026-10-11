"""Every entry is an entrant: one or more people who enter a contest together,
registered, decided and extended as one, with a row for each time someone was
in one. The `contestants`, `teams` and `team_members` tables go, and
`entrants` and `entrant_members` take their place. An invite may now ask
someone into an entrant, as a member's (`grants = member`) or as an
organiser's, which names the entrant, and a grading says which member
submitted it.

Nothing runs in production yet, so nothing is carried across: a workspace was
a person's (`u<id>`) or a team's (`team.<id>`), and no entrant owns either,
so the rows that name one go with the tables, so that no row names a
workspace nobody owns: every grading, every mark, and every upload of a
submission, whose place was such a workspace. An organiser's invite to a
contest, accepted or not, stays.

Going back puts the three tables back empty, as they stood at 0017, drops
the new ones, the invites into an entrant and what invites and gradings
gained, and empties gradings, marks and the uploads of submissions again,
since their workspaces were entrants'.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-11 12:00:00
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
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


def _forget_workspaces() -> None:
    op.execute("DELETE FROM marks")
    op.execute("DELETE FROM gradings")
    op.execute("DELETE FROM uploads WHERE purpose = 'submission'")


def upgrade() -> None:
    _forget_workspaces()
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("contestants")
    op.create_table(
        "entrants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("registered_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "time_extension_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("extension_tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected', 'withdrawn', 'removed')",
            name=op.f("ck_entrants_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entrants")),
    )
    op.create_index("ix_entrants_contest_id_status", "entrants", ["contest_id", "status"])
    op.create_index(
        "ix_entrants_contest_id_name",
        "entrants",
        ["contest_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("name IS NOT NULL"),
    )
    op.create_table(
        "entrant_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entrant_id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("left_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("way", sa.Integer(), nullable=True),
        sa.Column("way_name", sa.Text(), nullable=True),
        sa.Column("way_kind", sa.Text(), nullable=False),
        sa.Column("approval", sa.Text(), nullable=False),
        sa.Column(
            "eligibility",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint("status in ('member', 'left')", name=op.f("ck_entrant_members_status")),
        sa.CheckConstraint(
            "way_kind in ('invite', 'signup', 'organiser')",
            name=op.f("ck_entrant_members_way_kind"),
        ),
        sa.CheckConstraint(
            "approval in ('auto', 'manual')", name=op.f("ck_entrant_members_approval")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entrant_members")),
    )
    op.create_index(
        "ix_entrant_members_one_entrant",
        "entrant_members",
        ["contest_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'member'"),
    )
    op.create_index(
        "ix_entrant_members_contest_id_user_id", "entrant_members", ["contest_id", "user_id"]
    )
    op.create_index("ix_entrant_members_entrant_id", "entrant_members", ["entrant_id"])
    op.add_column("invites", sa.Column("entrant_id", sa.Uuid(), nullable=True))
    op.drop_constraint(op.f("ck_invites_grants"), "invites", type_="check")
    op.create_check_constraint(
        op.f("ck_invites_grants"),
        "invites",
        "grants in ('contestant', 'member', 'admin', 'manager', 'observer')",
    )
    op.create_check_constraint(
        op.f("ck_invites_member_entrant"),
        "invites",
        "grants <> 'member' OR entrant_id IS NOT NULL",
    )
    op.create_index("ix_invites_entrant_id_status", "invites", ["entrant_id", "status"])
    op.add_column("gradings", sa.Column("submitted_by", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    _forget_workspaces()
    op.drop_column("gradings", "submitted_by")
    op.execute("DELETE FROM invites WHERE grants = 'member'")
    op.drop_index("ix_invites_entrant_id_status", table_name="invites")
    op.drop_constraint(op.f("ck_invites_member_entrant"), "invites", type_="check")
    op.drop_constraint(op.f("ck_invites_grants"), "invites", type_="check")
    op.create_check_constraint(
        op.f("ck_invites_grants"),
        "invites",
        "grants in ('contestant', 'admin', 'manager', 'observer')",
    )
    op.drop_column("invites", "entrant_id")
    op.drop_table("entrant_members")
    op.drop_table("entrants")
    op.create_table(
        "contestants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "eligibility",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "time_extension_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        *_timestamps(),
        sa.Column("extension_tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected', 'withdrawn', 'removed')",
            name=op.f("ck_contestants_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contestants")),
        sa.UniqueConstraint(
            "contest_id", "user_id", name=op.f("uq_contestants_contest_id_user_id")
        ),
    )
    op.create_index("ix_contestants_contest_id_status", "contestants", ["contest_id", "status"])
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("leader_user_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.Column(
            "time_extension_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("extension_tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
