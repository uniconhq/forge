"""The declarative base every table inherits. Timestamps are `timestamptz`,
text is `text`, and constraint names follow one convention so migrations
generate the same names on every machine.
"""

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import TIMESTAMP, LargeBinary, MetaData, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: TIMESTAMP(timezone=True),
        str: Text,
        bytes: LargeBinary,
        uuid.UUID: Uuid(as_uuid=True),
        dict[str, Any]: JSONB,
    }


class Timestamped:
    """`created_at` and `updated_at`, kept by the database."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
