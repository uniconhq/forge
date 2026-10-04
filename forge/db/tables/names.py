"""The names people give orgs, contests and tasks, and the key each is filed
under. One row per thing: `id` is its id, built from keys (an org's key, a
contest's `<org>/<contest>`, a task's `<org>/<contest>/<task>`), `kind`
which of the three it is, `parent` the id of what it is in, empty for an
org, and `name` what people call it, unique among the things of its kind in
the same parent. The row is written when the thing is asked for, so its
name is taken from then on, and a route finds the id from the names in its
address here.
"""

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.roles import ScopeKind

KINDS = tuple(kind.value for kind in ScopeKind)


class Name(Base, Timestamped):
    __tablename__ = "names"

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    parent: Mapped[str]
    name: Mapped[str]

    __table_args__ = (
        CheckConstraint(f"kind in {KINDS}", name="kind"),
        UniqueConstraint("kind", "parent", "name"),
    )
