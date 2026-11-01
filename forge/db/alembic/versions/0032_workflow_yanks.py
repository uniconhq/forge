"""Workflow versions withdrawn from new use."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_yanks",
        sa.Column("workflow_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("by", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workflow_key", "version", name=op.f("pk_workflow_yanks")),
    )


def downgrade() -> None:
    op.drop_table("workflow_yanks")
