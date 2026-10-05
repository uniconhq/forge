"""How an account is made when sign-up is closed, and the two ways a user
steps away. Create makes the account at the host with a first password the
person must change, for the operator to hand over once. Deactivate revokes
every session and marks the account inactive at the host, reversibly. Delete
runs in a fixed order and stops at the first refusal: a scope that would be
left without an admin, or a shared workflow other people may be using. It
holds the role lock of every org the person has a role in while it checks
and removes those roles, the way a change of roles does. Deactivate and
delete need a session younger than the fresh sign-in window.
"""

from forge.domain.errors import (
    FreshSignInRequired,
    InvalidName,
    SharedWorkflowOwner,
)
from forge.domain.identity import AsUser, User
from forge.domain.names import is_service_account
from forge.domain.roles import Role, RoleGrant
from forge.domain.sessions import Session, is_fresh
from forge.domain.workflows import Visibility
from forge.log import get_logger
from forge.port import Forge
from forge.runtime.actions import action
from forge.runtime.context import AfterRollback, Context
from forge.services import invites, names, roles, sessions
from forge.services.passwords import new_password

log = get_logger(__name__)


@action
async def create(ctx: Context, username: str, *, email: str) -> tuple[User, str]:
    """Make a person's account at the host, for the operator, and return it
    with its first password, which is shown once and must be changed at the
    first sign-in. A name reserved for service accounts is refused.
    """
    if not username.strip():
        raise InvalidName("A username is needed.")
    if is_service_account(username):
        raise InvalidName(f"{username!r} is reserved for org service accounts.")
    password = new_password()
    user = await ctx.forge.identity.create_user(
        username, email, password, must_change_password=True
    )
    log.info("account.created", user_id=user.id, username=user.username)
    return user, password


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
    for org in sorted({grant.scope.org for grant in grants}):
        await roles.one_change_at_a_time(ctx, org)
    await _refuse_if_sole_admin(ctx, session.user_id, grants)
    await _refuse_if_sharing_workflows(ctx.forge, session.user_id)
    await sessions.revoke_all(ctx, session.user_id)
    await invites.forget_person(ctx, session.user_id)
    for grant in grants:
        await ctx.forge.orgs.revoke_role(session.user_id, grant.scope, grant.role)
        # The forge refuses to delete someone who still holds a role, so the
        # roles go first; if the delete then fails, they are given back.
        ctx.after_rollback(_give_back(ctx, session.user_id, grant))
    await ctx.forge.identity.delete_user(session.user_id)
    log.info("account.deleted", user_id=session.user_id)


def _give_back(ctx: Context, user_id: int, grant: RoleGrant) -> AfterRollback:
    async def give_back() -> None:
        await ctx.forge.orgs.grant_role(user_id, grant.scope, grant.role)

    return give_back


def _require_fresh(ctx: Context, session: Session) -> None:
    if not is_fresh(session.created_at, ctx.now, ctx.settings.fresh_sign_in_window):
        raise FreshSignInRequired("Sign in again to change your account.")


async def _refuse_if_sole_admin(ctx: Context, user_id: int, grants: tuple[RoleGrant, ...]) -> None:
    alone = [
        await names.labelled(ctx, grant.scope)
        for grant in grants
        if grant.role is Role.ADMIN and await roles.admins_of(ctx, grant.scope) == {user_id}
    ]
    if alone:
        raise roles.sole_admin(alone, "Someone else has to be an admin of these first.")


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
