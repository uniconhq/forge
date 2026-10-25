"""Uploads into a workflow's own repository."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("workflow_id", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_uploads_purpose"), "uploads", type_="check")
    op.create_check_constraint(
        op.f("ck_uploads_purpose"),
        "uploads",
        "purpose in ('submission', 'task_file', 'workflow_file')",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM uploads WHERE purpose = 'workflow_file'"))
    op.drop_constraint(op.f("ck_uploads_purpose"), "uploads", type_="check")
    op.create_check_constraint(
        op.f("ck_uploads_purpose"), "uploads", "purpose in ('submission', 'task_file')"
    )
    op.drop_column("uploads", "workflow_id")
