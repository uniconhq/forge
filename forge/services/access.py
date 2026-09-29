"""Who may do what. `organiser` is the one action that reads a person's
roles: it checks the session, reads the roles once with the person's own
credential, and either refuses or returns an `Organiser`, which the other
organiser actions take in place of a session. So a request reads roles once,
in the host's guard, and the action it then calls reads none: `require`
checks the `Organiser` against the scope the action acts on.
"""

from dataclasses import dataclass, field

from forge.domain.errors import Forbidden, SessionExpired
from forge.domain.identity import AsUser, User
from forge.domain.roles import Role, RoleGrant, Scope, holds
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import sessions

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Organiser:
    """A person checked to hold at least `role` at `scope`, with every role
    they hold, and the identity a call for them through the port is made
    under. The identity carries their credential and is kept out of `repr`.
    """

    user: User
    grants: tuple[RoleGrant, ...]
    scope: Scope
    role: Role
    identity: AsUser = field(repr=False)


@action
async def organiser(ctx: Context, session: Session, scope: Scope, role: Role) -> Organiser:
    """The session's user as an organiser holding at least `role` at
    `scope`, counting roles inherited from broader scopes and higher roles.
    `Forbidden`, naming the scope and the role, when they do not.
    """
    try:
        credential = await sessions.credential_for(ctx, session.id)
        grants = await ctx.forge.orgs.roles_of(AsUser(session.user_id, credential))
    except Forbidden as exc:
        log.warning("access.credential_refused", user_id=session.user_id)
        await sessions.revoke_now(ctx, session.id)
        raise SessionExpired("Sign in again.") from exc
    if not holds(grants, scope, role):
        log.info("access.refused", user_id=session.user_id, scope=scope.name, role=role.value)
        raise Forbidden(f"This needs the {role.value} role at {scope.name}.")
    return Organiser(
        user=User(id=session.user_id, username=session.username),
        grants=grants,
        scope=scope,
        role=role,
        identity=AsUser(session.user_id, credential),
    )


def require(organiser: Organiser, scope: Scope, role: Role) -> None:
    """Refuse with `Forbidden`, naming the scope and the role, unless the
    organiser holds at least `role` at `scope`, counting inherited roles.
    """
    if not holds(organiser.grants, scope, role):
        raise Forbidden(f"This needs the {role.value} role at {scope.name}.")
