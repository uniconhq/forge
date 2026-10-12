"""A regrade of many: a rejudge of a task, or the regrade every submission
to it gets when a save publishes a change to how it grades, kept as one row
so what it changed can be shown and named. Its attempts point at it by
`gradings.regrade_id`.

`cause` is what started it, `rejudge` or `save`, `publication_id` the
publication its attempts grade against, `started_by` the organiser who
rejudged or saved, and `started_at` when, by the package's clock.
`dismissed_at` and `dismissed_by` say when and by whom an organiser took
its summary off the contest's page; a newer regrade of the task replaces it
there in any case.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id
from forge.domain.regrades import RegradeCause

CAUSES = tuple(cause.value for cause in RegradeCause)


class Regrade(Base, Timestamped):
    __tablename__ = "regrades"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str]
    publication_id: Mapped[str]
    cause: Mapped[str]
    started_by: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime]
    dismissed_at: Mapped[datetime | None]
    dismissed_by: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint(f"cause in {CAUSES}", name="cause"),
        Index("ix_regrades_task_id", "task_id"),
    )
