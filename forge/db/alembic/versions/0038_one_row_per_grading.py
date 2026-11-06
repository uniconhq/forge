"""A grading is one row (#647): its pipeline is `grading`, or `save` for a
publication's own checking. Every late row goes, with its steps and their
outputs, an early row is a grading's row, and `early_id` goes.

Going back brings the pipelines `early` and `late` and `early_id` back, each
grading's row an early row, followed by no late row.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-05 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ONE = "pipeline in ('save', 'grading')"
SPLIT = "pipeline in ('save', 'early', 'late')"
TABLES = (
    ("gradings", "ck_gradings_pipeline"),
    ("grading_steps", "ck_grading_steps_pipeline"),
    ("step_outputs", "ck_step_outputs_pipeline"),
)
LATE = "SELECT id FROM gradings WHERE pipeline = 'late'"


def upgrade() -> None:
    op.execute(f"DELETE FROM step_outputs WHERE grading_id IN ({LATE})")
    op.execute(f"DELETE FROM grading_steps WHERE grading_id IN ({LATE})")
    op.execute("DELETE FROM gradings WHERE pipeline = 'late'")
    for table, check in TABLES:
        op.drop_constraint(op.f(check), table, type_="check")
        op.execute(f"UPDATE {table} SET pipeline = 'grading' WHERE pipeline in ('early', 'late')")
        op.create_check_constraint(op.f(check), table, ONE)
    op.alter_column("gradings", "pipeline", server_default=sa.text("'grading'"))
    op.drop_column("gradings", "early_id")


def downgrade() -> None:
    op.add_column("gradings", sa.Column("early_id", sa.Uuid(), nullable=True))
    op.alter_column("gradings", "pipeline", server_default=sa.text("'early'"))
    for table, check in TABLES:
        op.drop_constraint(op.f(check), table, type_="check")
        op.execute(f"UPDATE {table} SET pipeline = 'early' WHERE pipeline = 'grading'")
        op.create_check_constraint(op.f(check), table, SPLIT)
