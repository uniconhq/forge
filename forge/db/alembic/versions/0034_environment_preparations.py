"""Background package preparation before a task is published."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_preparations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status in ('preparing','ready','refused','replaced')",
            name=op.f("ck_environment_preparations_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environment_preparations")),
    )
    op.create_index(
        "ix_environment_preparations_task_id_created_at",
        "environment_preparations",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("environment_preparations")
