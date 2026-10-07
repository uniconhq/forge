"""Staff end a submission in `system_error` by cancelling its grading with a
sentence its contestant reads, which the grading keeps beside `error`, the
line for staff about what went wrong.

Going back drops the sentence; the grading stays cancelled.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-07 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("gradings", sa.Column("cancel_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("gradings", "cancel_reason")
