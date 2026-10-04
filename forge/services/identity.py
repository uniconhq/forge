"""Who a session belongs to and which roles they hold. The identity is read
from the host with the session's credential; the roles are read live and
never stored.
"""

import uuid
from dataclasses import dataclass

from forge.domain.errors import Forbidden, SessionExpired, Unavailable
from forge.domain.identity import AsUser, User
from forge.domain.roles import HeldRole
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, sessions

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Me:
    """The signed-in user and their roles at every scope, each with the names
    of where it is held. `degraded` is set when the host did not answer and
    the identity comes from the session alone.
    """

    user: User
    roles: tuple[HeldRole, ...]
    degraded: bool


@action
async def whoami(ctx: Context, session: Session) -> Me:
    try:
        credential = await sessions.credential_for(ctx, session.id)
        user = await ctx.forge.identity.user_of(credential)
    except Forbidden as exc:
        log.warning("identity.credential_refused", user_id=session.user_id)
        await sessions.revoke_now(ctx, session.id)
        raise SessionExpired("Sign in again.") from exc
    except Unavailable:
        return Me(user=User(id=session.user_id, username=session.username), roles=(), degraded=True)
    try:
        roles = await ctx.forge.orgs.roles_of(AsUser(user.id, credential))
    except Unavailable:
        return Me(user=user, roles=(), degraded=True)
    return Me(user=user, roles=await names.held_roles(ctx, roles), degraded=False)


@action
async def current(ctx: Context, session_id: uuid.UUID) -> Session:
    """The session behind an id, checked for its lifetimes."""
    return await sessions.authenticate(ctx, session_id)
