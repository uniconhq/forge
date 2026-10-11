"""Entrants and their members, one contest at a time. An entrant is one or
more people who enter a contest together, from its registration to its
removal; it has no object at the forge, since its workspace is named by its
id (`EntrantOwner`) and who may reach it is each member's own access, given
and taken away as the membership changes. A registration before approval has
nothing at the forge either, which is why the rows exist.

`entrants` holds the registration, the decision on it and the extension, the
entrant's alone. `entrant_members` holds a row for each time someone was in
an entrant: the way they came in by, what let them through, and when they
joined and left. A person is a member of one entrant in a contest at most,
held by the database.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.definitions import Approval
from forge.domain.entrants import MemberStatus
from forge.domain.ids import new_id
from forge.domain.registration import Status, WayKind

STATUSES = tuple(status.value for status in Status)
MEMBER_STATUSES = tuple(status.value for status in MemberStatus)
WAY_KINDS = tuple(kind.value for kind in WayKind)
APPROVALS = tuple(approval.value for approval in Approval)


class Entrant(Base, Timestamped):
    __tablename__ = "entrants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    contest_id: Mapped[str]
    name: Mapped[str | None]
    """None for an entrant of one in a contest whose entrants are one person
    each, shown under its member's username."""
    status: Mapped[str]
    registered_at: Mapped[datetime]
    registered_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    """Who registered it: its first member, or none for one an organiser
    made."""
    decided_at: Mapped[datetime | None]
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None]
    time_extension_seconds: Mapped[int] = mapped_column(server_default=text("0"))
    extension_tasks: Mapped[list[str] | None] = mapped_column(JSONB)
    """The tasks, by name, the extension is for; every task when none."""

    __table_args__ = (
        CheckConstraint(f"status in {STATUSES}", name="status"),
        Index("ix_entrants_contest_id_status", "contest_id", "status"),
        Index(
            "ix_entrants_contest_id_name",
            "contest_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("name IS NOT NULL"),
        ),
    )


class EntrantMember(Base, Timestamped):
    __tablename__ = "entrant_members"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    entrant_id: Mapped[uuid.UUID]
    """The entrant, which may be gone: a deleted entrant's members keep their
    rows, `left`, so each person's uploads afterwards are told from its."""
    contest_id: Mapped[str]
    user_id: Mapped[int] = mapped_column(BigInteger)
    """Who, kept once their account is gone."""
    status: Mapped[str]
    joined_at: Mapped[datetime]
    left_at: Mapped[datetime | None]
    way: Mapped[int | None]
    """The index in `registration.ways` of the way they came in by, as the
    ways stood then; none for someone an organiser put in."""
    way_name: Mapped[str | None]
    way_kind: Mapped[str]
    approval: Mapped[str]
    """Whether that way approved straight away, as it stood then."""
    eligibility: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    """What let them through: the invite, the code, the address."""

    __table_args__ = (
        CheckConstraint(f"status in {MEMBER_STATUSES}", name="status"),
        CheckConstraint(f"way_kind in {WAY_KINDS}", name="way_kind"),
        CheckConstraint(f"approval in {APPROVALS}", name="approval"),
        Index(
            "ix_entrant_members_one_entrant",
            "contest_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'member'"),
        ),
        Index("ix_entrant_members_contest_id_user_id", "contest_id", "user_id"),
        Index("ix_entrant_members_entrant_id", "entrant_id"),
    )
