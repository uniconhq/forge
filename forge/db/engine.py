"""The connection pool, the session factory, and the separate connection a
readiness probe asks with.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

SessionFactory = async_sessionmaker[AsyncSession]


def new_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def new_probe_engine(database_url: str) -> AsyncEngine:
    """A pool-less engine for readiness probes alone, so a busy pool is not
    reported as a dead database.
    """
    return create_async_engine(database_url, poolclass=NullPool)


def new_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def unit_of_work(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """One database session, closed when the block ends. The caller decides
    where the transaction commits.
    """
    async with factory() as session:
        yield session


async def ping(engine: AsyncEngine, timeout: float) -> None:
    """Raise if the database does not answer within `timeout` seconds."""
    async with asyncio.timeout(timeout), engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
