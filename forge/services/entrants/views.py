import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import select

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import EntrantMember, Grading
from forge.db.tables import Invite as InviteRow
from forge.domain import entrants as rules
from forge.domain import registration
from forge.domain.definitions import ContestDefinition
from forge.domain.entrants import EntrantRef, MemberStatus
from forge.domain.errors import (
    AlreadyRegistered,
    IsStaff,
    PortError,
    RegistrationRefused,
    Unavailable,
)
from forge.domain.identity import User
from forge.domain.ids import ContestId, WorkspaceId
from forge.domain.invites import Grant, InviteStatus
from forge.domain.registration import Status, WayKind
from forge.domain.sessions import Session
from forge.runtime.context import Context
from forge.services import release
from forge.services.entrants.guards import (
    _emails_or_none,
    _refuse_fixed,
    _refuse_full,
    _refuse_window,
    _teams_fixed,
)
from forge.services.entrants.queries import display_names, workspace_of
from forge.services.entrants.records import (
    ADDRESSES_UNREAD,
    Entrant,
    Invitation,
    Invited,
    Member,
    Mine,
    Refusal,
)

# Reading, for the services that work in an entrant's workspace.


async def panel(
    ctx: Context,
    session: Session,
    contest: ContestId,
    settings: ContestDefinition,
    person: release.Reader,
) -> Mine:
    """The person's registration panel, as `mine` answers it, from what the
    caller read of the contest and the person.
    """
    rules_now = settings.registration
    row = person.entrant
    shown = await _view(ctx, row, settings) if row is not None else None
    invitations = await _invitations(ctx, contest, session.user_id)
    window = (
        registration.window_open(rules_now, ctx.now)
        and ctx.now < settings.end
        and not _teams_fixed(settings, ctx.now)
    )
    asks = False
    refused: Refusal | None = None
    if row is not None:
        refused = Refusal(AlreadyRegistered.code, "You are in an entrant of this contest already.")
    else:
        invited = person.invited
        emails = await _emails_or_none(ctx, session.user_id, settings)
        asks = emails is not None and registration.asks_code(
            rules_now, emails=emails, invited=invited
        )
        try:
            _refuse_window(settings, ctx.now)
            _refuse_fixed(settings, ctx.now)
            if person.organises:
                raise IsStaff("You have a role at this contest, so you cannot also enter it.")
            if emails is None:
                refused = Refusal(Unavailable.code, ADDRESSES_UNREAD)
            elif not asks:
                registration.qualify(rules_now, emails=emails, code=None, invited=invited)
            if refused is None:
                await _refuse_full(ctx, contest, settings)
        except RegistrationRefused as refusal:
            refused = Refusal(refusal.code, refusal.detail)
    return Mine(
        entrant=shown,
        invitations=invitations,
        registration_open=window,
        can_register=refused is None,
        asks_code=asks and refused is None,
        refused=refused,
        most=settings.entrants.most,
        fewest=settings.entrants.fewest,
        locks=settings.entrants.locks,
    )


async def _invitations(ctx: Context, contest: ContestId, user_id: int) -> tuple[Invitation, ...]:
    """The person's own invites into the contest's entrants, still waiting,
    newest first.
    """
    found = (
        await ctx.db.execute(
            select(InviteRow, EntrantRow)
            .join(EntrantRow, EntrantRow.id == InviteRow.entrant_id)
            .where(
                InviteRow.scope == contest,
                InviteRow.user_id == user_id,
                InviteRow.status == InviteStatus.PENDING,
                InviteRow.expires_at > ctx.now,
            )
            .order_by(InviteRow.created_at.desc(), InviteRow.id)
        )
    ).all()
    named = await display_names(ctx, [row for _, row in found])
    return tuple(
        Invitation(
            invite=invite.id,
            entrant=EntrantRef(row.id, named[row.id]),
            by_organiser=invite.grants == Grant.CONTESTANT,
            expires_at=invite.expires_at,
        )
        for invite, row in found
    )


async def _view(ctx: Context, row: EntrantRow, settings: ContestDefinition) -> Entrant:
    return (await _views(ctx, [row], settings))[0]


async def _views(
    ctx: Context, rows: Sequence[EntrantRow], settings: ContestDefinition
) -> tuple[Entrant, ...]:
    """The entrants as organisers and their members read them: the members,
    the invites still waiting and what has been submitted read first, and
    the names from the forge once the connection is let go of.
    """
    ids = [row.id for row in rows]
    if not ids:
        return ()
    members: dict[uuid.UUID, list[EntrantMember]] = {}
    for member in await ctx.db.scalars(
        select(EntrantMember)
        .where(EntrantMember.entrant_id.in_(ids), EntrantMember.status == MemberStatus.MEMBER)
        .order_by(EntrantMember.joined_at, EntrantMember.id)
    ):
        members.setdefault(member.entrant_id, []).append(member)
    waiting: dict[uuid.UUID, list[Invited]] = {}
    for found in await ctx.db.scalars(
        select(InviteRow)
        .where(
            InviteRow.entrant_id.in_(ids),
            InviteRow.status == InviteStatus.PENDING,
            InviteRow.expires_at > ctx.now,
        )
        .order_by(InviteRow.created_at, InviteRow.id)
    ):
        assert found.entrant_id is not None
        waiting.setdefault(found.entrant_id, []).append(
            Invited(
                invite=found.id,
                username=found.username,
                email=found.email,
                invited_by=found.invited_by_user_id,
                by_organiser=found.grants == Grant.CONTESTANT,
                expires_at=found.expires_at,
            )
        )
    workspaces = {workspace_of(ctx, row): row.id for row in rows}
    made = {
        workspaces[WorkspaceId(workspace)]
        for workspace in await ctx.db.scalars(
            select(Grading.workspace_id)
            .where(Grading.workspace_id.in_(sorted(workspaces)))
            .distinct()
        )
    }
    named = await display_names(ctx, rows)
    users: dict[int, User | None] = {}
    for listed in members.values():
        for member in listed:
            if member.user_id not in users:
                users[member.user_id] = await _user(ctx, member.user_id)
    return tuple(
        Entrant(
            id=row.id,
            contest=ContestId(row.contest_id),
            name=row.name,
            display_name=named[row.id],
            status=Status(row.status),
            registered_at=row.registered_at,
            decided_at=row.decided_at,
            reason=row.reason,
            members=tuple(
                Member(
                    user_id=member.user_id,
                    user=users[member.user_id],
                    way_kind=WayKind(member.way_kind),
                    way_name=member.way_name,
                    joined_at=member.joined_at,
                )
                for member in members.get(row.id, [])
            ),
            invites=tuple(waiting.get(row.id, [])),
            time_extension=timedelta(seconds=row.time_extension_seconds),
            extension_tasks=tuple(row.extension_tasks) if row.extension_tasks is not None else None,
            submitted=row.id in made,
            complete=rules.complete(settings, len(members.get(row.id, []))),
            locked=rules.locked(settings, ctx.now, submitted=row.id in made),
        )
        for row in rows
    )


async def _user(ctx: Context, user_id: int) -> User | None:
    """Who someone is at the forge, or none when their account is gone or
    the forge did not answer: a name shown never fails a change already made
    at the forge.
    """
    try:
        return await ctx.forge.identity.find_user(user_id)
    except PortError:
        return None
