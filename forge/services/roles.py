"""Who holds a role at a scope, and changing that. An organiser lists the
holders at a scope they observe, and adds and removes holders at a scope
they manage; only an admin of the scope grants admin or takes it away. The
organiser's own roles come from the `Organiser` the host's guard produced;
everyone else's are read from the forge as the platform.

A person holds at most one role directly at a scope, so granting them a
different one moves them: the new role is granted first and the old one
revoked afterwards, and they never pass through holding nothing. That is
how a person is promoted or demoted, and handing a scope over is granting
admin to the new person and then removing or demoting the old one.

Three rules hold on every change, each checked before anything is written,
so a refusal leaves nothing half made:

- A scope always keeps someone who can administer it. Removing or demoting
  its last admin is refused with `SoleAdmin`, counting admins of broader
  scopes, so a contest's only admin may step down while an org admin stands.
  The same holds for a person changing their own role.
- Nobody is an organiser and a contestant of one contest. A role at a
  contest or one of its tasks is refused to someone registered in it, and a
  role at an org to someone registered in any of its contests, with
  `ContestantConflict`. `holds_role_in_contest` is the other direction, for
  registration to ask.
- An org's service account is not a person. It is never listed, and it is
  neither granted a role nor removed from one. It is known by its id in
  `org_accounts`, never by its name.

A role is granted or revoked only at a contest or task that is there, asked
of the forge as the platform, so nobody makes the roles of a contest that
does not exist.

The checks read the forge and the writes follow, so two changes at once
could each pass on what the other is about to undo, such as two admins
stepping down together. Every change to the roles of an org, at any scope
in it, first takes a Postgres advisory lock on the org, held until its unit
of work ends: the changes of one org happen one after another, whatever
process makes them, and each checks what the one before it left.
`one_change_at_a_time` is that lock, which deleting an account takes too.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select, text

from forge.db.tables import Contestant
from forge.domain.errors import ContestantConflict, Forbidden, NotFound, SoleAdmin
from forge.domain.identity import User
from forge.domain.ids import ContestId
from forge.domain.names import ScopeNames
from forge.domain.registration import REGISTERED
from forge.domain.roles import (
    RANK,
    Role,
    RoleGrant,
    Scope,
    ScopeKind,
    contest_id_of,
    contest_id_prefix,
    contest_scope,
    holds,
    place_of,
)
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, org_accounts
from forge.services.access import Organiser, require

log = get_logger(__name__)

ROLE_CHANGES_LOCK = 0x524F4C45
"""The first key of every lock on an org's roles, the second being the
org's id hashed by Postgres."""


@dataclass(frozen=True, slots=True)
class Holder:
    """Someone holding `role` at a scope, held directly at `at`, which is that
    scope or a broader one, named `at_names`. A person holding one role at
    two scopes is shown at the broader.
    """

    user: User
    role: Role
    at: Scope
    at_names: ScopeNames


@action
async def holders(ctx: Context, organiser: Organiser, scope: Scope) -> tuple[Holder, ...]:
    """Everyone holding a role at the scope, directly or from a broader scope,
    each once with the highest role they hold there, highest first. Needs the
    observer role at the scope.
    """
    require(organiser, scope, Role.OBSERVER)
    accounts = await org_accounts.service_account_ids(ctx)
    lineage = scope.lineage()
    named = await names.scopes_named(ctx, lineage)
    found: dict[int, Holder] = {}
    for at in lineage:
        for role in Role:
            for user in await ctx.forge.orgs.holders_of(at, role):
                held = found.get(user.id)
                if user.id not in accounts and (held is None or RANK[role] > RANK[held.role]):
                    found[user.id] = Holder(user=user, role=role, at=at, at_names=named[at])
    return tuple(
        sorted(
            found.values(), key=lambda holder: (-RANK[holder.role], holder.user.username.lower())
        )
    )


@action
async def grant(
    ctx: Context, organiser: Organiser, scope: Scope, username: str, role: Role
) -> None:
    """Give the named user `role` at the scope, moving them from any other
    role they hold directly there. Needs the manager role at the scope, and
    admin to grant admin or to move an admin to a lower role.
    """
    require(organiser, scope, Role.MANAGER)
    if role is Role.ADMIN:
        _require_admin(organiser, scope, "grant admin")
    await one_change_at_a_time(ctx, scope.org)
    await require_scope(ctx, scope)
    user = await person_named(ctx, username)
    await refuse_service_account(ctx, user.id)
    held = _direct(await ctx.forge.orgs.roles_of_user(user.id), scope)
    if held == {role}:
        return
    replaced = held - {role}
    if Role.ADMIN in replaced:
        _require_admin(organiser, scope, "demote an admin")
        await _refuse_if_last_admin(ctx, user.id, scope)
    await _refuse_if_contestant(ctx, user, scope)
    if role not in held:
        await ctx.forge.orgs.grant_role(user.id, scope, role)
        _logged("roles.granted", organiser, user.id, scope, role)
    for old in sorted(replaced, key=RANK.__getitem__):
        await ctx.forge.orgs.revoke_role(user.id, scope, old)
        _logged("roles.revoked", organiser, user.id, scope, old)


@action
async def revoke(ctx: Context, organiser: Organiser, scope: Scope, user_id: int) -> None:
    """Take away every role the user holds directly at the scope. Needs the
    manager role at the scope, and admin to remove an admin.
    """
    require(organiser, scope, Role.MANAGER)
    await one_change_at_a_time(ctx, scope.org)
    await require_scope(ctx, scope)
    await refuse_service_account(ctx, user_id)
    held = _direct(await ctx.forge.orgs.roles_of_user(user_id), scope)
    if Role.ADMIN in held:
        _require_admin(organiser, scope, "remove an admin")
        await _refuse_if_last_admin(ctx, user_id, scope)
    for role in sorted(held, key=RANK.__getitem__):
        await ctx.forge.orgs.revoke_role(user_id, scope, role)
        _logged("roles.revoked", organiser, user_id, scope, role)


async def grant_invited(
    ctx: Context, inviter_id: int, scope: Scope, user: User, role: Role, *, invite: str
) -> None:
    """Give `user` `role` at the scope because they accepted an invite from
    `inviter_id`, by the rules a grant keeps, as long as the inviter may still
    grant it: a manager there, and an admin for admin. Someone who holds the
    role there already, or a higher one, keeps what they hold; an invite never
    lowers a role. Run under the org's lock, which the caller holds.
    """
    await one_change_at_a_time(ctx, scope.org)
    sent_by = await ctx.forge.orgs.roles_of_user(inviter_id)
    least = Role.ADMIN if role is Role.ADMIN else Role.MANAGER
    if not holds(sent_by, scope, least):
        raise Forbidden(
            "Whoever sent this invite may no longer grant that role; ask the organisers again."
        )
    await require_scope(ctx, scope)
    await refuse_service_account(ctx, user.id)
    grants = await ctx.forge.orgs.roles_of_user(user.id)
    if holds(grants, scope, role):
        return
    replaced = _direct(grants, scope) - {role}
    await _refuse_if_contestant(ctx, user, scope)
    await ctx.forge.orgs.grant_role(user.id, scope, role)
    log.info(
        "roles.granted",
        user_id=user.id,
        by_user_id=inviter_id,
        scope=scope.path,
        role=role.value,
        invite=invite,
    )
    for old in sorted(replaced, key=RANK.__getitem__):
        await ctx.forge.orgs.revoke_role(user.id, scope, old)
        log.info(
            "roles.revoked",
            user_id=user.id,
            by_user_id=inviter_id,
            scope=scope.path,
            role=old.value,
        )


async def one_change_at_a_time(ctx: Context, org: str) -> None:
    """Wait for any other change to the org's roles to finish, and hold the
    org's lock until this unit of work ends.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:org))"),
        {"space": ROLE_CHANGES_LOCK, "org": org},
    )


async def holds_role_in_contest(ctx: Context, user_id: int, contest: ContestId) -> bool:
    """Whether the user holds any role at the contest, at one of its tasks or
    at its org, which is what refuses their registering in it.
    """
    scope = contest_scope(contest)
    return any(
        grant.scope.covers(scope) or scope.covers(grant.scope)
        for grant in await ctx.forge.orgs.roles_of_user(user_id)
    )


async def admins_of(ctx: Context, scope: Scope, *, leaving: int | None = None) -> set[int]:
    """Every admin of a scope, counting admins of broader scopes and leaving
    out service accounts. With `leaving`, the admins there would be once that
    user no longer held admin directly at the scope itself.
    """
    accounts = await org_accounts.service_account_ids(ctx)
    admins: set[int] = set()
    for at in scope.lineage():
        admins.update(
            user.id
            for user in await ctx.forge.orgs.holders_of(at, Role.ADMIN)
            if user.id not in accounts and not (at == scope and user.id == leaving)
        )
    return admins


def sole_admin(scopes: Iterable[Scope], detail: str) -> SoleAdmin:
    """The refusal that names each scope a change would leave without an
    admin, as `{"kind", "name"}` members of `extra["scopes"]`.
    """
    return SoleAdmin(
        detail, scopes=[{"kind": scope.kind.value, "name": scope.name} for scope in scopes]
    )


def _require_admin(organiser: Organiser, scope: Scope, doing: str) -> None:
    if not holds(organiser.grants, scope, Role.ADMIN):
        raise Forbidden(f"Only an admin of {scope.name} may {doing} there.")


async def require_scope(ctx: Context, scope: Scope) -> None:
    if scope.kind is not ScopeKind.ORG and not await ctx.forge.content.exists(place_of(scope)):
        raise NotFound(f"There is no {scope.kind.value} {scope.name}.")


async def person_named(ctx: Context, username: str) -> User:
    try:
        return await ctx.forge.identity.find_user_by_username(username)
    except NotFound as exc:
        raise NotFound(f"There is no user named {username!r} at the forge.") from exc


async def refuse_service_account(ctx: Context, user_id: int) -> None:
    if user_id in await org_accounts.service_account_ids(ctx):
        raise Forbidden("That is an org's service account, which holds no role.")


def _direct(grants: tuple[RoleGrant, ...], scope: Scope) -> set[Role]:
    return {grant.role for grant in grants if grant.scope == scope}


async def _refuse_if_last_admin(ctx: Context, user_id: int, scope: Scope) -> None:
    if not await admins_of(ctx, scope, leaving=user_id):
        raise sole_admin([scope], f"Someone else has to be an admin of {scope.name} first.")


async def _refuse_if_contestant(ctx: Context, user: User, scope: Scope) -> None:
    statement = select(Contestant.contest_id).where(
        Contestant.user_id == user.id, Contestant.status.in_(REGISTERED)
    )
    if scope.kind is ScopeKind.ORG:
        statement = statement.where(
            Contestant.contest_id.startswith(contest_id_prefix(scope.org), autoescape=True)
        )
    else:
        statement = statement.where(Contestant.contest_id == contest_id_of(scope))
    contests = sorted((await ctx.db.execute(statement)).scalars())
    if contests:
        where = await names.places_named(ctx, contests)
        named = sorted(where[contest].path if contest in where else contest for contest in contests)
        raise ContestantConflict(
            f"{user.username} is a contestant in {', '.join(named)} and cannot also "
            f"hold a role at {scope.name}.",
            contests=named,
        )


def _logged(event: str, organiser: Organiser, user_id: int, scope: Scope, role: Role) -> None:
    log.info(
        event, user_id=user_id, by_user_id=organiser.user.id, scope=scope.path, role=role.value
    )
