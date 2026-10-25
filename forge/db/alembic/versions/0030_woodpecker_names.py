"""A grading step keeps the name Woodpecker knows it by, beside its own
name. A row minted before this has none, and its door still finds it by
its own name.

Going back drops the column.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-24 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("grading_steps", sa.Column("woodpecker", sa.Text(), nullable=True))
    op.create_index(
        "ix_grading_steps_grading_id_woodpecker",
        "grading_steps",
        ["grading_id", "woodpecker"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_grading_steps_grading_id_woodpecker", table_name="grading_steps")
    op.drop_column("grading_steps", "woodpecker")
