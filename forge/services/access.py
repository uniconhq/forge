"""Who may do what. `organiser` is the one action that reads a person's
roles: it checks the session, reads the roles once with the person's own
credential, and either refuses or returns an `Organiser`, which the other
organiser actions take in place of a session. So a request reads roles once,
in the host's guard, and the action it then calls reads none: `require`
checks the `Organiser` against the scope the action acts on.

`organiser_at` is the same check for an address, by the names in it. A
scope that is not there is refused the way one the person may not reach
is, `Forbidden`, unless they hold the role at the deepest part of it that
is there, when it is `NotFound`: so nobody learns which contests or tasks
exist from a route they may not use.
"""

from dataclasses import dataclass, field

from forge.domain.errors import Forbidden, NotFound, SessionExpired
from forge.domain.identity import AsUser, User
from forge.domain.roles import Role, RoleGrant, Scope, holds
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, sessions

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
        await ctx.let_go()
        grants = await ctx.forge.orgs.roles_of(AsUser(session.user_id, credential))
    except Forbidden as exc:
        log.warning("access.credential_refused", user_id=session.user_id)
        sessions.revoke_at_end(ctx, session.id)
        raise SessionExpired("Sign in again.") from exc
    if not holds(grants, scope, role):
        log.info("access.refused", user_id=session.user_id, scope=scope.path, role=role.value)
        raise Forbidden(f"This needs the {role.value} role at {scope.name}.")
    return Organiser(
        user=User(id=session.user_id, username=session.username),
        grants=grants,
        scope=scope,
        role=role,
        identity=AsUser(session.user_id, credential),
    )


@action
async def organiser_at(
    ctx: Context,
    session: Session,
    role: Role,
    org: str,
    contest: str | None = None,
    task: str | None = None,
) -> Organiser:
    """`organiser` at the scope the names name. `Forbidden` naming the
    address when the person does not hold `role` there or above, and
    `NotFound` naming the first missing part when they do and it is not all
    there.
    """
    found, missing = await names.deepest(ctx, org, contest, task)
    if missing is None:
        assert found is not None
        return await organiser(ctx, session, found, role)
    address = "/".join(part for part in (org, contest, task) if part is not None)
    refused = Forbidden(f"This needs the {role.value} role at {address}.")
    if found is None:
        raise refused
    try:
        await organiser(ctx, session, found, role)
    except Forbidden:
        raise refused from None
    raise NotFound(f"There is no {missing}.")


def require(organiser: Organiser, scope: Scope, role: Role) -> None:
    """Refuse with `Forbidden`, naming the scope and the role, unless the
    organiser holds at least `role` at `scope`, counting inherited roles. The
    scope is named as the organiser's address named it when it is that one.
    """
    if not holds(organiser.grants, scope, role):
        named = organiser.scope if scope == organiser.scope else scope
        raise Forbidden(f"This needs the {role.value} role at {named.name}.")
