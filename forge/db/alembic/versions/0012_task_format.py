"""The task format of 2026-10-06 arrives: a task has one plan, so a grading
has no stage, and what a run reports is its result, `result.json`, not a
verdict. Every grading becomes the latest attempt's alone per submission:
the rows of any stage but `default` go, the rest drop `stage`, and the
column `verdict` becomes `result` with nothing in it, since a verdict is not
a result and no reader of one is kept; such a grading reads as finished
with no result until it is regraded. An extension names the tasks it is
for, on a contestant's row and now on a team's too, every task when it
names none.

Going back puts `stage` back as `default` and the column back as `verdict`,
empty; the results are not verdicts either.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM gradings WHERE stage <> 'default'")
    op.drop_index("ix_gradings_idempotency_key", table_name="gradings")
    op.drop_constraint(op.f("uq_gradings_submission_id_stage_attempt"), "gradings", type_="unique")
    op.drop_column("gradings", "stage")
    op.alter_column("gradings", "verdict", new_column_name="result")
    op.execute("UPDATE gradings SET result = NULL")
    op.create_unique_constraint(
        op.f("uq_gradings_submission_id_attempt"), "gradings", ["submission_id", "attempt"]
    )
    op.create_index(
        "ix_gradings_idempotency_key",
        "gradings",
        ["workspace_id", "task_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key is not null"),
    )
    op.add_column(
        "contestants",
        sa.Column("extension_tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "teams",
        sa.Column(
            "time_extension_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "teams",
        sa.Column("extension_tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("teams", "extension_tasks")
    op.drop_column("teams", "time_extension_seconds")
    op.drop_column("contestants", "extension_tasks")
    op.drop_index("ix_gradings_idempotency_key", table_name="gradings")
    op.drop_constraint(op.f("uq_gradings_submission_id_attempt"), "gradings", type_="unique")
    op.alter_column("gradings", "result", new_column_name="verdict")
    op.execute("UPDATE gradings SET verdict = NULL")
    op.add_column(
        "gradings",
        sa.Column("stage", sa.Text(), server_default=sa.text("'default'"), nullable=False),
    )
    op.alter_column("gradings", "stage", server_default=None)
    op.create_unique_constraint(
        op.f("uq_gradings_submission_id_stage_attempt"),
        "gradings",
        ["submission_id", "stage", "attempt"],
    )
    op.create_index(
        "ix_gradings_idempotency_key",
        "gradings",
        ["workspace_id", "task_id", "idempotency_key", "stage"],
        unique=True,
        postgresql_where=sa.text("idempotency_key is not null"),
    )
