"""Invites arrive with feature 15: an organiser asks one person, by username
or by email address, to take a contestant's place in a contest or a role at a
scope. The table is new, so there is nothing to carry across; going back
drops it.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("grants", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("invited_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("mail_status", sa.Text(), nullable=False),
        sa.Column("mailed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "grants in ('contestant', 'admin', 'manager', 'observer')",
            name=op.f("ck_invites_grants"),
        ),
        sa.CheckConstraint(
            "status in ('pending', 'accepted', 'declined', 'withdrawn')",
            name=op.f("ck_invites_status"),
        ),
        sa.CheckConstraint(
            "mail_status in ('waiting', 'sent', 'failed', 'off')",
            name=op.f("ck_invites_mail_status"),
        ),
        sa.CheckConstraint("(username IS NULL) <> (email IS NULL)", name=op.f("ck_invites_target")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
    )
    op.create_index("ix_invites_scope", "invites", ["scope"])
    op.create_index("ix_invites_user_id_status", "invites", ["user_id", "status"])
    op.create_index("ix_invites_email", "invites", ["email"])


def downgrade() -> None:
    op.drop_table("invites")
