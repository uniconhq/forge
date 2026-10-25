"""Staff enter a mark for a submission's `entered` value: each time is one
`entries` row, never changed or deleted, the latest for a submission's
value its mark, and no row a value still missing.

Going back drops the table.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-23 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("submission_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("submission_number", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("mark", sa.Text(), nullable=False),
        sa.Column("entered_by", sa.BigInteger(), nullable=False),
        sa.Column("entered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entries")),
    )
    op.create_index(
        "ix_entries_submission_id_value_seq", "entries", ["submission_id", "value", "seq"]
    )


def downgrade() -> None:
    op.drop_table("entries")
