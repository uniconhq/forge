"""What a grading row needs once the platform starts its runs: when it last
entered the queue, how many starts failed since and when it is next tried,
how many times it went back to the queue after a run of it ended without a
verdict, and the last progress its run reported. A row from before this
revision entered the queue when it was made and has failed no start.

Going back removes the five columns.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30 22:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gradings",
        sa.Column(
            "queued_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute("UPDATE gradings SET queued_at = created_at")
    op.add_column(
        "gradings",
        sa.Column("start_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("gradings", sa.Column("retry_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "gradings",
        sa.Column("requeues", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "gradings",
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    for column in ("progress", "requeues", "retry_at", "start_failures", "queued_at"):
        op.drop_column("gradings", column)
