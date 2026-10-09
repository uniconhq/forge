"""An org's service account, `unicon-ci-<org>`: the one account that
activates the org's tasks at the CI and starts their runs. It is a user at
the forge, a member of the org account's place in the org and nothing else,
and whatever the CI's implementation sets up for it there. The
`org_accounts` row holds its forge credential, what it holds at the CI and
the secret the org's event push is signed with, each as ciphertext; its
password at the forge is never stored.

The building blocks here are the steps making an org runs, and `identity`
and `event_secret_of` are what the rest of the package reads.

What the account holds at the CI is the implementation's own, and so is
when it goes stale: `identity` refreshes it first whenever the
implementation says it needs refreshing, holding the row so two callers
never refresh it at once. A state the CI refuses for any other reason, a
restored CI database or a revoked credential, is refreshed by `renew`,
which whoever was refused calls before trying once more.
"""

import secrets

from sqlalchemy import select

from forge.db.tables import OrgAccount
from forge.domain.errors import NotFound
from forge.domain.identity import AsOrgAccount, CiState, OrgAccountRef
from forge.domain.ids import OrgId
from forge.domain.names import service_account_name
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import credentials

log = get_logger(__name__)

TOKEN_NAME = "unicon"
TOKEN_SCOPES = ("read:user", "read:organization", "read:repository")
EMAIL_DOMAIN = "unicon.invalid"
SECRET_BYTES = 32


async def ensure_row(ctx: Context, org: OrgId) -> None:
    """The org's row with a fresh event secret and no credentials yet; the
    steps after this one fill it in. One that is there already is kept.
    """
    if await _find(ctx, org) is not None:
        return
    ctx.db.add(
        OrgAccount(
            org_id=org,
            username=service_account_name(org),
            forge_token=b"",
            ci_state=b"",
            event_secret=credentials.encrypt_text(
                secrets.token_urlsafe(SECRET_BYTES), ctx.settings
            ),
        )
    )
    await ctx.db.flush()


async def create_forge_account(ctx: Context, org: OrgId, password: str) -> int:
    """Make the account at the forge with `password`, record its id and
    hand it back. A username someone already took at the forge is refused
    with `Conflict`, since anyone may sign up there and nobody may be handed
    the org account's place.
    """
    row = await _row(ctx, org)
    user = await ctx.forge.identity.create_user(
        row.username,
        f"{row.username}@{EMAIL_DOMAIN}",
        password,
        must_change_password=False,
        visibility="private",
    )
    row.forge_user_id = user.id
    await ctx.db.flush()
    return user.id


async def join_org(ctx: Context, org: OrgId) -> None:
    """Put the account in its place in the org."""
    row = await _row(ctx, org)
    if row.forge_user_id is None:
        raise NotFound(f"the org account of {org} has not been made at the forge")
    await ctx.forge.orgs.ensure_account_membership(org, row.forge_user_id)


async def mint_forge_token(ctx: Context, org: OrgId, password: str) -> None:
    """Mint the account's forge credential with `password`."""
    row = await _row(ctx, org)
    token = await ctx.forge.identity.mint_token(
        row.username, password, name=TOKEN_NAME, scopes=TOKEN_SCOPES
    )
    row.forge_token = credentials.encrypt_text(token, ctx.settings)
    await ctx.db.flush()


async def set_up_at_ci(ctx: Context, org: OrgId, password: str) -> None:
    """Set the org up at the CI for its account, which `password` signs in
    at the forge, and keep what the account holds there.
    """
    row = await _row(ctx, org)
    if row.forge_user_id is None:
        raise NotFound(f"the org account of {org} has not been made at the forge")
    state = await ctx.forge.grading.set_up_org(
        org, OrgAccountRef(row.username, row.forge_user_id, password)
    )
    _keep(ctx, row, state)
    await ctx.db.flush()


async def identity(ctx: Context, org: OrgId) -> AsOrgAccount:
    """The account as a call through the port is made under it, its state
    at the CI refreshed first when the CI's implementation says it needs
    it. `NotFound` when the org has no account ready at both.
    """
    row = await _row(ctx, org)
    if row.ci_state and ctx.forge.grading.needs_refresh(_state(ctx, row), ctx.now):
        row = await _row(ctx, org, lock=True)
        if ctx.forge.grading.needs_refresh(_state(ctx, row), ctx.now):
            await _refresh(ctx, row)
            log.info("org_accounts.signed_in_again", org=org, reason="age")
    return _identity(row, ctx)


async def renew(ctx: Context, refused: AsOrgAccount) -> AsOrgAccount:
    """The account's state at the CI refreshed, for a caller the CI refused
    under `refused`. When another caller refreshed it meanwhile, its new
    state is handed out as it is, so a burst of refusals refreshes once.
    """
    row = await _row(ctx, OrgId(refused.org), lock=True)
    if _identity(row, ctx).ci_state == refused.ci_state:
        await _refresh(ctx, row)
        log.warning("org_accounts.signed_in_again", org=refused.org, reason="refused")
    return _identity(row, ctx)


async def event_secret_of(ctx: Context, org: OrgId) -> bytes:
    """The secret the org's event push is signed with. `NotFound` when the
    org has no account row.
    """
    row = await _row(ctx, org)
    return credentials.decrypt_text(row.event_secret, ctx.settings).encode()


async def org_ids(ctx: Context) -> tuple[OrgId, ...]:
    """Every org the platform made, the ones with an account row, by key.
    Their names are in the `names` table.
    """
    found = await ctx.db.execute(select(OrgAccount.org_id).order_by(OrgAccount.org_id))
    return tuple(OrgId(name) for name in found.scalars())


async def service_account_ids(ctx: Context) -> frozenset[int]:
    """The forge ids of every org's service account, the accounts the
    platform made itself. A user is a service account exactly when their id
    is here: anyone may sign up under a name that looks like one, so the
    name decides nothing.
    """
    found = await ctx.db.execute(
        select(OrgAccount.forge_user_id).where(OrgAccount.forge_user_id.is_not(None))
    )
    return frozenset(user_id for user_id in found.scalars() if user_id is not None)


async def _refresh(ctx: Context, row: OrgAccount) -> None:
    if row.forge_user_id is None:
        raise NotFound(f"the org account of {row.org_id} has not been made at the forge")
    _keep(ctx, row, await ctx.forge.grading.refresh(OrgId(row.org_id), _state(ctx, row)))
    await ctx.db.flush()


def _state(ctx: Context, row: OrgAccount) -> CiState:
    return CiState(credentials.decrypt_text(row.ci_state, ctx.settings))


def _keep(ctx: Context, row: OrgAccount, state: CiState) -> None:
    row.ci_state = credentials.encrypt_text(state, ctx.settings)


def _identity(row: OrgAccount, ctx: Context) -> AsOrgAccount:
    if not row.forge_token or not row.ci_state:
        raise NotFound(f"the org account of {row.org_id} is not ready")
    return AsOrgAccount(
        row.org_id,
        forge_token=credentials.decrypt_text(row.forge_token, ctx.settings),
        ci_state=_state(ctx, row),
    )


async def _row(ctx: Context, org: OrgId, *, lock: bool = False) -> OrgAccount:
    row = await _find(ctx, org, lock=lock)
    if row is None:
        raise NotFound(f"the org {org} has no account")
    return row


async def _find(ctx: Context, org: OrgId, *, lock: bool = False) -> OrgAccount | None:
    """The org's row, held until the unit of work ends and read afresh when
    `lock`, so two units of work never refresh the account at once.
    """
    query = select(OrgAccount).where(OrgAccount.org_id == org)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await ctx.db.execute(query)).scalar_one_or_none()
