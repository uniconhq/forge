"""What a grading row needs once a submit makes it: the task, the submission's
number and exact version, when it was submitted, and the key the submit was
sent with, unique for a workspace, task and stage; `system_error` as a
status; and `wait_reason` as a line an organiser reads rather than one of
two codes. A row from before this revision gets its task and number from its
submission's id, its time from when it was made and an empty version; none
is expected, since nothing made a grading row before it.

`gradings.plan_key` and `gradings.bundle_key`, which an earlier shape of
grading had, were never part of these migrations, so there is nothing to
drop.

Going back removes the new columns and indexes, turns a `system_error` row
`failed`, clears every wait reason the old check does not allow, and puts
both checks back as they were.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 18:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES_BEFORE = (
    "status in ('queued', 'dispatching', 'dispatched', 'running', 'done', 'failed', 'cancelled')"
)
STATUSES_AFTER = (
    "status in ('queued', 'dispatching', 'dispatched', 'running', 'done', 'failed', "
    "'cancelled', 'system_error')"
)
WAIT_REASONS_BEFORE = (
    "wait_reason is null or wait_reason in ('waiting_for_compute', 'ci_unavailable')"
)


def upgrade() -> None:
    op.add_column("gradings", sa.Column("task_id", sa.Text(), nullable=True))
    op.add_column("gradings", sa.Column("submission_number", sa.Integer(), nullable=True))
    op.add_column("gradings", sa.Column("submission_version", sa.Text(), nullable=True))
    op.add_column("gradings", sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("gradings", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.execute(
        "UPDATE gradings SET "
        "task_id = split_part(submission_id, '/', 1) || '/' || split_part(submission_id, '/', 2) "
        "|| '/' || split_part(split_part(submission_id, '/', 4), '#', 1), "
        "submission_number = split_part(submission_id, '#', 2)::integer, "
        "submission_version = '', "
        "submitted_at = created_at"
    )
    for column in ("task_id", "submission_number", "submission_version", "submitted_at"):
        op.alter_column("gradings", column, nullable=False)

    op.drop_constraint(op.f("ck_gradings_status"), "gradings", type_="check")
    op.create_check_constraint(op.f("ck_gradings_status"), "gradings", STATUSES_AFTER)
    op.drop_constraint(op.f("ck_gradings_wait_reason"), "gradings", type_="check")

    op.create_index(
        "ix_gradings_task_id_workspace_id", "gradings", ["task_id", "workspace_id"], unique=False
    )
    op.create_index(
        "ix_gradings_idempotency_key",
        "gradings",
        ["workspace_id", "task_id", "idempotency_key", "stage"],
        unique=True,
        postgresql_where=sa.text("idempotency_key is not null"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gradings_idempotency_key",
        table_name="gradings",
        postgresql_where=sa.text("idempotency_key is not null"),
    )
    op.drop_index("ix_gradings_task_id_workspace_id", table_name="gradings")

    op.execute(
        "UPDATE gradings SET wait_reason = NULL "
        "WHERE wait_reason NOT IN ('waiting_for_compute', 'ci_unavailable')"
    )
    op.create_check_constraint(op.f("ck_gradings_wait_reason"), "gradings", WAIT_REASONS_BEFORE)
    op.drop_constraint(op.f("ck_gradings_status"), "gradings", type_="check")
    op.execute("UPDATE gradings SET status = 'failed' WHERE status = 'system_error'")
    op.create_check_constraint(op.f("ck_gradings_status"), "gradings", STATUSES_BEFORE)

    for column in (
        "idempotency_key",
        "submitted_at",
        "submission_version",
        "submission_number",
        "task_id",
    ):
        op.drop_column("gradings", column)
