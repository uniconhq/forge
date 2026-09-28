"""The two ways a user steps away. Deactivate revokes every session and marks
the account inactive at the host, reversibly. Delete runs in a fixed order
and stops at the first refusal: a scope that would be left without an admin,
or a shared workflow other people may be using. Both need a session younger
than the fresh sign-in window.
"""

from forge.domain.errors import FreshSignInRequired, SharedWorkflowOwner, SoleAdmin
from forge.domain.identity import AsUser
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.sessions import Session, is_fresh
from forge.domain.workflows import Visibility
from forge.log import get_logger
from forge.port import Forge
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import sessions

log = get_logger(__name__)


@action
async def deactivate(ctx: Context, session: Session) -> None:
    _require_fresh(ctx, session)
    await sessions.revoke_all(ctx, session.user_id)
    await ctx.forge.identity.deactivate_user(session.user_id)
    log.info("account.deactivated", user_id=session.user_id)


@action
async def delete(ctx: Context, session: Session) -> None:
    _require_fresh(ctx, session)
    credential = await sessions.credential_for(ctx, session.id)
    grants = await ctx.forge.orgs.roles_of(AsUser(session.user_id, credential))
    await _refuse_if_sole_admin(ctx.forge, session.user_id, grants)
    await _refuse_if_sharing_workflows(ctx.forge, session.user_id)
    await sessions.revoke_all(ctx, session.user_id)
    for grant in grants:
        await ctx.forge.orgs.revoke_role(session.user_id, grant.scope, grant.role)
    await ctx.forge.identity.delete_user(session.user_id)
    log.info("account.deleted", user_id=session.user_id)


def _require_fresh(ctx: Context, session: Session) -> None:
    if not is_fresh(session.created_at, ctx.now, ctx.settings.fresh_sign_in_window):
        raise FreshSignInRequired("Sign in again to change your account.")


async def _refuse_if_sole_admin(forge: Forge, user_id: int, grants: tuple[RoleGrant, ...]) -> None:
    alone = [
        grant.scope
        for grant in grants
        if grant.role is Role.ADMIN and await _admins_of(forge, grant.scope) == {user_id}
    ]
    if alone:
        raise SoleAdmin(
            "Someone else has to be an admin of these first.",
            scopes=[{"kind": scope.kind.value, "name": scope.name} for scope in alone],
        )


async def _admins_of(forge: Forge, scope: Scope) -> set[int]:
    """Every admin of a scope, counting admins inherited from broader scopes."""
    admins: set[int] = set()
    for broader in scope.lineage():
        admins.update(user.id for user in await forge.orgs.holders_of(broader, Role.ADMIN))
    return admins


async def _refuse_if_sharing_workflows(forge: Forge, user_id: int) -> None:
    shared = [
        f"{workflow.owner}/{workflow.name}"
        for workflow in await forge.workflows.workflows_owned_by(user_id)
        if workflow.visibility is not Visibility.PRIVATE
    ]
    if shared:
        raise SharedWorkflowOwner(
            "Make these workflows private or hand them over first.", workflows=shared
        )
