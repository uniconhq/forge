"""Alembic's entry point. Migrations run on the synchronous driver, with the
URL from Alembic's configuration or from `UNICON_DATABASE_URL`.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from forge.db import tables
from forge.db.base import Base

target_metadata = Base.metadata
tables.register()


def _database_url() -> str:
    url = context.config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from_env = os.environ.get("UNICON_DATABASE_URL")
    if not from_env:
        raise SystemExit("UNICON_DATABASE_URL is not set")
    return from_env


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
