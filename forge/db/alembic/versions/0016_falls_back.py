"""A grading may fall back with feature 9: `falls_back` on a grading is staff
asking that its submission count as its last good result while this attempt
is a system error or cancelled. Every grading so far falls back on nothing,
so the column comes in false; going back drops it.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gradings",
        sa.Column("falls_back", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("gradings", "falls_back")
