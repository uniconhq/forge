"""A grading staff cancelled before 0013 has no sentence for its contestant,
so it read as cancelled with nothing said and still counted against the
task's `submissions.max`. Each cancelled grading that is the latest attempt
of its submission is one staff cancelled, since a rejudge cancels an
attempt only to make a later one, and is given `STOCK_REASON`, so it reads
and counts as one cancelled since. A cancelled attempt a later one replaced
keeps none.

Going back takes the stock sentence off the rows that carry it.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-08 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STOCK_REASON = "The organisers cancelled this grading."


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE gradings AS cancelled SET cancel_reason = :reason "
            "WHERE cancelled.status = 'cancelled' AND cancelled.cancel_reason IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM gradings AS later "
            "WHERE later.submission_id = cancelled.submission_id "
            "AND later.attempt > cancelled.attempt)"
        ).bindparams(reason=STOCK_REASON)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE gradings SET cancel_reason = NULL WHERE cancel_reason = :reason"
        ).bindparams(reason=STOCK_REASON)
    )
