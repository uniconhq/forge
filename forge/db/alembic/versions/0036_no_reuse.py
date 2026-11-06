"""No grading reuses another's outputs (#625): the store's keys, what was
reusable and the pins go, and so does grading from scratch. A step's job
keeps its runs alone, not each with its key.

Going back empties the store and the steps' rows, since no kept output
has a key to be reused by, and brings the columns and the pins back.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-05 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("grading_reuses")
    op.drop_index("ix_step_outputs_submission_id_key_pipeline", table_name="step_outputs")
    op.drop_index("ix_step_outputs_org_id_key_pipeline", table_name="step_outputs")
    for column in ("org_id", "submission_id", "key", "reusable"):
        op.drop_column("step_outputs", column)
    op.drop_column("grading_steps", "from_scratch")
    op.drop_column("gradings", "from_scratch")


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM step_outputs"))
    op.execute(sa.text("DELETE FROM grading_steps"))
    for table in ("gradings", "grading_steps"):
        op.add_column(
            table,
            sa.Column(
                "from_scratch", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
        )
    op.add_column("step_outputs", sa.Column("org_id", sa.Text(), nullable=False))
    op.add_column("step_outputs", sa.Column("submission_id", sa.Text(), nullable=True))
    op.add_column("step_outputs", sa.Column("key", sa.Text(), nullable=False))
    op.add_column("step_outputs", sa.Column("reusable", sa.Boolean(), nullable=False))
    op.create_index(
        "ix_step_outputs_submission_id_key_pipeline",
        "step_outputs",
        ["submission_id", "key", "pipeline"],
        unique=True,
        postgresql_where=sa.text("reusable and submission_id is not null"),
    )
    op.create_index(
        "ix_step_outputs_org_id_key_pipeline",
        "step_outputs",
        ["org_id", "key", "pipeline"],
        unique=True,
        postgresql_where=sa.text("reusable and submission_id is null"),
    )
    op.create_table(
        "grading_reuses",
        sa.Column("grading_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("output_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("grading_id", "key", name=op.f("pk_grading_reuses")),
    )
