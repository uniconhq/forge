"""A rejudge or a save's regrade of every submission to a task is kept as one
`regrades` row, and each attempt it makes, and each retry of one of those,
points at it by `gradings.regrade_id`, so what it changed can be shown and
named. No regrade so far was recorded, so the table comes in empty and the
column empty: a regrade made before reads as attempts that belong to none.

Going back drops the column and the table.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-12 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "regrades",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("publication_id", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("started_by", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("dismissed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dismissed_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("cause in ('rejudge', 'save')", name=op.f("ck_regrades_cause")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regrades")),
    )
    op.create_index("ix_regrades_task_id", "regrades", ["task_id"])
    op.add_column("gradings", sa.Column("regrade_id", sa.Uuid(), nullable=True))
    op.create_index("ix_gradings_regrade_id", "gradings", ["regrade_id"])


def downgrade() -> None:
    op.drop_index("ix_gradings_regrade_id", table_name="gradings")
    op.drop_column("gradings", "regrade_id")
    op.drop_table("regrades")
