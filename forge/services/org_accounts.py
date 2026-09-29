"""An org's service account, `unicon-ci-<org>`: the one account that
activates the org's tasks at the CI and starts their runs. It is a user at
the forge, a member of the org account's place in the org and nothing else,
and a user at the CI. The `org_accounts` row holds its two credentials and
the secret the org's event push is signed with, each as ciphertext; its
password at the forge is never stored. Whenever the account has to sign in
again, the package sets a fresh password with its administrator rights,
signs in with it, and throws it away.

The building blocks here are the steps `orgs.provision` runs, `identity`
and `event_secret_of` are what the rest of the package reads, and
`keepalive` and `restore_membership` are the nightly pass's two checks on
every account.
"""

import secrets
from collections.abc import Sequence

from sqlalchemy import select

from forge.db.tables import OrgAccount
from forge.domain.errors import Conflict, NotFound, PortError
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import OrgName
from forge.domain.names import service_account_name
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import credentials
from forge.services.credentials import CannotDecrypt
from forge.services.passwords import new_password

log = get_logger(__name__)

TOKEN_NAME = "unicon"
TOKEN_SCOPES = ("read:user", "read:organization", "read:repository")
EMAIL_DOMAIN = "unicon.invalid"
SECRET_BYTES = 32


async def ensure_row(ctx: Context, org: OrgName) -> None:
    """The org's row with a fresh event secret and no credentials yet, made
    once; the steps after this one fill it in.
    """
    if await _find(ctx, org) is not None:
        return
    ctx.db.add(
        OrgAccount(
            org_name=org,
            username=service_account_name(org),
            forge_token=b"",
            ci_token=b"",
            event_secret=credentials.encrypt_text(
                secrets.token_urlsafe(SECRET_BYTES), ctx.settings
            ),
        )
    )
    await ctx.db.flush()


async def create_forge_account(ctx: Context, org: OrgName, password: str) -> None:
    """Make the account at the forge with `password` and record its id. A
    rerun finds the account the row names and gives it the password instead.
    A username taken by an account the row does not name is refused: anyone
    may sign up at the forge, and a person who took the name first must not
    be handed the org account's place. The same refusal meets an account
    the forge made on a call that failed before the row recorded it; the
    operator removes that account at the forge and the next try makes it.
    """
    row = await _row(ctx, org)
    try:
        user = await ctx.forge.identity.create_user(
            row.username,
            f"{row.username}@{EMAIL_DOMAIN}",
            password,
            must_change_password=False,
            visibility="private",
        )
    except Conflict:
        user = await ctx.forge.identity.find_user_by_username(row.username)
        if user.id != row.forge_user_id:
            log.warning("org_accounts.username_taken", org=org, username=row.username)
            raise Conflict(
                f"{row.username} is taken at the forge by an account the platform did not make"
            ) from None
        await ctx.forge.identity.set_password(user.id, password)
    row.forge_user_id = user.id
    await ctx.db.flush()


async def mint_forge_token(ctx: Context, org: OrgName, password: str) -> None:
    """Put the account in its place in the org and mint its forge credential
    with `password`.
    """
    row = await _row(ctx, org)
    if row.forge_user_id is None:
        raise NotFound(f"the org account of {org} has not been made at the forge")
    await ctx.forge.orgs.ensure_account_membership(org, row.forge_user_id)
    token = await ctx.forge.identity.mint_token(
        row.username, password, name=TOKEN_NAME, scopes=TOKEN_SCOPES
    )
    row.forge_token = credentials.encrypt_text(token, ctx.settings)
    await ctx.db.flush()


async def create_ci_user(ctx: Context, org: OrgName) -> None:
    row = await _row(ctx, org)
    row.ci_user_id = await ctx.forge.grading.create_ci_user(row.username)
    await ctx.db.flush()


async def reset_password(ctx: Context, org: OrgName, password: str) -> None:
    """A fresh password for the account at the forge, set with the platform's
    administrator rights, for a sign-in that has no password in hand.
    """
    row = await _row(ctx, org)
    if row.forge_user_id is None:
        raise NotFound(f"the org account of {org} has not been made at the forge")
    await ctx.forge.identity.set_password(row.forge_user_id, password)


async def sign_in_at_ci(ctx: Context, org: OrgName, password: str) -> None:
    """Sign the account in at the CI with `password` and keep the CI
    credential that comes out of it.
    """
    row = await _row(ctx, org)
    token = await ctx.forge.grading.mint_ci_token(row.username, password)
    row.ci_token = credentials.encrypt_text(token, ctx.settings)
    row.last_kept_alive_at = ctx.now
    row.keepalive_error = None
    await ctx.db.flush()


async def identity(ctx: Context, org: OrgName) -> AsOrgAccount:
    """The account as a call through the port is made under it. `NotFound`
    until the org's provisioning has minted both credentials.
    """
    row = await _row(ctx, org)
    return _identity(row, ctx)


async def event_secret_of(ctx: Context, org: OrgName) -> bytes:
    """The secret the org's event push is signed with. `NotFound` when the
    org has no account row.
    """
    row = await _row(ctx, org)
    return credentials.decrypt_text(row.event_secret, ctx.settings).encode()


async def org_names(ctx: Context) -> tuple[OrgName, ...]:
    """Every org the platform made, the ones with an account row, by name."""
    found = await ctx.db.execute(select(OrgAccount.org_name).order_by(OrgAccount.org_name))
    return tuple(OrgName(name) for name in found.scalars())


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


async def keepalive(ctx: Context) -> int:
    """One call to the CI as each account under its CI credential, which is
    what keeps the account's copy at the CI fresh; an account the CI no
    longer answers is signed in again with a fresh password. Each row
    records when it was last kept alive, or what went wrong. Returns how
    many are alive.
    """
    alive = 0
    for row in await _rows(ctx):
        if not row.ci_token or row.forge_user_id is None:
            continue
        try:
            if not await ctx.forge.grading.ci_user_is_alive(_identity(row, ctx)):
                await _sign_in_again(ctx, row)
                log.warning("org_accounts.signed_in_again", org=row.org_name)
        except (PortError, CannotDecrypt) as exc:
            row.keepalive_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "org_accounts.keepalive_failed",
                org=row.org_name,
                error=type(exc).__name__,
                detail=str(exc),
            )
            continue
        row.last_kept_alive_at = ctx.now
        row.keepalive_error = None
        alive += 1
    await ctx.db.flush()
    return alive


async def restore_membership(ctx: Context) -> int:
    """Put back every account that is missing from its place in its org,
    which is what lets the org's tasks be activated at the CI at all.
    Returns how many had to be put back.
    """
    restored = 0
    for row in await _rows(ctx):
        if row.forge_user_id is None:
            continue
        try:
            if await ctx.forge.orgs.ensure_account_membership(
                OrgName(row.org_name), row.forge_user_id
            ):
                restored += 1
                log.warning("org_accounts.membership_restored", org=row.org_name)
        except PortError as exc:
            log.warning(
                "org_accounts.membership_check_failed",
                org=row.org_name,
                error=type(exc).__name__,
                detail=str(exc),
            )
    return restored


async def _sign_in_again(ctx: Context, row: OrgAccount) -> None:
    assert row.forge_user_id is not None
    password = new_password()
    await ctx.forge.identity.set_password(row.forge_user_id, password)
    token = await ctx.forge.grading.mint_ci_token(row.username, password)
    row.ci_token = credentials.encrypt_text(token, ctx.settings)


def _identity(row: OrgAccount, ctx: Context) -> AsOrgAccount:
    if not row.forge_token or not row.ci_token:
        raise NotFound(f"the org account of {row.org_name} is not ready")
    return AsOrgAccount(
        row.org_name,
        forge_token=credentials.decrypt_text(row.forge_token, ctx.settings),
        ci_token=credentials.decrypt_text(row.ci_token, ctx.settings),
    )


async def _row(ctx: Context, org: OrgName) -> OrgAccount:
    row = await _find(ctx, org)
    if row is None:
        raise NotFound(f"the org {org} has no account")
    return row


async def _find(ctx: Context, org: OrgName) -> OrgAccount | None:
    return (
        await ctx.db.execute(select(OrgAccount).where(OrgAccount.org_name == org))
    ).scalar_one_or_none()


async def _rows(ctx: Context) -> Sequence[OrgAccount]:
    return (await ctx.db.execute(select(OrgAccount).order_by(OrgAccount.org_name))).scalars().all()
