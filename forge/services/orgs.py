"""Making an org, the two things an org's admin may change about it
afterwards, and reading them back. `create` makes everything an org needs before it answers: its
account row, the org itself, its roles, the labels its threads are marked
with, its signed event push, its first admin, its service account at the
forge, that account's place in the org and its forge credential, its user at
the CI, and its sign-in at the CI. A step that fails fails the whole
request, which writes nothing here, and what the try already made at the
forge and the CI is removed again, the latest first, before the person is
told: the account's user at the CI, its place in the org, the account, and
the org with everything in it. That is best effort; what a removal could
not remove is logged (`forge.services.making`). The person asks again.

The org's name is a label: `create` reserves it and the org is made at the
forge under its key, so a rename never moves it and a name freed and taken
again names a new org. A name is refused when the forge has a person or an
org called that, so a workflow named `<owner>/<name>` means one owner.

Org creation is open to any signed-in user while `UNICON_ORG_CREATION_OPEN`
is on; with it off the operator creates an org from the command line with
`create_by_operator`, which makes it the same way.
"""

from forge.domain.errors import Conflict, Forbidden, NotFound, PortError
from forge.domain.ids import OrgId
from forge.domain.names import Named, OrgProfile, service_account_name, validate_org_name
from forge.domain.roles import Role, Scope, holds
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import making, names, org_accounts
from forge.services.access import Organiser
from forge.services.events import EVENTS_PATH
from forge.services.passwords import new_password
from forge.settings import Settings

log = get_logger(__name__)


@action
async def create(ctx: Context, session: Session, name: str, *, description: str) -> Named:
    """Make the org, as the signed-in user, who becomes its first admin.
    `Forbidden` while org creation is closed on this deployment; `Conflict`
    when the name is taken.
    """
    if not ctx.settings.org_creation_open:
        raise Forbidden("Org creation is closed on this deployment; ask the operator.")
    org = await _reserve(ctx, name)
    await _make(ctx, org, description=description, admin=session.user_id)
    log.info("orgs.created", org=org, user_id=session.user_id)
    return Named(org, name)


@action
async def create_by_operator(
    ctx: Context, name: str, *, description: str, admin_username: str
) -> Named:
    """Make the org now, whatever the setting says, with the named user as
    its first admin.
    """
    try:
        admin = await ctx.forge.identity.find_user_by_username(admin_username)
    except NotFound as exc:
        raise NotFound(f"There is no user named {admin_username!r} at the forge.") from exc
    org = await _reserve(ctx, name)
    await _make(ctx, org, description=description, admin=admin.id)
    log.info("orgs.created", org=org, user_id=admin.id)
    return Named(org, name)


async def _make(ctx: Context, org: OrgId, *, description: str, admin: int) -> None:
    """Everything the org needs, in order, and a failure in the platform's
    words (`making.failure`). What the steps made is removed again if the
    request fails (`making.undo_on_rollback`).
    """
    made = making.undo_on_rollback(ctx, "orgs", org=org)
    try:
        await _steps(ctx, org, made, description=description, admin=admin)
    except PortError as exc:
        raise making.failure(exc, "orgs.step_failed", org=org) from None


async def _steps(
    ctx: Context, org: OrgId, made: making.Made, *, description: str, admin: int
) -> None:
    """The service account's password is made here, used to mint its two
    credentials, and never written.

    What the steps make is noted in `made`. The org is noted once it is
    made, and removing it takes what was made in it, its roles, labels,
    event push and the admin's role, along. The account is noted once it is
    made, by the id the forge gave it: a username someone else holds is
    refused, and their account is never noted. Its place in the org and its
    user at the CI belong to it and are noted before the steps that make
    them, so a step that fails halfway is undone too; the forge deletes an
    account only once it has left its place, which is why that is noted
    after the account and so removed before it.
    """
    await org_accounts.ensure_row(ctx, org)
    await ctx.forge.orgs.create_org(org, description=description)
    made.add("org", org, lambda: ctx.forge.orgs.delete_org(org))
    await ctx.forge.orgs.create_roles(org)
    await ctx.forge.orgs.create_thread_labels(org)
    secret = await org_accounts.event_secret_of(ctx, org)
    await ctx.forge.orgs.create_event_push(
        org, url=event_push_url(ctx.settings, org), secret=secret.decode()
    )
    await ctx.forge.orgs.grant_role(admin, Scope(org), Role.ADMIN)
    password = new_password()
    account = await org_accounts.create_forge_account(ctx, org, password)
    made.add("service_account", str(account), lambda: ctx.forge.identity.delete_user(account))
    made.add(
        "account_membership",
        str(account),
        lambda: ctx.forge.orgs.remove_account_membership(org, account),
    )
    await org_accounts.join_org(ctx, org)
    await org_accounts.mint_forge_token(ctx, org, password)
    username = service_account_name(org)
    made.add("ci_user", username, lambda: ctx.forge.grading.delete_ci_user(username))
    await org_accounts.create_ci_user(ctx, org)
    await org_accounts.sign_in_at_ci(ctx, org, password)


@action
async def update(
    ctx: Context,
    organiser: Organiser,
    org: OrgId,
    *,
    description: str,
    display_name: str | None = None,
) -> None:
    """Change the org's description, and its display name when given, as the
    platform account: the forge lets only an owner change them, and the
    platform account is the one owner. The organiser must hold admin at the
    org.
    """
    if not holds(organiser.grants, Scope(org), Role.ADMIN):
        named = organiser.scope if organiser.scope == Scope(org) else Scope(org)
        raise Forbidden(f"This needs the admin role at {named.name}.")
    await ctx.forge.orgs.update_org(org, description=description, display_name=display_name)
    log.info("orgs.updated", org=org, user_id=organiser.user.id)


@action
async def read(ctx: Context, organiser: Organiser, org: OrgId) -> OrgProfile:
    """The org's display name and description as they stand at the forge,
    read as the platform, for an organiser holding any role in the org, at
    the org or at anything in it.
    """
    if not any(grant.scope.org == org for grant in organiser.grants):
        named = organiser.scope if organiser.scope == Scope(org) else Scope(org)
        raise Forbidden(f"This needs a role in {named.name}.")
    return await ctx.forge.orgs.read_org(org)


async def _reserve(ctx: Context, name: str) -> OrgId:
    """Reserve a name that keeps the rule and that nobody at the forge has,
    and hand back the new org's id. `Conflict` when another org has the
    name, or a person or an org at the forge is called that.
    """
    validate_org_name(name)
    if await names.org_id(ctx, name) is not None:
        raise Conflict(f"The name {name!r} is taken.")
    if await ctx.forge.orgs.name_taken(name):
        raise Conflict(f"The name {name!r} is taken at the forge.")
    return await names.reserve_org(ctx, name)


def event_push_url(settings: Settings, org: OrgId) -> str:
    """Where the forge sends the org's events: the platform's internal URL,
    the one host the forge is allowed to call.
    """
    return f"{str(settings.internal_url).rstrip('/')}{EVENTS_PATH}/{org}"
