"""A run task's grading keeps a `grading_steps` row per Woodpecker step, its
token's hash and what its job held; every item a step does is kept in
`step_outputs`, the first of an org's key in a pipeline the one reused; and
`publication_objects` keeps the large-file objects a publication's planned
files are, read once. No run task was graded before, so all three come in
empty.

Going back drops the three tables.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PIPELINES = "pipeline in ('save', 'early', 'late')"


def upgrade() -> None:
    op.create_table(
        "grading_steps",
        sa.Column("grading_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("from_scratch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("job_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("served", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("box_first", sa.Integer(), nullable=True),
        sa.Column("uploads", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("done_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fault", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(PIPELINES, name=op.f("ck_grading_steps_pipeline")),
        sa.PrimaryKeyConstraint("grading_id", "step", name=op.f("pk_grading_steps")),
    )
    op.create_index(
        "ix_grading_steps_box_first",
        "grading_steps",
        ["box_first"],
        unique=True,
        postgresql_where=sa.text("box_first is not null"),
    )
    op.create_table(
        "step_outputs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("reusable", sa.Boolean(), nullable=False),
        sa.Column("grading_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("run", sa.Text(), nullable=True),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("measured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(PIPELINES, name=op.f("ck_step_outputs_pipeline")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_step_outputs")),
    )
    op.create_index(
        "ix_step_outputs_org_id_key_pipeline",
        "step_outputs",
        ["org_id", "key", "pipeline"],
        unique=True,
        postgresql_where=sa.text("reusable"),
    )
    op.create_index("ix_step_outputs_grading_id", "step_outputs", ["grading_id"])
    op.create_table(
        "publication_objects",
        sa.Column("publication_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("objects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("publication_id", name=op.f("pk_publication_objects")),
    )


def downgrade() -> None:
    op.drop_table("publication_objects")
    op.drop_table("step_outputs")
    op.drop_table("grading_steps")
