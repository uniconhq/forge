"""Marks arrive with feature 9: a `marks` row per submission a row marked for
the `marked` boards, at most one per submission. The table is new, so there
is nothing to carry across; going back drops it.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "marks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("submission_number", sa.Integer(), nullable=False),
        sa.Column("marked_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marks")),
    )
    op.create_index(
        "ix_marks_workspace_id_task_id_submission_number",
        "marks",
        ["workspace_id", "task_id", "submission_number"],
        unique=True,
    )
    op.create_index("ix_marks_task_id", "marks", ["task_id"])


def downgrade() -> None:
    op.drop_table("marks")
