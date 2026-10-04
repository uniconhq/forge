"""An org's service account, `unicon-ci-<org>`: the one account that
activates the org's tasks at the CI and starts their runs. It is a user at
the forge, a member of the org account's place in the org and nothing else,
and a user at the CI. The `org_accounts` row holds its two credentials and
the secret the org's event push is signed with, each as ciphertext; its
password at the forge is never stored. Whenever the account has to sign in
again, the package sets a fresh password with its administrator rights,
signs in with it, and throws it away.

The building blocks here are the steps making an org runs, and `identity`
and `event_secret_of` are what the rest of the package reads.

The CI keeps the account's login at the forge fresh only while the account
calls it, and that login lasts as long as the forge's refresh token, which
deploy sets to the session's hard lifetime, `UNICON_SESSION_HARD_TTL`, 30
days unless the operator says otherwise; an org that grades nothing for
longer would lose it. `identity` therefore signs the account in at the CI
again before handing it out whenever its last sign-in there is older than
`SIGN_IN_SHARE` of that lifetime, 20 days by default: an org that grades
every day signs in again every 20 days, and one that was quiet for months
signs in on its first use. A login the CI refuses for any other reason, a
restored CI database or a revoked token, is signed in again by `renew`,
which whoever was refused calls before trying once more.
"""

import secrets
from datetime import datetime

from sqlalchemy import select

from forge.db.tables import OrgAccount
from forge.domain.errors import NotFound
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import OrgId
from forge.domain.names import service_account_name
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import credentials
from forge.services.passwords import new_password

log = get_logger(__name__)

TOKEN_NAME = "unicon"
TOKEN_SCOPES = ("read:user", "read:organization", "read:repository")
EMAIL_DOMAIN = "unicon.invalid"
SECRET_BYTES = 32
SIGN_IN_SHARE = 2 / 3


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
            ci_token=b"",
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


async def create_ci_user(ctx: Context, org: OrgId) -> None:
    row = await _row(ctx, org)
    row.ci_user_id = await ctx.forge.grading.create_ci_user(row.username)
    await ctx.db.flush()


async def sign_in_at_ci(ctx: Context, org: OrgId, password: str) -> None:
    """Sign the account in at the CI with `password` and keep the CI
    credential that comes out of it.
    """
    row = await _row(ctx, org)
    token = await ctx.forge.grading.mint_ci_token(row.username, password)
    row.ci_token = credentials.encrypt_text(token, ctx.settings)
    row.ci_signed_in_at = ctx.now
    await ctx.db.flush()


async def identity(ctx: Context, org: OrgId) -> AsOrgAccount:
    """The account as a call through the port is made under it, signed in
    at the CI again first when that sign-in is older than `SIGN_IN_SHARE` of
    the session's hard lifetime. `NotFound` when the org has no account with
    both credentials.
    """
    row = await _row(ctx, org)
    if row.ci_token and _stale(ctx, row.ci_signed_in_at):
        row = await _row(ctx, org, lock=True)
        if _stale(ctx, row.ci_signed_in_at):
            await _sign_in_again(ctx, row)
            log.info("org_accounts.signed_in_again", org=org, reason="age")
    return _identity(row, ctx)


async def renew(ctx: Context, refused: AsOrgAccount) -> AsOrgAccount:
    """The account signed in at the CI again, for a caller the CI refused
    under `refused`. When another caller signed it in meanwhile, its new
    credential is handed out as it is, so a burst of refusals signs in once.
    """
    row = await _row(ctx, OrgId(refused.org), lock=True)
    if _identity(row, ctx).ci_token == refused.ci_token:
        await _sign_in_again(ctx, row)
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


def _stale(ctx: Context, signed_in_at: datetime | None) -> bool:
    longest = ctx.settings.session_hard_ttl * SIGN_IN_SHARE
    return signed_in_at is None or ctx.now - signed_in_at > longest


async def _sign_in_again(ctx: Context, row: OrgAccount) -> None:
    if row.forge_user_id is None:
        raise NotFound(f"the org account of {row.org_id} has not been made at the forge")
    password = new_password()
    await ctx.forge.identity.set_password(row.forge_user_id, password)
    token = await ctx.forge.grading.mint_ci_token(row.username, password)
    row.ci_token = credentials.encrypt_text(token, ctx.settings)
    row.ci_signed_in_at = ctx.now
    await ctx.db.flush()


def _identity(row: OrgAccount, ctx: Context) -> AsOrgAccount:
    if not row.forge_token or not row.ci_token:
        raise NotFound(f"the org account of {row.org_id} is not ready")
    return AsOrgAccount(
        row.org_id,
        forge_token=credentials.decrypt_text(row.forge_token, ctx.settings),
        ci_token=credentials.decrypt_text(row.ci_token, ctx.settings),
    )


async def _row(ctx: Context, org: OrgId, *, lock: bool = False) -> OrgAccount:
    row = await _find(ctx, org, lock=lock)
    if row is None:
        raise NotFound(f"the org {org} has no account")
    return row


async def _find(ctx: Context, org: OrgId, *, lock: bool = False) -> OrgAccount | None:
    """The org's row, held until the unit of work ends and read afresh when
    `lock`, so two units of work never sign the account in at once.
    """
    query = select(OrgAccount).where(OrgAccount.org_id == org)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await ctx.db.execute(query)).scalar_one_or_none()
