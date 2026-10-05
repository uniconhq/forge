"""Invites: an organiser asking one person, by username or by email address,
to take a contestant's place in a contest or an organiser's role at an org,
a contest or a task (`forge.domain.invites` holds the rules). An invite is a
row in `invites`, the package's own table, because it may name an address
with no account behind it, which the forge has nothing to hold.

Making one needs what granting it would: the manager role at the scope, and
admin to invite an admin; a contestant's place is offered at a contest, by
its managers. A username is looked up at the forge then and there, so the
invite is that person's from the start; an address is matched later. The
same pending invite twice is refused (`already_invited`), since the first
can be sent again, and an org makes at most `ORG_DAILY_MAX` invites in a
day (`invite_limit`), so an org nobody vetted cannot turn the platform's
mail server on strangers. Making, sending again, withdrawing, accepting
and declining take the org's lock on role changes, the one `roles` takes,
so two organisers cannot both make the same invite and an acceptance never
passes a grant or a removal.

The invite's mail goes out once the request that made it has committed, and
the request answers without waiting for it (`Context.in_background`): a mail
server that is slow or down never fails an organiser's click. The invite is
the record, and its `mail_status` says what became of the mail. One that
fails, for any reason, is `failed`; one a restart cut short stays
`waiting`. Either way an organiser sends it again, which makes a new token,
since the old one was never kept, so the earlier link stops working, and
gives the invite its whole lifetime again from then, a lapsed one included;
once a mail has gone, the next waits `SEND_AGAIN_AFTER`. At most
`MAILS_AT_ONCE` mails are built and sent at a time in a process, and none
holds a database connection while it talks to the forge or the mail
server. A deployment with no mail server mails nothing (`off`), and the
person finds the invite in Unicon once signed in, as everyone can.

The person acts on their own invites only. An invite to an address becomes
theirs when they list their invites, or open the link the mail carried, and
the address is one the forge has confirmed belongs to them; matching then
rather than at sign-in covers an address confirmed after the first sign-in
and costs every sign-in nothing. Opening a link that is not theirs is no
such invite. An address counts only as far as the forge confirms it is
theirs, so with Forgejo an email invite needs `REGISTER_EMAIL_CONFIRM` on
wherever people sign themselves up, as an email pattern does. Accepting
grants what the invite carries, as long as whoever sent it may still grant
it: an organiser role, which never lowers a role the person holds, or a
contestant's place, which is eligibility to register for an invite-only or
hidden contest while they have no registration there (`contestants`), and
which organisers can withdraw again until they register. Declining grants
nothing. Both close the invite, and a lapsed invite can do neither; the
person's list leaves lapsed ones out.
"""

import builtins
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update

from forge.db.tables import Contestant
from forge.db.tables import Invite as InviteRow
from forge.domain import invites as rules
from forge.domain.errors import (
    AlreadyInvited,
    ContestantConflict,
    Forbidden,
    InvalidInvite,
    InviteLimit,
    NotFound,
    PortError,
    Unavailable,
)
from forge.domain.identity import User
from forge.domain.invites import Grant, InviteStatus, MailStatus
from forge.domain.names import ScopeNames
from forge.domain.registration import REGISTERED
from forge.domain.roles import Role, Scope, ScopeKind, contest_id_of, holds
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.port.mail import Mail
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, roles
from forge.services.access import Organiser, require
from forge.services.turns import Turns

log = get_logger(__name__)

NO_SUCH_INVITE = "There is no such invite."
LISTED_MAX = 500
"""The most invites one scope's list shows, newest first."""
ORG_DAILY_MAX = 1000
"""The most invites an org makes in a day, at all its scopes together: a
course inviting its whole class fits, and an org made to send mail at
strangers runs out."""
SEND_AGAIN_AFTER = timedelta(minutes=10)
"""How long after a mail went out the same invite may be mailed again."""
MAILS_AT_ONCE = 4
_mailing = Turns(MAILS_AT_ONCE)


@dataclass(frozen=True, slots=True)
class Invite:
    """An invite as organisers and the person it is for see it: where it is
    to, by its keys and its names, what it grants, who it names, who sent
    it, where it stands, when it lapses, and what became of its mail.
    `expired` is a pending invite past its expiry.
    """

    id: uuid.UUID
    scope: Scope
    where: ScopeNames
    grants: Grant
    username: str | None
    email: str | None
    invited_by: User | None
    status: InviteStatus
    expired: bool
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    mail_status: MailStatus
    mailed_at: datetime | None


@action
async def create(
    ctx: Context,
    organiser: Organiser,
    scope: Scope,
    grants: Grant,
    *,
    username: str | None = None,
    email: str | None = None,
    lifetime: timedelta | None = None,
) -> Invite:
    """Invite the person a username or an email address names to what
    `grants` gives at the scope, and mail them once it commits. Needs the
    manager role at the scope, and admin to invite an admin.
    """
    _require_to_grant(organiser, scope, grants)
    named, address = rules.checked_target(username, email)
    standing = rules.checked_lifetime(lifetime)
    await roles.one_change_at_a_time(ctx, scope.org)
    await roles.require_scope(ctx, scope)
    user = await roles.person_named(ctx, named) if named is not None else None
    if user is not None:
        await roles.refuse_service_account(ctx, user.id)
        await _refuse_held(ctx, user, scope, grants)
    await _refuse_twice(ctx, scope, grants, user, address)
    await _refuse_too_many(ctx, scope.org)
    token = rules.new_token()
    row = InviteRow(
        scope=scope.path,
        grants=grants.value,
        username=user.username if user is not None else None,
        email=address,
        user_id=user.id if user is not None else None,
        invited_by_user_id=organiser.user.id,
        token_hash=rules.token_hash(token),
        status=InviteStatus.PENDING,
        created_at=ctx.now,
        expires_at=ctx.now + standing,
        mail_status=MailStatus.WAITING if ctx.forge.mail.configured else MailStatus.OFF,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    await ctx.db.refresh(row)
    _mail_later(ctx, row, token)
    log.info(
        "invites.created",
        invite=str(row.id),
        scope=scope.path,
        grants=grants.value,
        by_user_id=organiser.user.id,
        to_user_id=row.user_id,
    )
    return await _view(ctx, row)


@action
async def at(ctx: Context, organiser: Organiser, scope: Scope) -> tuple[Invite, ...]:
    """The invites made at the scope, newest first, whatever became of
    them. Needs the observer role at the scope.
    """
    require(organiser, scope, Role.OBSERVER)
    rows = (
        await ctx.db.execute(
            select(InviteRow)
            .where(InviteRow.scope == scope.path)
            .order_by(InviteRow.created_at.desc(), InviteRow.id)
            .limit(LISTED_MAX)
        )
    ).scalars()
    return await _views(ctx, builtins.list(rows))


@action
async def send_again(ctx: Context, organiser: Organiser, scope: Scope, invite: uuid.UUID) -> Invite:
    """Mail a pending invite again, lapsed or not, with a new token in place
    of the old, whose link stops working, and its whole lifetime again from
    now. Needs what making it needed, and `SEND_AGAIN_AFTER` since the last
    mail went out.
    """
    row = await _organisers(ctx, organiser, scope, invite)
    if row.status != InviteStatus.PENDING:
        rules.refuse_closed(InviteStatus(row.status), row.expires_at, ctx.now, "sent again")
    if not ctx.forge.mail.configured:
        raise InvalidInvite(
            "This deployment sends no mail; the person finds the invite in Unicon once signed in."
        )
    if row.mailed_at is not None and ctx.now < row.mailed_at + SEND_AGAIN_AFTER:
        retry_at = row.mailed_at + SEND_AGAIN_AFTER
        raise InviteLimit(
            f"This invite was mailed minutes ago; send it again after {retry_at:%H:%M} UTC.",
            retry_at=retry_at.isoformat(),
        )
    token = rules.new_token()
    row.expires_at = ctx.now + (row.expires_at - row.created_at)
    row.created_at = ctx.now
    row.token_hash = rules.token_hash(token)
    row.mail_status = MailStatus.WAITING
    row.mailed_at = None
    await ctx.db.flush()
    _mail_later(ctx, row, token)
    log.info("invites.sent_again", invite=str(row.id), by_user_id=organiser.user.id)
    return await _view(ctx, row)


@action
async def withdraw(ctx: Context, organiser: Organiser, scope: Scope, invite: uuid.UUID) -> Invite:
    """Take back a pending invite, lapsed or not, or an accepted invite to a
    contestant's place, which is eligibility until its person registers.
    Needs what making it needed.
    """
    row = await _organisers(ctx, organiser, scope, invite)
    accepted_place = row.status == InviteStatus.ACCEPTED and row.grants == Grant.CONTESTANT
    if row.status != InviteStatus.PENDING and not accepted_place:
        rules.refuse_closed(InviteStatus(row.status), row.expires_at, ctx.now, "withdrawn")
    row.status = InviteStatus.WITHDRAWN
    row.decided_at = ctx.now
    await ctx.db.flush()
    log.info("invites.withdrawn", invite=str(row.id), by_user_id=organiser.user.id)
    return await _view(ctx, row)


@action
async def mine(ctx: Context, session: Session) -> tuple[Invite, ...]:
    """The signed-in person's pending invites that have not lapsed, newest
    first, once every pending invite to an address the forge has confirmed is
    theirs is made theirs.
    """
    await _attach(ctx, session.user_id)
    rows = (
        await ctx.db.execute(
            select(InviteRow)
            .where(
                InviteRow.user_id == session.user_id,
                InviteRow.status == InviteStatus.PENDING,
                InviteRow.expires_at > ctx.now,
            )
            .order_by(InviteRow.created_at.desc(), InviteRow.id)
        )
    ).scalars()
    return await _views(ctx, builtins.list(rows))


@action
async def by_token(ctx: Context, session: Session, token: str) -> Invite:
    """The invite a mail's link carries, when it is the signed-in person's:
    named by their username, or to an address the forge has confirmed is
    theirs, which makes it theirs. `NotFound` for anyone else's.
    """
    row = (
        await ctx.db.execute(
            select(InviteRow).where(InviteRow.token_hash == rules.token_hash(token))
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(NO_SUCH_INVITE)
    if row.user_id is None and row.status == InviteStatus.PENDING:
        if not await _attach(ctx, session.user_id):
            raise Unavailable("The forge did not answer; try the link again in a moment.")
        await ctx.db.refresh(row)
    if row.user_id != session.user_id:
        raise NotFound(NO_SUCH_INVITE)
    return await _view(ctx, row)


@action
async def accept(ctx: Context, session: Session, invite: uuid.UUID) -> Invite:
    """Accept one of the signed-in person's own invites and take what it
    grants. `NotFound` for one that is not theirs.
    """
    row = await _persons(ctx, session, invite, "accepted")
    grants = Grant(row.grants)
    role = grants.role
    if role is not None:
        user = User(id=session.user_id, username=session.username)
        await roles.grant_invited(
            ctx, row.invited_by_user_id, _scope(row.scope), user, role, invite=str(row.id)
        )
    else:
        await roles.refuse_unless_may_grant(
            ctx, row.invited_by_user_id, _scope(row.scope), Role.MANAGER
        )
    row.status = InviteStatus.ACCEPTED
    row.decided_at = ctx.now
    await ctx.db.flush()
    log.info("invites.accepted", invite=str(row.id), user_id=session.user_id)
    return await _view(ctx, row)


@action
async def decline(ctx: Context, session: Session, invite: uuid.UUID) -> Invite:
    """Decline one of the signed-in person's own invites; nothing is
    granted. `NotFound` for one that is not theirs.
    """
    row = await _persons(ctx, session, invite, "declined")
    row.status = InviteStatus.DECLINED
    row.decided_at = ctx.now
    await ctx.db.flush()
    log.info("invites.declined", invite=str(row.id), user_id=session.user_id)
    return await _view(ctx, row)


async def accepted_place(ctx: Context, contest: str, user_id: int) -> bool:
    """Whether the person has accepted an invite to a contestant's place in
    the contest, which is the eligibility an invite-only contest asks for,
    and what shows them a hidden one while they have no registration there.
    """
    found = await ctx.db.scalar(
        select(InviteRow.id)
        .where(
            InviteRow.user_id == user_id,
            InviteRow.status == InviteStatus.ACCEPTED,
            InviteRow.grants == Grant.CONTESTANT,
            InviteRow.scope == contest,
        )
        .limit(1)
    )
    return found is not None


async def accepted_places(ctx: Context, user_id: int) -> frozenset[str]:
    """Every contest the person has accepted an invite to take part in."""
    found = await ctx.db.execute(
        select(InviteRow.scope).where(
            InviteRow.user_id == user_id,
            InviteRow.status == InviteStatus.ACCEPTED,
            InviteRow.grants == Grant.CONTESTANT,
        )
    )
    return frozenset(found.scalars())


def _require_to_grant(organiser: Organiser, scope: Scope, grants: Grant) -> None:
    """What making or changing an invite needs: manager at the scope, admin
    for an admin, and a contestant's place only at a contest.
    """
    require(organiser, scope, Role.MANAGER)
    if grants is Grant.ADMIN and not holds(organiser.grants, scope, Role.ADMIN):
        raise Forbidden(f"Only an admin of {scope.name} may invite an admin there.")
    if grants is Grant.CONTESTANT and scope.kind is not ScopeKind.CONTEST:
        raise InvalidInvite("A contestant's place is offered at a contest.")


async def _refuse_held(ctx: Context, user: User, scope: Scope, grants: Grant) -> None:
    """Refuse inviting someone to what they hold already, or to what they
    could not take: the role there or above, or a role in a contest they are
    registered for; a registration for the contest, or a role at it.
    """
    role = grants.role
    if role is not None:
        if holds(await ctx.forge.orgs.roles_of_user(user.id), scope, role):
            raise InvalidInvite(f"{user.username} holds that role at {scope.name} already.")
        try:
            await roles.refuse_if_contestant(ctx, user, scope)
        except ContestantConflict as exc:
            raise InvalidInvite(exc.detail) from None
        return
    contest = contest_id_of(scope)
    registered = await ctx.db.scalar(
        select(Contestant.id).where(
            Contestant.contest_id == contest,
            Contestant.user_id == user.id,
            Contestant.status.in_(REGISTERED),
        )
    )
    if registered is not None:
        raise InvalidInvite(f"{user.username} is registered for this contest already.")
    if await roles.holds_role_in_contest(ctx, user.id, contest):
        raise InvalidInvite(f"{user.username} holds a role at this contest, so cannot enter it.")


async def _refuse_twice(
    ctx: Context, scope: Scope, grants: Grant, user: User | None, address: str | None
) -> None:
    target = InviteRow.user_id == user.id if user is not None else InviteRow.email == address
    found = await ctx.db.scalar(
        select(InviteRow.id)
        .where(
            InviteRow.scope == scope.path,
            InviteRow.grants == grants,
            InviteRow.status == InviteStatus.PENDING,
            InviteRow.expires_at > ctx.now,
            target,
        )
        .limit(1)
    )
    if found is not None:
        raise AlreadyInvited(
            "That person holds this invite already; send it again instead.", invite=str(found)
        )


async def _refuse_too_many(ctx: Context, org: str) -> None:
    """`InviteLimit` once the org has made `ORG_DAILY_MAX` invites in the last
    day, at all its scopes together.
    """
    since = ctx.now - timedelta(days=1)
    made = await ctx.db.scalar(
        select(func.count())
        .select_from(InviteRow)
        .where(
            or_(InviteRow.scope == org, InviteRow.scope.startswith(f"{org}/", autoescape=True)),
            InviteRow.created_at > since,
        )
    )
    if (made or 0) >= ORG_DAILY_MAX:
        retry_at = ctx.now + timedelta(hours=1)
        raise InviteLimit(
            f"This org has made {ORG_DAILY_MAX} invites in the last day; make more later.",
            retry_at=retry_at.isoformat(),
        )


async def _organisers(
    ctx: Context, organiser: Organiser, scope: Scope, invite: uuid.UUID
) -> InviteRow:
    """One invite at the scope, held until the unit of work ends, once the
    organiser may change it. `NotFound` for one made elsewhere.
    """
    require(organiser, scope, Role.MANAGER)
    await roles.one_change_at_a_time(ctx, scope.org)
    row = (
        await ctx.db.execute(
            select(InviteRow)
            .where(InviteRow.id == invite, InviteRow.scope == scope.path)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(NO_SUCH_INVITE)
    _require_to_grant(organiser, scope, Grant(row.grants))
    return row


async def _persons(ctx: Context, session: Session, invite: uuid.UUID, doing: str) -> InviteRow:
    """One of the person's own invites, held until the unit of work ends,
    once it is pending and has not lapsed.
    """
    found = await ctx.db.scalar(select(InviteRow.scope).where(InviteRow.id == invite))
    if found is not None:
        await roles.one_change_at_a_time(ctx, _scope(found).org)
    row = (
        await ctx.db.execute(
            select(InviteRow)
            .where(InviteRow.id == invite, InviteRow.user_id == session.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(NO_SUCH_INVITE)
    rules.refuse_closed(InviteStatus(row.status), row.expires_at, ctx.now, doing)
    return row


async def _attach(ctx: Context, user_id: int) -> bool:
    """Make the person's every pending invite to an address the forge has
    confirmed is theirs. False when the forge did not answer, which attaches
    nothing this time; the next look tries again.
    """
    try:
        emails = await ctx.forge.identity.verified_emails(user_id)
    except PortError as exc:
        log.warning("invites.emails_unread", user_id=user_id, error=type(exc).__name__)
        return False
    addresses = sorted({email.strip().lower() for email in emails if email.strip()})
    if not addresses:
        return True
    attached = await ctx.db.execute(
        update(InviteRow)
        .where(
            InviteRow.user_id.is_(None),
            InviteRow.status == InviteStatus.PENDING,
            or_(*(InviteRow.email == address for address in addresses)),
        )
        .values(user_id=user_id)
        .returning(InviteRow.id)
    )
    for invite in attached.scalars():
        log.info("invites.matched", invite=str(invite), user_id=user_id)
    return True


async def forget_person(ctx: Context, user_id: int) -> None:
    """Withdraw every pending invite to someone whose account is going, so
    an invite to their address waits for whoever confirms it next.
    """
    await ctx.db.execute(
        update(InviteRow)
        .where(InviteRow.user_id == user_id, InviteRow.status == InviteStatus.PENDING)
        .values(status=InviteStatus.WITHDRAWN, decided_at=ctx.now)
    )


def _mail_later(ctx: Context, row: InviteRow, token: str) -> None:
    """Once this unit of work commits, mail the invite with its token, and
    record what became of the mail.
    """
    if row.mail_status != MailStatus.WAITING:
        return
    invite, hashed = row.id, row.token_hash

    async def work(later: Context) -> None:
        await _send(later, invite, hashed, token)

    ctx.in_background(work)


async def _send(ctx: Context, invite: uuid.UUID, hashed: bytes, token: str) -> None:
    """Mail the invite, if it is still pending under this token and waiting,
    in one of the process's `MAILS_AT_ONCE` turns, and record what came of
    it. What is needed is read first and the connection let go of before the
    forge or the mail server is asked anything; a mail that cannot be built
    or sent, for whatever reason, is `failed` and never left `waiting`.
    """
    async with _mailing():
        row = await ctx.db.get(InviteRow, invite)
        if (
            row is None
            or row.token_hash != hashed
            or row.status != InviteStatus.PENDING
            or row.mail_status != MailStatus.WAITING
        ):
            return
        draft = _Draft(
            invite=row.id,
            scope=_scope(row.scope),
            grants=Grant(row.grants),
            email=row.email,
            user_id=row.user_id,
            invited_by=row.invited_by_user_id,
            expires_at=row.expires_at,
        )
        await ctx.db.commit()
        status = MailStatus.FAILED
        try:
            mail = await _mail(ctx, draft, token)
            await ctx.db.commit()
            if mail is not None:
                await ctx.forge.mail.send(mail)
                status = MailStatus.SENT
        except Exception as exc:
            log.warning("invites.mail_failed", invite=str(invite), error=type(exc).__name__)
            await ctx.db.rollback()
        await ctx.db.execute(
            update(InviteRow)
            .where(InviteRow.id == invite, InviteRow.token_hash == hashed)
            .values(mail_status=status, mailed_at=ctx.now if status is MailStatus.SENT else None)
        )
        log.info("invites.mailed", invite=str(invite), status=status.value)


@dataclass(frozen=True, slots=True)
class _Draft:
    """What an invite's mail is written from, read before the forge is asked."""

    invite: uuid.UUID
    scope: Scope
    grants: Grant
    email: str | None
    user_id: int | None
    invited_by: int
    expires_at: datetime


async def _mail(ctx: Context, draft: _Draft, token: str) -> Mail | None:
    """The invite's mail, or none when there is no address to send it to: a
    username whose account has no confirmed address.
    """
    address = draft.email
    if address is None:
        assert draft.user_id is not None
        emails = await ctx.forge.identity.verified_emails(draft.user_id)
        if not emails:
            log.info("invites.no_address", invite=str(draft.invite))
            return None
        address = emails[0]
    where = (await names.scope_names(ctx, draft.scope)).path
    inviter = await _user(ctx, draft.invited_by)
    by = inviter.username if inviter is not None else "An organiser"
    what = _offer(draft.grants, where)
    link = f"{str(ctx.settings.public_url).rstrip('/')}/invites#{token}"
    text = (
        f"{by} has invited you to {what} on Unicon.\n\n"
        f"Open this link to accept or decline it:\n{link}\n\n"
        f"Sign in first, or make an account with this address if you have none; "
        f"the invite waits for you in Unicon either way.\n"
        f"It lapses on {draft.expires_at:%d %B %Y at %H:%M} UTC.\n"
    )
    return Mail(to=address, subject=f"You are invited to {what}", text=text)


def _offer(grants: Grant, where: str) -> str:
    if grants is Grant.CONTESTANT:
        return f"take part in {where}"
    article = "an" if grants is Grant.ADMIN or grants is Grant.OBSERVER else "a"
    return f"be {article} {grants.value} of {where}"


def _scope(path: str) -> Scope:
    return Scope(*path.split("/"))


async def _user(ctx: Context, user_id: int) -> User | None:
    """Who someone is at the forge, or none when their account is gone or
    the forge did not answer, since a name shown is never worth a refusal.
    """
    try:
        return await ctx.forge.identity.find_user(user_id)
    except PortError:
        return None


async def _view(ctx: Context, row: InviteRow) -> Invite:
    return (await _views(ctx, [row]))[0]


async def _views(ctx: Context, rows: builtins.list[InviteRow]) -> tuple[Invite, ...]:
    scopes = {row.scope: _scope(row.scope) for row in rows}
    named = await names.scopes_named(ctx, scopes.values())
    inviters: dict[int, User | None] = {}
    for row in rows:
        if row.invited_by_user_id not in inviters:
            inviters[row.invited_by_user_id] = await _user(ctx, row.invited_by_user_id)
    return tuple(
        Invite(
            id=row.id,
            scope=scopes[row.scope],
            where=named.get(scopes[row.scope]) or ScopeNames(org=scopes[row.scope].org),
            grants=Grant(row.grants),
            username=row.username,
            email=row.email,
            invited_by=inviters[row.invited_by_user_id],
            status=InviteStatus(row.status),
            expired=row.status == InviteStatus.PENDING and ctx.now >= row.expires_at,
            created_at=row.created_at,
            expires_at=row.expires_at,
            decided_at=row.decided_at,
            mail_status=MailStatus(row.mail_status),
            mailed_at=row.mailed_at,
        )
        for row in rows
    )
