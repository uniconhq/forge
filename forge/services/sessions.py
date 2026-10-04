"""Sessions: creating one from a sign-in, checking one on every request,
keeping the credential inside it usable, and ending one. The only place the
`sessions` table is read or written and the only place a credential is
decrypted.

`revoke`, `revoke_all` and `list_for` are actions; the rest are building
blocks. Writes made on behalf of the caller go on the caller's unit of work.
The three writes that must land whatever the request does, touching the
last-seen time, the refresh compare-and-set and the revocation of a session
whose credential the host refuses to renew, run in a short transaction of
their own.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from sqlalchemy import CursorResult, Row, Update, delete, func, select, update

from forge.db.tables import Session as SessionRow
from forge.domain.client_address import client_address
from forge.domain.errors import Forbidden, NotFound, SessionExpired, Unauthenticated
from forge.domain.identity import Credential, User
from forge.domain.sessions import Session, SessionTimes, is_expired, needs_touch, refresh_due
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import credentials
from forge.services.credentials import CannotDecrypt
from forge.settings import Settings

log = get_logger(__name__)

NO_CREDENTIAL = b""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One row as the account page lists it."""

    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    ip: IPv4Address | IPv6Address | None
    user_agent: str | None
    current: bool


@dataclass(frozen=True, slots=True)
class _Stored:
    credential: bytes
    expires_at: datetime


async def create(
    ctx: Context, *, user: User, credential: Credential, ip: str | None, user_agent: str | None
) -> Session:
    """A new session for the user, after sweeping the rows that ended long
    ago, which every sign-in does so the table stays small.
    """
    await sweep(ctx)
    now = ctx.now
    row = SessionRow(
        user_id=user.id,
        username=user.username,
        credential=credentials.encrypt(credential, ctx.settings),
        credential_expires_at=credential.expires_at,
        created_at=now,
        expires_at=now + ctx.settings.session_hard_ttl,
        last_seen_at=now,
        ip=client_address(ip),
        user_agent=user_agent,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    session = _session(row)
    ctx.db.expunge(row)
    return session


async def authenticate(ctx: Context, session_id: uuid.UUID) -> Session:
    """The session behind an id, or `Unauthenticated` when there is none and
    `SessionExpired` when it has ended.
    """
    now = ctx.now
    row = (await ctx.db.execute(_columns().where(SessionRow.id == session_id))).one_or_none()
    if row is None:
        raise Unauthenticated("No session.")
    if is_expired(_times(row), now, ctx.settings.session_idle_ttl):
        raise SessionExpired("This session has ended.")
    if needs_touch(row.last_seen_at, now):
        async with ctx.own_transaction() as own:
            await own.execute(
                update(SessionRow).where(SessionRow.id == session_id).values(last_seen_at=now)
            )
    return _session(row)


async def credential_for(ctx: Context, session_id: uuid.UUID) -> Credential:
    """The session's credential, refreshed first when it is about to expire.
    Nothing is locked while the host is called: the row is read and released,
    the host is asked, and the new value is written only if the stored one is
    still the one that was read. A refresh the host refuses because another
    process already used the stored value is not a dead session; the other
    process's value is read back instead.
    """
    stored = await _stored(ctx, session_id)
    if not refresh_due(stored.expires_at, ctx.now):
        return _decrypt(stored.credential, ctx.settings)

    async with ctx.refresh_lock(session_id):
        stored = await _stored(ctx, session_id)
        if not refresh_due(stored.expires_at, ctx.now):
            return _decrypt(stored.credential, ctx.settings)
        try:
            issued = await ctx.forge.identity.refresh_credential(
                _decrypt(stored.credential, ctx.settings)
            )
        except Forbidden as exc:
            latest = await _stored(ctx, session_id)
            if latest.credential != stored.credential:
                return _decrypt(latest.credential, ctx.settings)
            log.info("session.credential_refused", session=str(session_id), reason=exc.detail)
            await revoke_now(ctx, session_id)
            raise SessionExpired("Sign in again to keep working at the forge.") from exc
        if await _store(ctx, session_id, was=stored.credential, issued=issued):
            return issued
    return _decrypt((await _stored(ctx, session_id)).credential, ctx.settings)


@action
async def revoke(ctx: Context, session_id: uuid.UUID, *, owner: int | None = None) -> None:
    """End one session on the caller's unit of work. Given an `owner`, the
    session must be theirs.
    """
    statement = _revocation(ctx.now).where(SessionRow.id == session_id)
    if owner is not None:
        statement = statement.where(SessionRow.user_id == owner)
    revoked = await ctx.db.execute(statement)
    if owner is not None and _rows_touched(revoked) == 0:
        raise NotFound("No such session.")


@action
async def revoke_all(ctx: Context, user_id: int) -> None:
    await ctx.db.execute(_revocation(ctx.now).where(SessionRow.user_id == user_id))


@action
async def list_for(ctx: Context, session: Session) -> list[SessionInfo]:
    now = ctx.now
    rows = await ctx.db.execute(
        _columns()
        .where(SessionRow.user_id == session.user_id, SessionRow.revoked_at.is_(None))
        .order_by(SessionRow.created_at.desc())
    )
    return [
        SessionInfo(
            id=row.id,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            ip=row.ip,
            user_agent=row.user_agent,
            current=row.id == session.id,
        )
        for row in rows
        if not is_expired(_times(row), now, ctx.settings.session_idle_ttl)
    ]


async def sweep(ctx: Context) -> int:
    """Delete rows that ended longer ago than the hard lifetime, so the table
    holds only sessions someone could still be shown.
    """
    now, settings = ctx.now, ctx.settings
    cutoff = now - settings.session_hard_ttl
    idle_cutoff = now - settings.session_idle_ttl - settings.session_hard_ttl
    gone = await ctx.db.execute(
        delete(SessionRow).where(
            (SessionRow.revoked_at < cutoff)
            | (SessionRow.expires_at < cutoff)
            | (SessionRow.last_seen_at < idle_cutoff)
        )
    )
    return _rows_touched(gone)


def _columns() -> Any:
    return select(
        SessionRow.id,
        SessionRow.user_id,
        SessionRow.username,
        SessionRow.created_at,
        SessionRow.expires_at,
        SessionRow.last_seen_at,
        SessionRow.revoked_at,
        SessionRow.ip,
        SessionRow.user_agent,
    )


def _session(row: SessionRow | Row[Any]) -> Session:
    return Session(
        id=row.id,
        user_id=row.user_id,
        username=row.username,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_seen_at=row.last_seen_at,
    )


def _times(row: SessionRow | Row[Any]) -> SessionTimes:
    return SessionTimes(
        expires_at=row.expires_at, last_seen_at=row.last_seen_at, revoked_at=row.revoked_at
    )


def _revocation(now: datetime) -> Update:
    return update(SessionRow).values(
        revoked_at=func.coalesce(SessionRow.revoked_at, now), credential=NO_CREDENTIAL
    )


def _rows_touched(result: Any) -> int:
    cursor: CursorResult[Any] = result
    return cursor.rowcount


async def _stored(ctx: Context, session_id: uuid.UUID) -> _Stored:
    async with ctx.own_transaction() as own:
        found = (
            await own.execute(
                select(
                    SessionRow.credential, SessionRow.credential_expires_at, SessionRow.revoked_at
                ).where(SessionRow.id == session_id)
            )
        ).one_or_none()
    if found is None or found.revoked_at is not None or not found.credential:
        raise Unauthenticated("No session.")
    return _Stored(credential=found.credential, expires_at=found.credential_expires_at)


async def revoke_now(ctx: Context, session_id: uuid.UUID) -> None:
    """End one session in a transaction of its own, so the revocation lands
    whatever the caller does with its unit of work afterwards. This is the
    revoke for a session the host has refused: the caller raises next, and a
    raise rolls the caller's transaction back.
    """
    async with ctx.own_transaction() as own:
        await own.execute(_revocation(ctx.now).where(SessionRow.id == session_id))


async def _store(ctx: Context, session_id: uuid.UUID, *, was: bytes, issued: Credential) -> bool:
    async with ctx.own_transaction() as own:
        written = await own.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.credential == was)
            .values(
                credential=credentials.encrypt(issued, ctx.settings),
                credential_expires_at=issued.expires_at,
            )
        )
    return _rows_touched(written) == 1


def _decrypt(blob: bytes, settings: Settings) -> Credential:
    try:
        return credentials.decrypt(blob, settings)
    except CannotDecrypt as exc:
        raise SessionExpired("Sign in again.") from exc
