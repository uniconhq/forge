"""Staff end a contest's hold early: each time is one `hold_ends` row,
never changed or deleted, naming the hold it ended by its `from`, at most
one for each.

Going back drops the table.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-23 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hold_ends",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("hold_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_by", sa.BigInteger(), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hold_ends")),
        sa.UniqueConstraint(
            "contest_id", "hold_from", name=op.f("uq_hold_ends_contest_id_hold_from")
        ),
    )


def downgrade() -> None:
    op.drop_table("hold_ends")
