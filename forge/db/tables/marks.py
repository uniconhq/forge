"""The submissions a row has marked for the `marked` boards, one set per row
and task, shared by every board and, for a team, by every member. A mark
names a submission by its number in the row's workspace, graded or not, so
it is kept whatever becomes of the submission's grading; only a board reads
whether it is a candidate.
"""

import uuid

from sqlalchemy import BigInteger, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id


class Mark(Base, Timestamped):
    __tablename__ = "marks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    workspace_id: Mapped[str]
    task_id: Mapped[str]
    submission_number: Mapped[int]
    marked_by: Mapped[int] = mapped_column(BigInteger)
    """Who marked it, a member of the row."""

    __table_args__ = (
        Index(
            "ix_marks_workspace_id_task_id_submission_number",
            "workspace_id",
            "task_id",
            "submission_number",
            unique=True,
        ),
        Index("ix_marks_task_id", "task_id"),
    )
