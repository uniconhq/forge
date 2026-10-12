"""A grading that fails is tried again once by itself, and a submission keeps
its last result until a newer one replaces it, so staff's fallback goes:
`falls_back` is dropped, and `automatic` marks the attempt the platform made
by itself when the one before it failed, so it is made once. Every grading
so far was made by a submit, staff or a save, so the column comes in false.
Going back gives `falls_back` back, false on every row, and drops
`automatic`.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("gradings", "falls_back")
    op.add_column(
        "gradings",
        sa.Column("automatic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("gradings", "automatic")
    op.add_column(
        "gradings",
        sa.Column("falls_back", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
