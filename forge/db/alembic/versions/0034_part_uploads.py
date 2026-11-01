"""Resumable organiser file uploads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "part_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("workflow_id", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("part_size", sa.BigInteger(), nullable=False),
        sa.Column("upload_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(task_id is null) <> (workflow_id is null)", name=op.f("ck_part_uploads_place")
        ),
        sa.CheckConstraint(
            "status in ('open','finishing','stored','refused','aborted')",
            name=op.f("ck_part_uploads_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_part_uploads")),
    )
    op.create_index(
        "ix_part_uploads_owner_user_id_status", "part_uploads", ["owner_user_id", "status"]
    )
    op.create_index("ix_part_uploads_deadline_at", "part_uploads", ["deadline_at"])


def downgrade() -> None:
    op.drop_table("part_uploads")
