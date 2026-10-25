"""Staff release a task's results and take a release back: each time is one
`releases` row, never changed or deleted, the latest a task's state, and no
row a task never released. Each grading says whether it takes its plan's
released split (`gradings.released_split`); every grading made before was
made with no release, so the column comes in false.

Going back drops the column and the table.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("acted_by", sa.BigInteger(), nullable=False),
        sa.Column("acted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action in ('released', 'hidden_again')", name=op.f("ck_releases_action")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_releases")),
    )
    op.create_index("ix_releases_task_id_acted_at", "releases", ["task_id", "acted_at"])
    op.add_column(
        "gradings",
        sa.Column("released_split", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("gradings", "released_split")
    op.drop_table("releases")
