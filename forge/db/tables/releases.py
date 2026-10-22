"""What staff did to a task's results (SIMPLER-CORE.md 9.4, stage 5 section 6
item 2): one row each time they released them or took a release back,
never changed or deleted. The latest row of a task is where its results
stand: `released` shows what its `show` lists under `on_release`, and
`hidden_again` puts the task back as if never released. A task with no row
was never released.

`action` is which, `acted_by` the organiser who did it, and `acted_at` when,
by the package's clock.
"""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id

RELEASED: Final = "released"
HIDDEN_AGAIN: Final = "hidden_again"


class Release(Base, Timestamped):
    __tablename__ = "releases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str]
    action: Mapped[str]
    acted_by: Mapped[int] = mapped_column(BigInteger)
    acted_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint(f"action in ('{RELEASED}', '{HIDDEN_AGAIN}')", name="action"),
        Index("ix_releases_task_id_acted_at", "task_id", "acted_at"),
    )
