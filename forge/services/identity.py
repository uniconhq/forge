"""Who a session belongs to and which roles they hold. The identity is read
from the host with the session's credential; the roles are read live and
never stored. `current` is what the host's guard calls before every route's
action: it checks the session and keeps its credential fresh, so the action
that follows reads a credential that is good for minutes yet.
"""

import uuid
from dataclasses import dataclass

from forge.domain.errors import Forbidden, SessionExpired, Unavailable
from forge.domain.identity import AsUser, User
from forge.domain.names import ScopeNames
from forge.domain.roles import HeldRole
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import ActionSetup, Context
from forge.runtime.held import setup_or_held
from forge.services import names, sessions, workflows

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Me:
    """The signed-in user and their roles at every scope, each with the names
    of where it is held; the orgs a role of theirs reaches, at the org or
    inside it, by name; the contests a role of theirs is held at or inside,
    and the tasks one is held at, by name only, for the pages to name where
    a list they may not read is; and the owners they may make a workflow
    under (`workflows.owners`). `degraded` is set when the host did not
    answer and the identity comes from the session alone.
    """

    user: User
    roles: tuple[HeldRole, ...]
    degraded: bool
    orgs: tuple[str, ...]
    workflow_owners: tuple[str, ...]
    contests: tuple[ScopeNames, ...] = ()
    tasks: tuple[ScopeNames, ...] = ()


@action
async def whoami(ctx: Context, session: Session) -> Me:
    try:
        credential = await sessions.credential_for(ctx, session.id)
        await ctx.let_go()
        user = await ctx.forge.identity.user_of(credential)
    except Forbidden as exc:
        log.warning("identity.credential_refused", user_id=session.user_id)
        sessions.revoke_at_end(ctx, session.id)
        raise SessionExpired("Your session has ended. Sign in again.") from exc
    except Unavailable:
        user = User(id=session.user_id, username=session.username)
        return Me(user, (), True, (), tuple(workflows.owners((), user.username, ())))
    try:
        roles = await ctx.forge.orgs.roles_of(AsUser(user.id, credential))
    except Unavailable:
        return Me(user, (), True, (), tuple(workflows.owners((), user.username, ())))
    held = await names.held_roles(ctx, roles)
    orgs = {each.scope.org: each.names.org for each in held}
    contests = {
        ScopeNames(each.names.org, each.names.contest) for each in held if each.names.contest
    }
    tasks = {each.names for each in held if each.names.task}
    return Me(
        user,
        held,
        False,
        orgs=tuple(sorted(set(orgs.values()))),
        workflow_owners=tuple(workflows.owners(roles, user.username, orgs.items())),
        contests=tuple(sorted(contests, key=lambda each: each.path)),
        tasks=tuple(sorted(tasks, key=lambda each: each.path)),
    )


async def current(session_id: uuid.UUID, *, setup: ActionSetup | None = None) -> Session:
    """The session behind an id, checked for its lifetimes, with its
    credential refreshed when it is close to expiry. Each step is a
    transaction of its own and the host is asked while none is open, so the
    refresh, which must land whatever the route's action does, never waits
    on the pool while holding a connection from it.
    """
    on = setup_or_held(setup)
    session, due = await _checked(on, session_id)
    if due:
        await sessions.keep_fresh(on, session_id)
    return session


@action
async def _checked(ctx: Context, session_id: uuid.UUID) -> tuple[Session, bool]:
    return await sessions.check(ctx, session_id)
