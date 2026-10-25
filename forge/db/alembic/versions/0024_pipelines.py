"""A run task's grading is a row per pipeline: `gradings.pipeline` says
which, `early` for every row made before, `late` naming the early row it
follows in `early_id`, and `save` for a publication's own checking, which
has no submission, so the submission's columns may be empty on a save row
and on no other. A submission's attempts are counted per pipeline, and a
publication's save attempts once each. `run_user` is the user a running
row's steps run as, from the sequence `grading_users`, unique among the
rows not finished. `taken_over_at` is when a save row that passed let its
publication take over.

Going back deletes every late and save row, which no row before knew, with
the steps, outputs and reuse pins that were theirs.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-22 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUBMISSION_COLUMNS = (
    "workspace_id",
    "submission_id",
    "submission_number",
    "submission_version",
    "submitted_at",
)
PIPELINES = "pipeline in ('save', 'early', 'late')"
SUBMISSION = (
    "(pipeline = 'save') = (submission_id is null) and (pipeline = 'save' or ("
    "workspace_id is not null and submission_number is not null "
    "and submission_version is not null and submitted_at is not null))"
)
UNFINISHED = "run_user is not null and status in ('queued', 'dispatched', 'running')"
LEFT = "SELECT id FROM gradings WHERE pipeline <> 'early'"


def upgrade() -> None:
    op.add_column(
        "gradings",
        sa.Column("pipeline", sa.Text(), server_default=sa.text("'early'"), nullable=False),
    )
    op.add_column("gradings", sa.Column("early_id", sa.Uuid(), nullable=True))
    op.add_column("gradings", sa.Column("run_user", sa.Integer(), nullable=True))
    op.add_column(
        "gradings", sa.Column("taken_over_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    for column in SUBMISSION_COLUMNS:
        op.alter_column("gradings", column, nullable=True)
    op.create_check_constraint(op.f("ck_gradings_pipeline"), "gradings", PIPELINES)
    op.create_check_constraint(op.f("ck_gradings_submission"), "gradings", SUBMISSION)
    op.drop_constraint(op.f("uq_gradings_submission_id_attempt"), "gradings", type_="unique")
    op.create_unique_constraint(
        op.f("uq_gradings_submission_id_pipeline_attempt"),
        "gradings",
        ["submission_id", "pipeline", "attempt"],
    )
    op.create_index(
        "ix_gradings_publication_id_attempt",
        "gradings",
        ["publication_id", "attempt"],
        unique=True,
        postgresql_where=sa.text("pipeline = 'save'"),
    )
    op.create_index(
        "ix_gradings_run_user",
        "gradings",
        ["run_user"],
        unique=True,
        postgresql_where=sa.text(UNFINISHED),
    )
    op.execute("CREATE SEQUENCE grading_users")


def downgrade() -> None:
    op.execute("DROP SEQUENCE grading_users")
    op.execute(
        f"DELETE FROM grading_reuses WHERE grading_id IN ({LEFT}) "
        f"OR output_id IN (SELECT id FROM step_outputs WHERE grading_id IN ({LEFT}))"
    )
    op.execute(f"DELETE FROM step_outputs WHERE grading_id IN ({LEFT})")
    op.execute(f"DELETE FROM grading_steps WHERE grading_id IN ({LEFT})")
    op.execute("DELETE FROM gradings WHERE pipeline <> 'early'")
    op.drop_index(
        "ix_gradings_run_user", table_name="gradings", postgresql_where=sa.text(UNFINISHED)
    )
    op.drop_index(
        "ix_gradings_publication_id_attempt",
        table_name="gradings",
        postgresql_where=sa.text("pipeline = 'save'"),
    )
    op.drop_constraint(
        op.f("uq_gradings_submission_id_pipeline_attempt"), "gradings", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_gradings_submission_id_attempt"), "gradings", ["submission_id", "attempt"]
    )
    op.drop_constraint(op.f("ck_gradings_submission"), "gradings", type_="check")
    op.drop_constraint(op.f("ck_gradings_pipeline"), "gradings", type_="check")
    for column in SUBMISSION_COLUMNS:
        op.alter_column("gradings", column, nullable=False)
    op.drop_column("gradings", "taken_over_at")
    op.drop_column("gradings", "run_user")
    op.drop_column("gradings", "early_id")
    op.drop_column("gradings", "pipeline")
