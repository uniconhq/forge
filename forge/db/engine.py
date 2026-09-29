"""The connection pool, the factory that opens a transaction on it, and the
separate connection a readiness probe asks with. Every connection runs in
UTC, so a time read back from the database is in UTC whatever zone the
server itself is set to, the same as every time the package writes.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

TransactionFactory = async_sessionmaker[AsyncSession]

IN_UTC = {"options": "-c TimeZone=UTC"}


def new_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True, connect_args=IN_UTC)


def new_probe_engine(database_url: str) -> AsyncEngine:
    """A pool-less engine for readiness probes alone, so a busy pool is not
    reported as a dead database.
    """
    return create_async_engine(database_url, poolclass=NullPool, connect_args=IN_UTC)


def new_transaction_factory(engine: AsyncEngine) -> TransactionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine, timeout: float) -> None:
    """Raise if the database does not answer within `timeout` seconds."""
    async with asyncio.timeout(timeout), engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
