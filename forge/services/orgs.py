"""Making an org, and the two things an org's admin may change about it
afterwards. `create` writes the request and answers at once; the
`provisioning` poller runs `provision`, the ten recorded steps that make
everything an org needs: its account row, the org itself, its roles, the
labels its threads are marked with, its signed event push, its first admin,
its service account at the forge, that account's place in the org and its
forge credential, its user at the CI, and its sign-in at the CI. A rerun
after a failure picks up where the record says it stopped. An org of that
name already at the forge is taken as made by an earlier try only when the
platform account owns it; a name someone else took between the check and
the step is refused, so nobody's org is ever taken over.

Org creation is open to any signed-in user while `UNICON_ORG_CREATION_OPEN`
is on; with it off the operator creates an org from the command line with
`create_by_operator`, which runs the same steps inline.
"""

from forge.db.tables import Provisioning
from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.ids import OrgName
from forge.domain.names import validate_org_name
from forge.domain.roles import Role, Scope, holds
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import org_accounts, provisioning
from forge.services.access import Organiser
from forge.services.events import EVENTS_PATH
from forge.services.passwords import new_password
from forge.services.provisioning import Attempt, Record
from forge.settings import Settings

log = get_logger(__name__)

KIND = "org"


@action
async def create(ctx: Context, session: Session, name: OrgName, *, description: str) -> Record:
    """Ask for the org to be made, as the signed-in user, who becomes its
    first admin. `Forbidden` while org creation is closed on this
    deployment; `Conflict` when the name is taken or being made.
    """
    if not ctx.settings.org_creation_open:
        raise Forbidden("Org creation is closed on this deployment; ask the operator.")
    await _refuse_taken(ctx, name)
    row = await provisioning.request(
        ctx, KIND, name, {"description": description, "creator_user_id": session.user_id}
    )
    log.info("orgs.requested", org=name, user_id=session.user_id)
    return provisioning.record_from(row)


@action
async def create_by_operator(
    ctx: Context, name: OrgName, *, description: str, admin_username: str
) -> Record:
    """Make the org now, whatever the setting says, with the named user as
    its first admin. The steps run inline on this unit of work, and the
    record that comes back says whether they all completed.
    """
    await _refuse_taken(ctx, name)
    try:
        admin = await ctx.forge.identity.find_user_by_username(admin_username)
    except NotFound as exc:
        raise NotFound(f"There is no user named {admin_username!r} at the forge.") from exc
    row = await provisioning.request(
        ctx, KIND, name, {"description": description, "creator_user_id": admin.id}
    )
    await provision(ctx, row)
    return provisioning.record_from(row)


async def provision(ctx: Context, row: Provisioning) -> None:
    """The poller's work over an org's row: the steps, in order, each safe to
    run again. The service account's password is made here, held for this
    attempt only, and never written; a rerun that starts after the account
    was made sets a fresh one first.
    """
    name = OrgName(row.target_id)
    description = str(row.payload.get("description", ""))
    creator = row.payload.get("creator_user_id")
    password: str | None = None

    async def account_row(attempt: Attempt) -> None:
        await org_accounts.ensure_row(ctx, name)

    async def make_org(attempt: Attempt) -> None:
        try:
            await ctx.forge.orgs.create_org(name, description=description)
        except Conflict:
            if not await ctx.forge.orgs.platform_owns(name):
                raise Conflict(
                    f"The name {name!r} was taken at the forge by someone else."
                ) from None

    async def make_roles(attempt: Attempt) -> None:
        await ctx.forge.orgs.create_roles(name)

    async def make_labels(attempt: Attempt) -> None:
        await ctx.forge.orgs.create_thread_labels(name)

    async def make_event_push(attempt: Attempt) -> None:
        secret = await org_accounts.event_secret_of(ctx, name)
        await ctx.forge.orgs.create_event_push(
            name, url=event_push_url(ctx.settings, name), secret=secret.decode()
        )

    async def make_first_admin(attempt: Attempt) -> None:
        if creator is not None:
            await ctx.forge.orgs.grant_role(int(creator), Scope(name), Role.ADMIN)

    async def make_service_account(attempt: Attempt) -> None:
        nonlocal password
        password = new_password()
        await org_accounts.create_forge_account(ctx, name, password)

    async def mint_service_token(attempt: Attempt) -> None:
        nonlocal password
        if password is None:
            password = new_password()
            await org_accounts.reset_password(ctx, name, password)
        await org_accounts.mint_forge_token(ctx, name, password)

    async def make_ci_user(attempt: Attempt) -> None:
        await org_accounts.create_ci_user(ctx, name)

    async def sign_in_at_ci(attempt: Attempt) -> None:
        nonlocal password
        if password is None:
            password = new_password()
            await org_accounts.reset_password(ctx, name, password)
        await org_accounts.sign_in_at_ci(ctx, name, password)

    await provisioning.run(
        ctx,
        row,
        provisioning.steps(
            KIND,
            {
                "account_row": account_row,
                "org": make_org,
                "roles": make_roles,
                "labels": make_labels,
                "event_push": make_event_push,
                "first_admin": make_first_admin,
                "service_account": make_service_account,
                "service_token": mint_service_token,
                "ci_user": make_ci_user,
                "ci_login": sign_in_at_ci,
            },
        ),
    )


@action
async def update(
    ctx: Context,
    organiser: Organiser,
    name: OrgName,
    *,
    description: str,
    display_name: str | None = None,
) -> None:
    """Change the org's description, and its display name when given, as the
    platform account: the forge lets only an owner change them, and the
    platform account is the one owner. The organiser must hold admin at the
    org.
    """
    if not holds(organiser.grants, Scope(name), Role.ADMIN):
        raise Forbidden(f"This needs the admin role at {name}.")
    await ctx.forge.orgs.update_org(name, description=description, display_name=display_name)
    log.info("orgs.updated", org=name, user_id=organiser.user.id)


@action
async def status(ctx: Context, session: Session, name: OrgName) -> Record | None:
    """Where making the org has got to, for the person who asked for it, or
    none when nothing has asked for it or someone else did. Once the org is
    made, its organisers read it at the forge like any other scope.
    """
    row = await provisioning.find(ctx, KIND, name)
    if row is None or row.payload.get("creator_user_id") != session.user_id:
        return None
    return provisioning.record_from(row)


async def _refuse_taken(ctx: Context, name: OrgName) -> None:
    """Refuse a name that breaks the rule, or that someone or something at the
    forge already has, before anything is written. A name asked for here
    already has its provisioning row, which `provisioning.request` refuses.
    """
    validate_org_name(name)
    if await provisioning.find(ctx, KIND, name) is None and await ctx.forge.orgs.name_taken(name):
        raise Conflict(f"The name {name!r} is taken at the forge.")


def event_push_url(settings: Settings, org: OrgName) -> str:
    """Where the forge sends the org's events: the platform's internal URL,
    the one host the forge is allowed to call.
    """
    return f"{str(settings.internal_url).rstrip('/')}{EVENTS_PATH}/{org}"
