"""A grading keeps which stored output each item it reuses resolved to the
first time it read it, `grading_reuses`, so a grading from scratch that
replaces the reused output of a key changes nothing for a grading already
under way. No grading reused anything kept for it before, so the table
comes in empty.

Going back drops the table.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-22 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "grading_reuses",
        sa.Column("grading_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("output_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("grading_id", "key", name=op.f("pk_grading_reuses")),
    )


def downgrade() -> None:
    op.drop_table("grading_reuses")
