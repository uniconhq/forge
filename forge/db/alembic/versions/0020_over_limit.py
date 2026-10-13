"""A delayed submission graded again once its entrant has no room left under
`submissions.max` ends `over_limit`, run by nothing, with the most it
reached in `submission_limit`. No grading so far is one, so the column comes
in empty.

Going back makes an `over_limit` attempt `cancelled` with a sentence saying
why, which leaves its submission void as it was, and drops the column.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES_BEFORE = (
    "status in ('queued', 'dispatched', 'running', 'done', 'cancelled', 'system_error')"
)
STATUSES_AFTER = (
    "status in ('queued', 'dispatched', 'running', 'done', 'cancelled', 'system_error', "
    "'over_limit')"
)


def upgrade() -> None:
    op.add_column("gradings", sa.Column("submission_limit", sa.Integer(), nullable=True))
    op.drop_constraint(op.f("ck_gradings_status"), "gradings", type_="check")
    op.create_check_constraint(op.f("ck_gradings_status"), "gradings", STATUSES_AFTER)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_gradings_status"), "gradings", type_="check")
    op.execute(
        "UPDATE gradings SET status = 'cancelled', cancel_reason = 'Not graded: you had "
        "reached the limit of ' || submission_limit || ' submissions for this task.' "
        "WHERE status = 'over_limit'"
    )
    op.create_check_constraint(op.f("ck_gradings_status"), "gradings", STATUSES_BEFORE)
    op.drop_column("gradings", "submission_limit")
