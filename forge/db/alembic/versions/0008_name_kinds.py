"""A name's kind is one of the three things that have names: an org, a
contest or a task, held to that by the database like every other column
that takes one of a fixed set of words. Every row written so far is one of
the three, so nothing is changed. Going back drops the check.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-03 21:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "kind in ('org', 'contest', 'task')"


def upgrade() -> None:
    op.create_check_constraint(op.f("ck_names_kind"), "names", KINDS)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_names_kind"), "names", type_="check")
