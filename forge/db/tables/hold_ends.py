"""A contest's hold that staff ended early (SIMPLER-CORE.md 10.2 rule 10,
stage 5 section 6 items 2 and 10): one row each time, never changed or
deleted. A row names the hold it ended by its `from`, so a hold written
again with another `from` holds afresh, and the same hold is ended once:
ending it again answers the first row.

`ended_by` is the manager or admin who ended it and `ended_at` when, by
the package's clock.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id


class HoldEnd(Base, Timestamped):
    __tablename__ = "hold_ends"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    contest_id: Mapped[str]
    hold_from: Mapped[datetime]
    ended_by: Mapped[int] = mapped_column(BigInteger)
    ended_at: Mapped[datetime]

    __table_args__ = (UniqueConstraint("contest_id", "hold_from"),)
