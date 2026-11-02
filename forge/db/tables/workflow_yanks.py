"""A workflow version's withdrawal from new use, keyed by its stable forge id."""

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base


class WorkflowYank(Base):
    __tablename__ = "workflow_yanks"

    workflow_key: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(primary_key=True)
    reason: Mapped[str]
    by: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[datetime]
