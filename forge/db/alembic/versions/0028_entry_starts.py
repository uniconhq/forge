"""A value given an `entered` block again starts with no marks: the save
writes an `entries` row with no submission and no mark for it, and the
rows before it no longer count. So an entry's submission, workspace,
number and mark may be empty, on that row alone.

Going back drops those rows and makes the columns required again.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-23 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("submission_id", "workspace_id", "submission_number", "mark")


def upgrade() -> None:
    for column in COLUMNS:
        op.alter_column("entries", column, nullable=True)
    op.create_index("ix_entries_task_id_value_seq", "entries", ["task_id", "value", "seq"])


def downgrade() -> None:
    op.drop_index("ix_entries_task_id_value_seq", table_name="entries")
    op.execute(sa.text("DELETE FROM entries WHERE submission_id IS NULL"))
    for column in COLUMNS:
        op.alter_column("entries", column, nullable=False)
