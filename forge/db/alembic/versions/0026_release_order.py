"""A task's releases are ordered by `releases.seq`, a database sequence,
not by when the package's clock said each was made, since processes'
clocks may differ and the latest row is the task's state. A row made
before takes the next number in the order the table holds it.

Going back drops the column, and orders by the time again.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-23 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.drop_index("ix_releases_task_id_acted_at", table_name="releases")
    op.create_index("ix_releases_task_id_seq", "releases", ["task_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_releases_task_id_seq", table_name="releases")
    op.create_index("ix_releases_task_id_acted_at", "releases", ["task_id", "acted_at"])
    op.drop_column("releases", "seq")
