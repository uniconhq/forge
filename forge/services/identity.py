"""Who a session belongs to and which roles they hold. The identity is read
from the forge with the session's credential; the roles are read live and
never stored.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.errors import Forbidden, SessionExpired, Unavailable
from forge.domain.identity import User
from forge.domain.roles import RoleGrant
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.port import Forge
from forge.services import sessions
from forge.settings import Settings

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Me:
    """The signed-in user and their roles at every scope. `degraded` is set
    when the forge did not answer and the identity comes from the session
    alone.
    """

    user: User
    roles: tuple[RoleGrant, ...]
    degraded: bool


async def whoami(db: AsyncSession, settings: Settings, forge: Forge, session: Session) -> Me:
    try:
        credential = await sessions.credential_for(db, settings, forge, session.id)
        user = await forge.user_of(credential)
    except Forbidden as exc:
        log.warning("identity.credential_refused", user_id=session.user_id)
        await sessions.revoke(db, session.id)
        raise SessionExpired("Sign in again.") from exc
    except Unavailable:
        return Me(user=User(id=session.user_id, username=session.username), roles=(), degraded=True)
    try:
        roles = await forge.roles_of(user.id)
    except Unavailable:
        return Me(user=user, roles=(), degraded=True)
    return Me(user=user, roles=roles, degraded=False)


async def current(db: AsyncSession, settings: Settings, session_id: uuid.UUID) -> Session:
    """The session behind an id, checked for its lifetimes."""
    return await sessions.authenticate(db, settings, session_id)
