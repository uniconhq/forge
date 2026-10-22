"""The marks staff enter for a submission's `entered` values (SIMPLER-CORE.md
14.2, stage 5 section 6 item 9): one row each time a mark is entered, never
changed or deleted, so who changed a mark stays known. The latest row for
a submission's value is its mark; a value with no row is missing.

A row names its submission by id, with its workspace and number as staff
found it, and its value by name, since a value holds one `entered` block
at most. `mark` is the number exactly as entered, as text, so nothing is
lost to a float. `entered_by` is the organiser who entered it and
`entered_at` when, by the package's clock; `seq` is the order the rows
were written in, from a database sequence, which decides the latest row.
A regrade never touches an entry: a mark is the submission's, not its
grading's.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Identity, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id


class Entry(Base, Timestamped):
    __tablename__ = "entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str]
    submission_id: Mapped[str]
    workspace_id: Mapped[str]
    submission_number: Mapped[int]
    value: Mapped[str]
    mark: Mapped[str]
    entered_by: Mapped[int] = mapped_column(BigInteger)
    entered_at: Mapped[datetime]
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))

    __table_args__ = (Index("ix_entries_submission_id_value_seq", "submission_id", "value", "seq"),)
