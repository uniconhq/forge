"""Keep reusable measurements within the submission that owns them."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("step_outputs", sa.Column("submission_id", sa.Text(), nullable=True))
    op.add_column(
        "gradings",
        sa.Column("from_scratch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(
        sa.text("""
        UPDATE step_outputs AS output SET submission_id = grading.submission_id
        FROM gradings AS grading WHERE output.grading_id = grading.id
    """)
    )
    op.drop_index("ix_step_outputs_org_id_key_pipeline", table_name="step_outputs")
    op.execute(
        sa.text("""
        INSERT INTO step_outputs
            (id, org_id, submission_id, key, pipeline, reusable, grading_id,
             step, run, outputs, files, measured_at)
        SELECT gen_random_uuid(), pinned.org_id, pinned.submission_id, pinned.key,
               pinned.pipeline, true, pinned.grading_id, pinned.step, pinned.run,
               pinned.outputs, pinned.files, pinned.measured_at
        FROM (
            SELECT DISTINCT ON (grading.submission_id, output.key, output.pipeline)
                   output.org_id, grading.submission_id, output.key, output.pipeline,
                   output.grading_id, output.step, output.run, output.outputs,
                   output.files, output.measured_at
            FROM grading_reuses AS reuse
            JOIN gradings AS grading ON grading.id = reuse.grading_id
            JOIN step_outputs AS output ON output.id = reuse.output_id
            WHERE grading.submission_id IS NOT NULL
              AND output.pipeline IN ('early', 'late')
              AND NOT EXISTS (
                  SELECT 1 FROM step_outputs AS owned
                  WHERE owned.submission_id = grading.submission_id
                    AND owned.key = output.key AND owned.pipeline = output.pipeline
                    AND owned.reusable
              )
            ORDER BY grading.submission_id, output.key, output.pipeline,
                     grading.created_at DESC, grading.id DESC
        ) AS pinned
    """)
    )
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


def downgrade() -> None:
    op.drop_index("ix_step_outputs_submission_id_key_pipeline", table_name="step_outputs")
    op.drop_index("ix_step_outputs_org_id_key_pipeline", table_name="step_outputs")
    op.execute(
        sa.text("""
        UPDATE step_outputs SET reusable = false WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY org_id, key, pipeline ORDER BY measured_at DESC, id DESC
                ) AS position FROM step_outputs WHERE reusable
            ) AS ranked WHERE position > 1
        )
    """)
    )
    op.create_index(
        "ix_step_outputs_org_id_key_pipeline",
        "step_outputs",
        ["org_id", "key", "pipeline"],
        unique=True,
        postgresql_where=sa.text("reusable"),
    )
    op.drop_column("step_outputs", "submission_id")
    op.drop_column("gradings", "from_scratch")
