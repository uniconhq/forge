"""The connection pool, the factory that opens a transaction on it, and the
separate connection a readiness probe asks with. Every connection runs in
UTC, so a time read back from the database is in UTC whatever zone the
server itself is set to, the same as every time the package writes. A JSON
column's numbers are kept as written (`forge.domain.exact_json`), so a
result's values come back exactly.

A unit of work holds one connection from its first query until it commits
or rolls back, and never asks for a second while it holds the first: a
request that did would wait on the pool while keeping a connection from it,
and a few dozen such requests at once would each hold one and wait for
another until every one of them timed out. Work that must land whatever a
unit of work does runs after it ends, on a connection of its own
(`Context.after_end`). The transactions the factory opens note whether
they have written, so a unit of work that has only read may hand its
connection back before a slow call (`Context.let_go`) and one that has
written cannot.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction
from sqlalchemy.pool import NullPool

from forge.domain import exact_json

TransactionFactory = async_sessionmaker[AsyncSession]

IN_UTC = {"options": "-c TimeZone=UTC"}

WROTE = "unicon.wrote"


@dataclass(frozen=True, slots=True)
class PoolSize:
    """How many connections one process keeps and how long a request waits
    for one. `kept` stay open, `overflow` more are opened in a rush and closed
    again, and a request that finds all of them taken waits `wait` and then
    fails, which the person sees as a moment's unavailability rather than a
    page that hangs.
    """

    kept: int
    overflow: int
    wait: timedelta


def new_engine(database_url: str, pool: PoolSize) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool.kept,
        max_overflow=pool.overflow,
        pool_timeout=pool.wait.total_seconds(),
        connect_args=IN_UTC,
        json_serializer=exact_json.dumps,
        json_deserializer=exact_json.loads,
    )


def new_probe_engine(database_url: str) -> AsyncEngine:
    """A pool-less engine for readiness probes alone, so a busy pool is not
    reported as a dead database.
    """
    return create_async_engine(database_url, poolclass=NullPool, connect_args=IN_UTC)


class NotingSession(Session):
    """A session that notes in its `info` whether its transaction has written
    or locked anything, for `wrote`.
    """


@event.listens_for(NotingSession, "do_orm_execute")
def _note_a_write(state: ORMExecuteState) -> None:
    statement: Any = state.statement
    if not state.is_select or getattr(statement, "_for_update_arg", None) is not None:
        state.session.info[WROTE] = True


@event.listens_for(NotingSession, "after_flush")
def _note_a_flush(session: Session, _context: Any) -> None:
    session.info[WROTE] = True


@event.listens_for(NotingSession, "after_transaction_end")
def _forget_on_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop(WROTE, None)


def wrote(db: AsyncSession) -> bool:
    """Whether the transaction `db` has open has written, holds a row lock, or
    has changes waiting to be flushed.
    """
    return bool(db.info.get(WROTE) or db.new or db.dirty or db.deleted)


def new_transaction_factory(engine: AsyncEngine) -> TransactionFactory:
    return async_sessionmaker(engine, expire_on_commit=False, sync_session_class=NotingSession)


async def ping(engine: AsyncEngine, timeout: float) -> None:
    """Raise if the database does not answer within `timeout` seconds."""
    async with asyncio.timeout(timeout), engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
