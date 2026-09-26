"""Sessions: creating one from a sign-in, checking one on every request,
keeping the credential inside it usable, and ending one. The only place the
`sessions` table is read or written and the only place a credential is
decrypted.
"""

import asyncio
import json
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from sqlalchemy import CursorResult, Row, Update, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge.crypto import CannotDecrypt, decrypt, encrypt
from forge.db.tables import Session as SessionRow
from forge.domain.client_address import client_address
from forge.domain.errors import Forbidden, NotFound, SessionExpired, Unauthenticated
from forge.domain.identity import Credential, User
from forge.domain.sessions import Session, SessionTimes, is_expired, needs_touch, refresh_due
from forge.log import get_logger
from forge.port import Forge
from forge.settings import Settings

log = get_logger(__name__)

NO_CREDENTIAL = b""

_refreshing: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = weakref.WeakValueDictionary()


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
    db: AsyncSession,
    settings: Settings,
    *,
    user: User,
    credential: Credential,
    ip: str | None,
    user_agent: str | None,
) -> Session:
    now = datetime.now(UTC)
    row = SessionRow(
        user_id=user.id,
        username=user.username,
        credential=_encrypt(credential, settings),
        credential_expires_at=credential.expires_at,
        created_at=now,
        expires_at=now + settings.session_hard_ttl,
        last_seen_at=now,
        ip=client_address(ip),
        user_agent=user_agent,
    )
    db.add(row)
    await db.commit()
    session = _session(row)
    db.expunge(row)
    return session


async def authenticate(db: AsyncSession, settings: Settings, session_id: uuid.UUID) -> Session:
    """The session behind an id, or `Unauthenticated` when there is none and
    `SessionExpired` when it has ended.
    """
    now = datetime.now(UTC)
    row = (await db.execute(_columns().where(SessionRow.id == session_id))).one_or_none()
    if row is None:
        raise Unauthenticated("No session.")
    if is_expired(_times(row), now, settings.session_idle_ttl):
        raise SessionExpired("This session has ended.")
    if needs_touch(row.last_seen_at, now):
        await db.execute(
            update(SessionRow).where(SessionRow.id == session_id).values(last_seen_at=now)
        )
        await db.commit()
    return _session(row)


async def credential_for(
    db: AsyncSession, settings: Settings, forge: Forge, session_id: uuid.UUID
) -> Credential:
    """The session's credential, refreshed first when it is about to expire.
    Nothing is locked while the forge is called: the row is read and released,
    the forge is asked, and the new value is written only if the stored one is
    still the one that was read.
    """
    stored = await _stored(db, session_id)
    if not refresh_due(stored.expires_at, datetime.now(UTC)):
        return _decrypt(stored.credential, settings)

    async with _lock_for(session_id):
        stored = await _stored(db, session_id)
        if not refresh_due(stored.expires_at, datetime.now(UTC)):
            return _decrypt(stored.credential, settings)
        issued = await _refreshed(db, settings, forge, session_id, stored)
        if await _store(db, settings, session_id, was=stored.credential, issued=issued):
            return issued
    return _decrypt((await _stored(db, session_id)).credential, settings)


async def revoke(db: AsyncSession, session_id: uuid.UUID, *, owner: int | None = None) -> None:
    """End one session. Given an `owner`, the session must be theirs."""
    statement = _revocation().where(SessionRow.id == session_id)
    if owner is not None:
        statement = statement.where(SessionRow.user_id == owner)
    revoked = await db.execute(statement)
    await db.commit()
    if owner is not None and _rows_touched(revoked) == 0:
        raise NotFound("No such session.")


async def revoke_all(db: AsyncSession, user_id: int) -> None:
    await db.execute(_revocation().where(SessionRow.user_id == user_id))
    await db.commit()


async def list_for(db: AsyncSession, settings: Settings, session: Session) -> list[SessionInfo]:
    now = datetime.now(UTC)
    rows = await db.execute(
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
        if not is_expired(_times(row), now, settings.session_idle_ttl)
    ]


async def sweep(db: AsyncSession, settings: Settings, now: datetime | None = None) -> int:
    """Delete rows that ended longer ago than the hard lifetime, so the table
    holds only sessions someone could still be shown.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - settings.session_hard_ttl
    idle_cutoff = moment - settings.session_idle_ttl - settings.session_hard_ttl
    gone = await db.execute(
        delete(SessionRow).where(
            (SessionRow.revoked_at < cutoff)
            | (SessionRow.expires_at < cutoff)
            | (SessionRow.last_seen_at < idle_cutoff)
        )
    )
    await db.commit()
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


def _revocation() -> Update:
    return update(SessionRow).values(
        revoked_at=func.coalesce(SessionRow.revoked_at, datetime.now(UTC)),
        credential=NO_CREDENTIAL,
    )


def _rows_touched(result: Any) -> int:
    cursor: CursorResult[Any] = result
    return cursor.rowcount


def _lock_for(session_id: uuid.UUID) -> asyncio.Lock:
    lock = _refreshing.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _refreshing[session_id] = lock
    return lock


async def _stored(db: AsyncSession, session_id: uuid.UUID) -> _Stored:
    found = (
        await db.execute(
            select(
                SessionRow.credential, SessionRow.credential_expires_at, SessionRow.revoked_at
            ).where(SessionRow.id == session_id)
        )
    ).one_or_none()
    await db.commit()
    if found is None or found.revoked_at is not None or not found.credential:
        raise Unauthenticated("No session.")
    return _Stored(credential=found.credential, expires_at=found.credential_expires_at)


async def _refreshed(
    db: AsyncSession, settings: Settings, forge: Forge, session_id: uuid.UUID, stored: _Stored
) -> Credential:
    try:
        return await forge.refresh_credential(_decrypt(stored.credential, settings))
    except Forbidden as exc:
        log.info("session.credential_refused", session=str(session_id), reason=exc.detail)
        await revoke(db, session_id)
        raise SessionExpired("Sign in again to keep working at the forge.") from exc


async def _store(
    db: AsyncSession, settings: Settings, session_id: uuid.UUID, *, was: bytes, issued: Credential
) -> bool:
    written = await db.execute(
        update(SessionRow)
        .where(SessionRow.id == session_id, SessionRow.credential == was)
        .values(credential=_encrypt(issued, settings), credential_expires_at=issued.expires_at)
    )
    await db.commit()
    return _rows_touched(written) == 1


def _encrypt(credential: Credential, settings: Settings) -> bytes:
    payload = json.dumps(
        {
            "access": credential.access,
            "refresh": credential.refresh,
            "expires_at": credential.expires_at.isoformat(),
        }
    ).encode()
    return encrypt(payload, settings.token_encryption_key_bytes)


def _decrypt(blob: bytes, settings: Settings) -> Credential:
    try:
        payload = json.loads(decrypt(blob, settings.token_encryption_key_bytes))
    except CannotDecrypt as exc:
        raise SessionExpired("Sign in again.") from exc
    return Credential(
        access=str(payload["access"]),
        refresh=str(payload["refresh"]),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
    )
