"""A grading keeps one output per item (#625): with nothing reused, the
reader reads a grading's outputs by step and run alone, so a second for the
same item is refused, a step done once among them, whose run is null.
The index leads with the grading, so it stands for the one it replaces.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-05 17:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_step_outputs_grading_id", table_name="step_outputs")
    op.create_index(
        "ix_step_outputs_grading_id_step_run",
        "step_outputs",
        ["grading_id", "step", "run"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("ix_step_outputs_grading_id_step_run", table_name="step_outputs")
    op.create_index("ix_step_outputs_grading_id", "step_outputs", ["grading_id"])
