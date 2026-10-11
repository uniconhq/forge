"""Entrants: the one kind of entry a contest has, one or more people who enter
it together (`forge.domain.entrants` and `forge.domain.registration` hold the
rules). An entrant is an `entrants` row, with a row in `entrant_members` for
each time someone was in it; nothing about it is a forge role. Its
registration, the organisers' decision on it, its extension, its workspace
and its row on a board are the entrant's, never a member's.

A contestant's actions each need a session, and are refused once the contest
has ended or is archived, since an entrant stands as it is from then on:

1. `register` makes a new entrant with the signed-in person as its first
   member. In this order, each with its own code: the contest is one they
   see, or no such contest; the window is open (`registration_closed`);
   they hold no role at the contest, its tasks or its org (`is_staff`),
   read under the org's lock on role changes, so a grant and a registration
   of one person never pass each other; they are in no entrant of the
   contest, whatever became of it (`already_registered`); they qualify by
   one of the contest's ways (`invite_required`, `wrong_invite_code`,
   `domain_not_allowed`, or `registration_closed` for a contest with none),
   where invited means holding an organiser's invite they accepted; a place
   is free (`contest_full`), the entrants pending or approved counted under
   an advisory lock on the contest, a lock Postgres holds against a name, so
   the last place goes once; and the name, needed when the contest's
   entrants may be more than one person (`entrant_name_required`), refused
   when they are one each (`entrants_alone`), checked
   (`invalid_entrant_name`) and no other entrant's, ignoring case
   (`entrant_name_taken`).
2. `invite` asks someone into the person's entrant, by username or address,
   as an invite with `grants = member` mailed as an organiser's is, under
   the org's daily limit; refused once the roster has locked
   (`roster_locked`), when the members and the invites waiting reach the
   contest's `entrants.most` (`entrant_full`), or when entrants are one
   person each (`entrants_alone`). Any member withdraws its waiting
   invites (`withdraw_invite`).
3. Accepting an invite (`invites.accept`, which calls `take_place`) puts
   the person in the entrant it names, or, for an organiser's invite to the
   contest, registers them as `register` would, by the contest's invite way,
   without a name when entrants are one person each and named after their
   username otherwise; someone in an entrant already accepts that one
   without anything else changing. Joining is refused when the roster has
   locked or the window is closed, unless an organiser sent the invite;
   when they hold a role (`is_staff`); when the entrant is full
   (`entrant_full`); or when they do not qualify: an organiser's invite
   qualifies them by the invite way (`invite_required` once the contest has
   none), and a member's does not, so they qualify as themselves and may
   need a code. Someone in another entrant of the contest moves out of it
   only while its roster is open, it has no submissions and it has not been
   turned away (`in_entrant`).
4. `leave` takes the person out before the roster locks (`roster_locked`);
   it is also how someone changes entrant.
5. `rename` gives the entrant a new name, by any member, before the roster
   locks.

Who may reach an entrant's workspace changes in the request that changes
the membership, before the membership is written: someone who joins is given
access to every part already made, and someone who leaves, is taken out or
is moved loses it. A call to the forge that fails fails the request, and
asking again finishes it, since giving or taking access twice changes
nothing. A part made later is made with whoever is a member then (`settle`),
so a place to submit is finished only once every member can write it. An
entrant left with nobody and no submissions is deleted, its member rows kept
`left`.

An entrant is approved straight away once it is complete and every member
came in by a way that approves straight away, checked whenever someone
joins; one who joins an approved entrant by a way an organiser approves
sends it back to `pending`, which can happen only before the roster locks,
so nothing it submitted is held back.

Organisers act on the entrant, whatever its roster's lock and the window:
anyone observing the contest lists them (`list`), and a manager decides
them: `approve`, refused while the entrant is short of `entrants.fewest`
(`entrant_incomplete`); `reject` with a reason its members read; `reopen` a
rejection, refused like a registration when a member holds a role by now or
every place is taken; `remove`, which takes every member's access away and
keeps what the workspace holds; `extend`, which every deadline check adds;
`organise_rename`; `remove_member`; `move_member` to another entrant, never
leaving behind an entrant whose results would have nobody
(`entrant_has_submissions`); and `delete` one that has no submissions.

A change takes, in this order, the org's lock on role changes when it reads
roles, the invite it acts on, an advisory lock on each person it moves in
the order of their ids, the contest's places, then the entrant rows it
touches in the order of their ids, so two changes of one person or one
entrant happen one after another and a removal never passes a join; a
submit holds its entrant's row for reading, so no change of who is in it
passes the submit.
"""

import builtins
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Entrant as EntrantRow
from forge.db.tables import EntrantMember, Grading
from forge.db.tables import Invite as InviteRow
from forge.domain import entrants as rules
from forge.domain import registration
from forge.domain import release as release_rules
from forge.domain.definitions import Approval, ContestDefinition, Locks, State
from forge.domain.entrants import DELETED_USER, EntrantRef, MemberStatus
from forge.domain.errors import (
    AlreadyRegistered,
    ContestFull,
    EntrantChanged,
    EntrantHasSubmissions,
    EntrantNameRequired,
    EntrantNameTaken,
    EntrantsAlone,
    InEntrant,
    InviteRequired,
    IsStaff,
    NotFound,
    PortError,
    RegistrationClosed,
    RegistrationRefused,
)
from forge.domain.identity import User
from forge.domain.ids import ContestId, WorkspaceId, new_id
from forge.domain.invites import Grant, InviteStatus
from forge.domain.names import EntrantOwner
from forge.domain.registration import ORGANISER, Qualified, Status, WayKind
from forge.domain.roles import Role, contest_id_of, contest_scope
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import invites, places, published, release, roles, timelines
from forge.services.access import Organiser, require

log = get_logger(__name__)

PLACES_LOCK = 0x504C4143
"""The first key of every lock on a contest's places, the second being the
contest's id hashed by Postgres."""
PEOPLE_LOCK = 0x50504C45
"""The first key of every lock on one person in one contest, the second being
the contest and the user id hashed by Postgres."""
NO_SUCH_ENTRANT = "There is no such entrant in this contest."
NOT_IN_ONE = "You are in no entrant of this contest."
ENDED = "Entrants stand as they are once the contest ends; ask the organisers."


@dataclass(frozen=True, slots=True)
class Member:
    """Someone in an entrant: who they are at the forge, or none once their
    account is gone, the way they came in by, its kind and its name, and
    when they joined.
    """

    user_id: int
    user: User | None
    way_kind: WayKind
    way_name: str | None
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class Invited:
    """An invite into an entrant still waiting: whom it names, whether an
    organiser or a member sent it, who, and when it lapses.
    """

    invite: uuid.UUID
    username: str | None
    email: str | None
    invited_by: int
    by_organiser: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Entrant:
    """An entrant: its id, its own name, if it has one, and the name it is
    shown under; where its registration stands, when it was made and the last
    decision on it with, for a rejection, why; its members, oldest first, and
    the invites into it still waiting; its extension, how long and on which
    tasks, every task when it names none; whether it has submitted; whether
    it has as many members as the contest asks; and whether its roster has
    locked.
    """

    id: uuid.UUID
    contest: ContestId
    name: str | None
    display_name: str
    status: Status
    registered_at: datetime
    decided_at: datetime | None
    reason: str | None
    members: tuple[Member, ...]
    invites: tuple[Invited, ...]
    time_extension: timedelta
    extension_tasks: tuple[str, ...] | None
    submitted: bool
    complete: bool
    locked: bool


@dataclass(frozen=True, slots=True)
class Invitation:
    """One of the person's own invites into an entrant of the contest, still
    waiting: the invite, the entrant, whether an organiser sent it, and when
    it lapses.
    """

    invite: uuid.UUID
    entrant: EntrantRef
    by_organiser: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the person cannot register now: the code `register` would refuse
    with, and its sentence.
    """

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Mine:
    """What the person's registration panel shows: their entrant, if any,
    and their own invites into the contest's entrants; whether the window is
    open; whether they can register, and why not when they cannot; whether
    to ask them for a code, only when a code would let them in; and the
    contest's size rules and when a roster locks.
    """

    entrant: Entrant | None
    invitations: tuple[Invitation, ...]
    registration_open: bool
    can_register: bool
    asks_code: bool
    refused: Refusal | None
    most: int
    fewest: int
    locks: Locks


@dataclass(frozen=True, slots=True)
class Entered:
    """What accepting a place in a contest did: the entrant the person is in
    now, its status, and whether accepting `made` it, `joined` it, or found
    them in it `already`.
    """

    entrant: EntrantRef
    status: Status
    how: str


@dataclass(frozen=True, slots=True)
class Standing:
    """The entrant a person is in: its row, the workspace it owns, every
    member, and when the person joined it, by the database's clock, which
    tells a first upload whether its place was made with them on it.
    """

    entrant: EntrantRow
    owner: EntrantOwner
    members: tuple[int, ...]
    since: datetime


# The contestant's actions.


@action
async def register(
    ctx: Context,
    session: Session,
    contest: ContestId,
    *,
    name: str | None = None,
    code: str | None = None,
) -> Entrant:
    """Register a new entrant with the signed-in person as its first member,
    named `name` and with the contest's `code` when it asks for them.
    `NotFound` for a contest they may not see, and each rule's own refusal
    otherwise.
    """
    settings = await published.contest(ctx, contest)
    invited = await invites.accepted_place(ctx, contest, session.user_id)
    if not release_rules.contest_visible_to(
        settings, has_session=True, is_contestant=invited, is_organiser=False
    ):
        raise NotFound(published.NO_SUCH_CONTEST)
    _refuse_window(settings, ctx.now)
    user = await ctx.forge.identity.find_user(session.user_id)
    emails = await _emails(ctx, user.id, settings)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    await _refuse_staff(ctx, user.id, contest)
    await _hold_people(ctx, contest, [user.id])
    if await membership(ctx, contest, user.id) is not None:
        raise AlreadyRegistered("You are in an entrant of this contest already.")
    qualified = registration.qualify(
        settings.registration, emails=emails, code=code, invited=invited
    )
    await _hold_places(ctx, contest)
    await _refuse_full(ctx, contest, settings)
    named = _new_name(settings, name)
    row = await _make(ctx, contest, named, user.id, qualified, by=user.id)
    log.info("entrants.registered", entrant=str(row.id), contest=contest, user_id=user.id)
    await _after_join(ctx, row, settings, qualified.approval)
    return await _view(ctx, row, settings)


@action
async def mine(ctx: Context, session: Session, contest: ContestId) -> Mine:
    """The signed-in person's registration panel for the contest: their
    entrant, their invites into its entrants, and whether and how they may
    register. `NotFound` for a contest they neither see nor are in.
    """
    settings, person = await _own(ctx, session, contest)
    return await panel(ctx, session, contest, settings, person)


@action
async def invite(
    ctx: Context,
    session: Session,
    contest: ContestId,
    *,
    username: str | None = None,
    email: str | None = None,
) -> invites.Invite:
    """Ask the person a username or an address names into the signed-in
    person's entrant, and mail them once it commits.
    """
    settings, _ = await _own(ctx, session, contest)
    if settings.entrants.most == 1:
        raise EntrantsAlone("Everyone enters this contest alone, so nobody is invited in.")
    target = await invites.target(ctx, username, email)
    row = await _held_own(ctx, contest, session.user_id)
    registration.refuse_undecidable(Status(row.status), registration.REGISTERED, "joined")
    rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, row))
    await invites.refuse_twice(ctx, contest_scope(contest), Grant.MEMBER, target, row.id)
    rules.refuse_full(
        len(await member_ids(ctx, row.id)) + await _waiting(ctx, row.id),
        settings.entrants.most,
    )
    if target.user is not None:
        if target.user.id in await member_ids(ctx, row.id):
            raise InEntrant(f"{target.user.username} is in this entrant already.")
        if await roles.holds_role_in_contest(ctx, target.user.id, contest):
            raise IsStaff(
                f"{target.user.username} holds a role at this contest, so cannot enter it."
            )
    made = await invites.make(
        ctx,
        contest_scope(contest),
        Grant.MEMBER,
        target,
        invited_by=session.user_id,
        entrant=row.id,
    )
    log.info("entrants.invited", entrant=str(row.id), invite=str(made.id), by=session.user_id)
    return made


@action
async def withdraw_invite(
    ctx: Context, session: Session, contest: ContestId, invite: uuid.UUID
) -> invites.Invite:
    """Take back a member's invite into the signed-in person's entrant that
    is still waiting. `NotFound` for any other.
    """
    await _own(ctx, session, contest)
    found = (
        await ctx.db.execute(
            select(InviteRow)
            .where(
                InviteRow.id == invite,
                InviteRow.scope == contest,
                InviteRow.grants == Grant.MEMBER,
                InviteRow.status == InviteStatus.PENDING,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if found is None or found.entrant_id is None:
        raise NotFound(invites.NO_SUCH_INVITE)
    await _hold_entrants(ctx, contest, [found.entrant_id])
    if session.user_id not in await member_ids(ctx, found.entrant_id):
        raise NotFound(invites.NO_SUCH_INVITE)
    found.status = InviteStatus.WITHDRAWN
    found.decided_at = ctx.now
    await ctx.db.flush()
    log.info("entrants.invite_withdrawn", invite=str(invite), by=session.user_id)
    return await invites.view(ctx, found)


@action
async def leave(ctx: Context, session: Session, contest: ContestId) -> None:
    """Leave the signed-in person's entrant before its roster locks. Their
    access to its workspace goes; what it made stays the entrant's.
    """
    settings, _ = await _own(ctx, session, contest)
    await _hold_people(ctx, contest, [session.user_id])
    member = await membership(ctx, contest, session.user_id)
    if member is None:
        raise NotFound(NOT_IN_ONE)
    (row,) = await _hold_entrants(ctx, contest, [member.entrant_id])
    registration.refuse_undecidable(Status(row.status), registration.REGISTERED, "left")
    rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, row))
    await _take_out(ctx, row, member)
    log.info("entrants.left", entrant=str(row.id), user_id=session.user_id)


@action
async def rename(ctx: Context, session: Session, contest: ContestId, name: str) -> Entrant:
    """Give the signed-in person's entrant a new name, before its roster
    locks.
    """
    settings, _ = await _own(ctx, session, contest)
    checked = _new_name(settings, name)
    row = await _held_own(ctx, contest, session.user_id)
    rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, row))
    await _rename(ctx, row, checked)
    log.info("entrants.renamed", entrant=str(row.id), by=session.user_id)
    return await _view(ctx, row, settings)


# The organisers' actions.


@action
async def list(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Entrant, ...]:
    """Every entrant of the contest, the earliest registered first. Needs
    the observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await published.contest(ctx, contest)
    rows = (
        await ctx.db.scalars(
            select(EntrantRow)
            .where(EntrantRow.contest_id == contest)
            .order_by(EntrantRow.registered_at, EntrantRow.id)
        )
    ).all()
    return await _views(ctx, rows, settings)


@action
async def approve(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> Entrant:
    """Approve a pending entrant that has as many members as the contest
    asks. Needs the manager role at the contest.
    """
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.PENDING,), "approved")
    rules.refuse_incomplete(settings, len(await member_ids(ctx, row.id)))
    await _approve(ctx, row, decided_by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def reject(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID, reason: str
) -> Entrant:
    """Reject a pending entrant with a reason its members read. Needs the
    manager role at the contest.
    """
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.PENDING,), "rejected")
    checked = registration.checked_reason(reason)
    _decide(ctx, row, Status.REJECTED, decided_by=organiser.user.id, reason=checked)
    await ctx.db.flush()
    log.info("entrants.rejected", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def reopen(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> Entrant:
    """Take a rejection back: the entrant is pending again, waiting for a
    decision, with the reason gone. It takes a place again, so it is refused
    like a new registration when a member holds a role at the contest by now
    (`is_staff`) or every place is taken (`contest_full`), checked under the
    same locks. Needs the manager role at the contest.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    await roles.one_change_at_a_time(ctx, contest_scope(contest).org)
    for user_id in await member_ids(ctx, entrant):
        if await roles.holds_role_in_contest(ctx, user_id, contest):
            raise IsStaff("A member of this entrant holds a role at this contest now.")
    await _hold_places(ctx, contest)
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.REJECTED,), "reopened")
    await _refuse_full(ctx, contest, settings)
    row.status = Status.PENDING
    row.decided_at = None
    row.decided_by_user_id = None
    row.reason = None
    await ctx.db.flush()
    log.info("entrants.reopened", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def remove(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> Entrant:
    """End an approved entrant: its members can no longer write its
    workspace, and what is in it stays. Needs the manager role at the
    contest.
    """
    settings, row = await _decided(ctx, organiser, contest, entrant, (Status.APPROVED,), "removed")
    _decide(ctx, row, Status.REMOVED, decided_by=organiser.user.id, reason=None)
    await ctx.db.flush()
    await _close(ctx, workspace_of(ctx, row), await member_ids(ctx, row.id))
    log.info("entrants.removed", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def extend(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    entrant: uuid.UUID,
    extension: timedelta,
    tasks: Sequence[str] | None = None,
) -> Entrant:
    """Give the entrant `extension` on the contest's `tasks`, by name, every
    task when none are named, in place of any it had, so an extension of
    nothing takes it away. It moves the entrant's due and close on those
    tasks. Needs the manager role at the contest, and the entrant pending or
    approved. `InvalidExtension` when it names a task the contest does not
    list, would let the entrant submit to a task whose reveal has passed, or
    would leave one of its submissions after the due or the close it was
    made before.
    """
    settings, row = await _decided(
        ctx, organiser, contest, entrant, registration.REGISTERED, "given time"
    )
    checked = registration.checked_extension(extension)
    named = registration.checked_tasks(tasks, [entry.id for entry in settings.tasks])
    await timelines.refuse_extension(
        ctx,
        contest,
        settings,
        workspace_of(ctx, row),
        timelines.of_entrant(row),
        timelines.extension_of(int(checked.total_seconds()), named),
    )
    row.time_extension_seconds = int(checked.total_seconds())
    row.extension_tasks = [*named] if named is not None else None
    await ctx.db.flush()
    log.info(
        "entrants.extended",
        entrant=str(entrant),
        seconds=row.time_extension_seconds,
        by=organiser.user.id,
    )
    return await _view(ctx, row, settings)


@action
async def organise_rename(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID, name: str
) -> Entrant:
    """Give an entrant a new name. Needs the manager role at the contest."""
    settings = await _managed(ctx, organiser, contest)
    checked = _new_name(settings, name)
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    await _rename(ctx, row, checked)
    log.info("entrants.renamed", entrant=str(entrant), by=organiser.user.id)
    return await _view(ctx, row, settings)


@action
async def remove_member(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID, user_id: int
) -> Entrant | None:
    """Take someone out of an entrant, their access to its workspace taken
    away; none when that leaves an entrant with nobody and no submissions,
    which is deleted. Needs the manager role at the contest.
    """
    settings = await _managed(ctx, organiser, contest)
    await _hold_people(ctx, contest, [user_id])
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    member = await membership(ctx, contest, user_id)
    if member is None or member.entrant_id != row.id:
        raise NotFound("That person is not in this entrant.")
    kept = await _take_out(ctx, row, member)
    log.info("entrants.member_removed", entrant=str(entrant), user_id=user_id, by=organiser.user.id)
    return await _view(ctx, row, settings) if kept else None


@action
async def move_member(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    entrant: uuid.UUID,
    user_id: int,
    to: uuid.UUID,
) -> Entrant:
    """Move someone from an entrant to another of the contest, within its
    `entrants.most` (`entrant_full`), and answer with the one they are in
    now. Refused with `entrant_has_submissions` when they are the last member
    of an entrant that has submitted, whose results would have nobody. Needs
    the manager role at the contest.
    """
    settings = await _managed(ctx, organiser, contest)
    await _hold_people(ctx, contest, [user_id])
    held = {row.id: row for row in await _hold_entrants(ctx, contest, [entrant, to])}
    source, target = held[entrant], held[to]
    member = await membership(ctx, contest, user_id)
    if member is None or member.entrant_id != source.id:
        raise NotFound("That person is not in this entrant.")
    if source.id == target.id:
        return await _view(ctx, target, settings)
    others = [other for other in await member_ids(ctx, source.id) if other != user_id]
    if not others and await submitted(ctx, source):
        raise EntrantHasSubmissions(
            "That person is the last member of an entrant that has submitted, so its results "
            "would have nobody; move someone else in first."
        )
    rules.refuse_full(len(await member_ids(ctx, target.id)), settings.entrants.most)
    await _take_out(ctx, source, member)
    await _join(ctx, target, settings, user_id, ORGANISER)
    log.info(
        "entrants.member_moved",
        entrant=str(entrant),
        to=str(to),
        user_id=user_id,
        by=organiser.user.id,
    )
    return await _view(ctx, target, settings)


@action
async def delete(
    ctx: Context, organiser: Organiser, contest: ContestId, entrant: uuid.UUID
) -> None:
    """Delete an entrant that has submitted nothing, taking every member's
    access to its workspace away and withdrawing the invites into it. Needs
    the manager role at the contest.
    """
    await _managed(ctx, organiser, contest)
    await _hold_people(ctx, contest, await member_ids(ctx, entrant))
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    if await submitted(ctx, row):
        raise EntrantHasSubmissions("This entrant has submitted, so it stays with its results.")
    current = await member_ids(ctx, row.id)
    if current:
        await _close(ctx, workspace_of(ctx, row), current)
    await _end(ctx, row)
    log.info("entrants.deleted", entrant=str(entrant), by=organiser.user.id)


# Accepting an invite to a place in a contest.


async def take_place(
    ctx: Context, session: Session, invite: InviteRow, *, code: str | None
) -> Entered:
    """What accepting `invite` does for the signed-in person, an organiser's
    place in the contest or a member's in an entrant, as this module's
    account of accepting says. The caller holds the org's lock on role
    changes and the invite.
    """
    contest = contest_id_of(invites.scope_of(invite.scope))
    settings = await published.contest(ctx, contest)
    by_organiser = invite.grants == Grant.CONTESTANT
    user = await ctx.forge.identity.find_user(session.user_id)
    if invite.entrant_id is None:
        return await _register_invited(ctx, contest, settings, user)
    invited = by_organiser or await invites.accepted_place(ctx, contest, user.id)
    emails = () if by_organiser else await _emails(ctx, user.id, settings)
    await _refuse_staff(ctx, user.id, contest)
    await _hold_people(ctx, contest, [user.id])
    current = await membership(ctx, contest, user.id)
    if current is not None and current.entrant_id == invite.entrant_id:
        raise InEntrant("You are in this entrant already.")
    wanted = [invite.entrant_id, *([current.entrant_id] if current is not None else [])]
    held = {row.id: row for row in await _hold_entrants(ctx, contest, wanted)}
    target = held[invite.entrant_id]
    registration.refuse_undecidable(Status(target.status), registration.REGISTERED, "joined")
    if not by_organiser:
        _refuse_window(settings, ctx.now)
        rules.refuse_locked(settings, ctx.now, submitted=await submitted(ctx, target))
    rules.refuse_full(len(await member_ids(ctx, target.id)), settings.entrants.most)
    if by_organiser:
        qualified = _by_invite_way(settings)
    else:
        qualified = registration.qualify(
            settings.registration, emails=emails, code=code, invited=invited
        )
    if current is not None:
        source = held[current.entrant_id]
        await _refuse_moving(ctx, settings, source)
        await _take_out(ctx, source, current)
    await _join(ctx, target, settings, user.id, qualified)
    log.info("entrants.joined", entrant=str(target.id), user_id=user.id, invite=str(invite.id))
    return Entered(await ref(ctx, target), Status(target.status), "joined")


async def _register_invited(
    ctx: Context, contest: ContestId, settings: ContestDefinition, user: User
) -> Entered:
    """Register the person by the contest's invite way, as an organiser
    asked, or find them in the entrant they are in already.
    """
    await _hold_people(ctx, contest, [user.id])
    current = await membership(ctx, contest, user.id)
    if current is not None:
        row = await ctx.db.get(EntrantRow, current.entrant_id)
        assert row is not None
        return Entered(await ref(ctx, row), Status(row.status), "already")
    qualified = _by_invite_way(settings)
    await _refuse_staff(ctx, user.id, contest)
    await _hold_places(ctx, contest)
    await _refuse_full(ctx, contest, settings)
    name = await _free_name(ctx, contest, user.username) if settings.entrants.most > 1 else None
    row = await _make(ctx, contest, name, user.id, qualified, by=user.id)
    log.info("entrants.registered", entrant=str(row.id), contest=contest, user_id=user.id)
    await _after_join(ctx, row, settings, qualified.approval)
    return Entered(await ref(ctx, row), Status(row.status), "made")


def _by_invite_way(settings: ContestDefinition) -> Qualified:
    """How someone an organiser invited qualifies: by the contest's invite
    way. `InviteRequired` once the contest has none.
    """
    index = settings.registration.invite_way
    if index is None:
        raise InviteRequired("This contest no longer takes people by invite.")
    way = settings.registration.ways[index]
    return Qualified(index, WayKind.INVITE, None, way.approval, {"invited": True})


async def _refuse_moving(ctx: Context, settings: ContestDefinition, source: EntrantRow) -> None:
    """`InEntrant` unless the person may leave the entrant they are in for
    another: its roster is open, it has no submissions, and it has not been
    turned away.
    """
    if (
        Status(source.status) not in registration.REGISTERED
        or await submitted(ctx, source)
        or rules.locked(settings, ctx.now, submitted=False)
    ):
        raise InEntrant("You are in another entrant of this contest that you can no longer leave.")


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
    window = registration.window_open(rules_now, ctx.now) and ctx.now < settings.end
    asks = False
    refused: Refusal | None = None
    if row is not None:
        refused = Refusal(AlreadyRegistered.code, "You are in an entrant of this contest already.")
    else:
        invited = person.invited
        emails = await _emails_or_none(ctx, session.user_id, settings)
        asks = registration.asks_code(rules_now, emails=emails, invited=invited)
        try:
            _refuse_window(settings, ctx.now)
            if person.organises:
                raise IsStaff("Organisers of a contest cannot also enter it.")
            if not asks:
                registration.qualify(rules_now, emails=emails, code=None, invited=invited)
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


async def membership(ctx: Context, contest: ContestId, user_id: int) -> EntrantMember | None:
    """The person's member row of the entrant they are in now, or none."""
    return (
        await ctx.db.execute(
            select(EntrantMember)
            .where(
                EntrantMember.contest_id == contest,
                EntrantMember.user_id == user_id,
                EntrantMember.status == MemberStatus.MEMBER,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def of_person(ctx: Context, contest: ContestId, user_id: int) -> EntrantRow | None:
    """The entrant the person is in now, whatever its status, or none."""
    return (
        await ctx.db.execute(
            select(EntrantRow)
            .join(EntrantMember, EntrantMember.entrant_id == EntrantRow.id)
            .where(
                EntrantMember.contest_id == contest,
                EntrantMember.user_id == user_id,
                EntrantMember.status == MemberStatus.MEMBER,
            )
        )
    ).scalar_one_or_none()


async def standing(ctx: Context, contest: ContestId, user_id: int) -> Standing | None:
    """The entrant the person is in for the contest, with its workspace's
    owner, every member, and when the person joined; none outside one.
    """
    member = await membership(ctx, contest, user_id)
    if member is None:
        return None
    row = await ctx.db.get(EntrantRow, member.entrant_id)
    if row is None:
        return None
    return Standing(
        row, EntrantOwner(row.id), tuple(await member_ids(ctx, row.id)), member.created_at
    )


async def member_ids(ctx: Context, entrant: uuid.UUID) -> builtins.list[int]:
    """The entrant's members now, the earliest joined first."""
    found = await ctx.db.scalars(
        select(EntrantMember.user_id)
        .where(EntrantMember.entrant_id == entrant, EntrantMember.status == MemberStatus.MEMBER)
        .order_by(EntrantMember.joined_at, EntrantMember.created_at, EntrantMember.user_id)
    )
    return builtins.list(found)


async def everyone_in(ctx: Context, entrant: uuid.UUID) -> frozenset[int]:
    """Everyone who is or was a member of the entrant, whose messages on its
    desk are the entrant's.
    """
    found = await ctx.db.scalars(
        select(EntrantMember.user_id).where(EntrantMember.entrant_id == entrant)
    )
    return frozenset(found)


async def entrant_ids(ctx: Context, user_id: int) -> frozenset[str]:
    """The id of every entrant the person is a member of, in any contest."""
    found = await ctx.db.scalars(
        select(EntrantMember.entrant_id).where(
            EntrantMember.user_id == user_id, EntrantMember.status == MemberStatus.MEMBER
        )
    )
    return frozenset(str(entrant) for entrant in found)


async def approved_contests(ctx: Context, user_id: int) -> frozenset[ContestId]:
    """Every contest where the person is in an approved entrant."""
    found = await ctx.db.scalars(
        select(EntrantRow.contest_id)
        .join(EntrantMember, EntrantMember.entrant_id == EntrantRow.id)
        .where(
            EntrantMember.user_id == user_id,
            EntrantMember.status == MemberStatus.MEMBER,
            EntrantRow.status == Status.APPROVED,
        )
    )
    return frozenset(ContestId(contest) for contest in found)


async def statuses(ctx: Context, user_id: int) -> dict[str, Status]:
    """The status of the entrant the person is in, by contest."""
    found = await ctx.db.execute(
        select(EntrantRow.contest_id, EntrantRow.status)
        .join(EntrantMember, EntrantMember.entrant_id == EntrantRow.id)
        .where(EntrantMember.user_id == user_id, EntrantMember.status == MemberStatus.MEMBER)
    )
    return {contest: Status(status) for contest, status in found}


async def workspaces_of(ctx: Context, contest: ContestId) -> builtins.list[WorkspaceId]:
    """The workspace of every entrant of the contest, whatever became of it,
    since a removed entrant's submissions are kept.
    """
    found = await ctx.db.scalars(
        select(EntrantRow.id)
        .where(EntrantRow.contest_id == contest)
        .order_by(EntrantRow.registered_at, EntrantRow.id)
    )
    return [ctx.forge.workspaces.workspace_of(contest, EntrantOwner(one)) for one in found.all()]


def workspace_of(ctx: Context, row: EntrantRow) -> WorkspaceId:
    return ctx.forge.workspaces.workspace_of(ContestId(row.contest_id), EntrantOwner(row.id))


async def submitted(ctx: Context, row: EntrantRow) -> bool:
    """Whether the entrant has made a submission."""
    found = await ctx.db.scalar(
        select(Grading.id).where(Grading.workspace_id == workspace_of(ctx, row)).limit(1)
    )
    return found is not None


async def settle(
    ctx: Context, entrant: uuid.UUID, workspace: WorkspaceId, made_with: Sequence[int]
) -> tuple[int, ...]:
    """After a part of the entrant's workspace was made with `made_with` and
    no lock held, hold the entrant and, when its members have changed
    meanwhile, make exactly the members now the people who reach every part
    of it, so someone who joined is added and someone who left, or was left
    behind by a change that failed halfway, is taken off. The members now,
    none for an entrant that is gone.
    """
    held = (
        await ctx.db.execute(
            select(EntrantRow)
            .where(EntrantRow.id == entrant)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current = tuple(await member_ids(ctx, entrant)) if held is not None else ()
    if set(current) != set(made_with):
        await ctx.forge.workspaces.share_workspace(workspace, current)
        log.info("entrants.access_settled", entrant=str(entrant), members=builtins.list(current))
    return current


async def hold_for_reading(ctx: Context, entrant: uuid.UUID) -> None:
    """Hold the entrant's row for reading until the unit of work ends, so no
    change of who is in it, or of its extension, passes the caller.
    """
    await ctx.db.execute(
        select(EntrantRow.id).where(EntrantRow.id == entrant).with_for_update(read=True)
    )


async def ref(ctx: Context, row: EntrantRow) -> EntrantRef:
    """The entrant by its id and the name it is shown under."""
    return EntrantRef(row.id, (await display_names(ctx, [row]))[row.id])


async def display_names(ctx: Context, rows: Collection[EntrantRow]) -> dict[uuid.UUID, str]:
    """The name each entrant is shown under: its own, or the username of its
    member, the latest to leave when nobody is in it now, read from the forge
    once the connection is let go of; `DELETED_USER` for an account that is
    gone, and nothing when the forge did not say.
    """
    found = {row.id: row.name for row in rows if row.name is not None}
    nameless = [row.id for row in rows if row.name is None]
    whose: dict[uuid.UUID, int] = {}
    if nameless:
        members = await ctx.db.execute(
            select(EntrantMember.entrant_id, EntrantMember.user_id)
            .where(EntrantMember.entrant_id.in_(nameless))
            .order_by(
                (EntrantMember.status == MemberStatus.MEMBER).desc(),
                EntrantMember.left_at.desc().nulls_first(),
                EntrantMember.joined_at,
            )
        )
        for entrant, member in members:
            whose.setdefault(entrant, member)
    await ctx.let_go()
    usernames: dict[int, str] = {}
    for entrant in nameless:
        user_id = whose.get(entrant)
        if user_id is None:
            found[entrant] = ""
            continue
        if user_id not in usernames:
            usernames[user_id] = await username_of(ctx, user_id)
        found[entrant] = usernames[user_id]
    return {entrant: name or "" for entrant, name in found.items()}


async def username_of(ctx: Context, user_id: int) -> str:
    """Someone's username, `DELETED_USER` once their account is gone, and
    nothing when the forge does not answer.
    """
    try:
        return (await ctx.forge.identity.find_user(user_id)).username
    except NotFound:
        return DELETED_USER
    except PortError:
        return ""


# The pieces.


async def _own(
    ctx: Context, session: Session, contest: ContestId
) -> tuple[ContestDefinition, release.Reader]:
    """The contest's settings and the person, once the contest is one they
    see or one they are in an entrant of, whatever its visibility says of
    them now. `NotFound` otherwise.
    """
    settings = await published.contest(ctx, contest)
    person = await release.reader(ctx, session, contest)
    if not release.sees(settings, person) and person.entrant is None:
        raise NotFound(published.NO_SUCH_CONTEST)
    return settings, person


async def _held_own(ctx: Context, contest: ContestId, user_id: int) -> EntrantRow:
    """The person's entrant, held until the unit of work ends, once they are
    found still in it. `NotFound` for someone in none.
    """
    member = await membership(ctx, contest, user_id)
    if member is None:
        raise NotFound(NOT_IN_ONE)
    (row,) = await _hold_entrants(ctx, contest, [member.entrant_id])
    if user_id not in await member_ids(ctx, row.id):
        raise EntrantChanged("Your entrant changed meanwhile; try again.")
    return row


async def _managed(ctx: Context, organiser: Organiser, contest: ContestId) -> ContestDefinition:
    require(organiser, contest_scope(contest), Role.MANAGER)
    return await published.contest(ctx, contest)


async def _decided(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    entrant: uuid.UUID,
    allowed: tuple[Status, ...],
    doing: str,
) -> tuple[ContestDefinition, EntrantRow]:
    """The contest's settings and the entrant, held until the unit of work
    ends, once the organiser is checked to manage the contest and the
    entrant's status allows `doing`.
    """
    settings = await _managed(ctx, organiser, contest)
    (row,) = await _hold_entrants(ctx, contest, [entrant])
    registration.refuse_undecidable(Status(row.status), allowed, doing)
    return settings, row


def _refuse_window(settings: ContestDefinition, now: datetime) -> None:
    """`RegistrationClosed` unless the window is open and the contest still
    running or to come.
    """
    if settings.state is State.ARCHIVED or now >= settings.end:
        raise RegistrationClosed("The contest has ended.")
    registration.refuse_closed(settings.registration, now)


async def _refuse_staff(ctx: Context, user_id: int, contest: ContestId) -> None:
    if await roles.holds_role_in_contest(ctx, user_id, contest):
        raise IsStaff("Organisers of a contest cannot also enter it.")


async def _emails(ctx: Context, user_id: int, settings: ContestDefinition) -> tuple[str, ...]:
    """The person's confirmed addresses, asked of the forge only when some
    way takes only some addresses.
    """
    if not registration.asks_address(settings.registration):
        return ()
    return await ctx.forge.identity.verified_emails(user_id)


async def _emails_or_none(
    ctx: Context, user_id: int, settings: ContestDefinition
) -> tuple[str, ...]:
    """`_emails`, or none when the forge does not answer, for a panel that
    says what it can rather than failing.
    """
    try:
        return await _emails(ctx, user_id, settings)
    except PortError:
        return ()


async def _hold_people(ctx: Context, contest: ContestId, user_ids: Sequence[int]) -> None:
    """Hold each person in the contest, in the order of their ids, until the
    unit of work ends, so two changes of who one person is with happen one
    after another.
    """
    for user_id in sorted(set(user_ids)):
        await ctx.db.execute(
            text("SELECT pg_advisory_xact_lock(:space, hashtext(:person))"),
            {"space": PEOPLE_LOCK, "person": f"{contest}|{user_id}"},
        )


async def _hold_places(ctx: Context, contest: ContestId) -> None:
    """Wait for any other registration for the contest to finish, and hold
    the contest's places until this unit of work ends.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:contest))"),
        {"space": PLACES_LOCK, "contest": contest},
    )


async def _hold_entrants(
    ctx: Context, contest: ContestId, entrants: Sequence[uuid.UUID]
) -> builtins.list[EntrantRow]:
    """Each entrant of the contest, held until the unit of work ends, taken
    in the order of their ids and given back in the order asked.
    `NotFound` for one that is not the contest's.
    """
    held: dict[uuid.UUID, EntrantRow] = {}
    for entrant in sorted(set(entrants)):
        found = (
            await ctx.db.execute(
                select(EntrantRow)
                .where(EntrantRow.id == entrant, EntrantRow.contest_id == contest)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFound(NO_SUCH_ENTRANT)
        held[entrant] = found
    return [held[entrant] for entrant in entrants]


async def _refuse_full(ctx: Context, contest: ContestId, settings: ContestDefinition) -> None:
    capacity = settings.registration.capacity
    if capacity is None:
        return
    taken = await ctx.db.scalar(
        select(func.count())
        .select_from(EntrantRow)
        .where(EntrantRow.contest_id == contest, EntrantRow.status.in_(registration.REGISTERED))
    )
    if (taken or 0) >= capacity:
        raise ContestFull("Every place in this contest is taken.")


async def _waiting(ctx: Context, entrant: uuid.UUID) -> int:
    """How many invites into the entrant are still waiting."""
    found = await ctx.db.scalar(
        select(func.count())
        .select_from(InviteRow)
        .where(
            InviteRow.entrant_id == entrant,
            InviteRow.status == InviteStatus.PENDING,
            InviteRow.expires_at > ctx.now,
        )
    )
    return found or 0


def _new_name(settings: ContestDefinition, name: str | None) -> str | None:
    """The name a new or renamed entrant takes: needed and checked when the
    contest's entrants may be more than one person, refused when they are
    one each.
    """
    if settings.entrants.most == 1:
        if name is not None:
            raise EntrantsAlone("Everyone enters this contest alone, so an entrant has no name.")
        return None
    if name is None:
        raise EntrantNameRequired("Give the entrant a name.")
    return rules.checked_name(name)


async def _free_name(ctx: Context, contest: ContestId, username: str) -> str:
    """The name an entrant made by an invite takes: the person's username,
    or that with the first number after it that no entrant of the contest
    has, ignoring case.
    """
    taken: set[str | None] = set(
        (
            await ctx.db.scalars(
                select(func.lower(EntrantRow.name)).where(
                    EntrantRow.contest_id == contest, EntrantRow.name.is_not(None)
                )
            )
        ).all()
    )
    name, number = username, 1
    while name.lower() in taken:
        number += 1
        name = f"{username} ({number})"
    return name


async def _make(
    ctx: Context,
    contest: ContestId,
    name: str | None,
    user_id: int,
    qualified: Qualified,
    *,
    by: int | None,
) -> EntrantRow:
    """A new pending entrant with the person as its only member."""
    if name is not None:
        await _refuse_taken(ctx, contest, name, None)
    row = EntrantRow(
        id=new_id(),
        contest_id=contest,
        name=name,
        status=Status.PENDING,
        registered_at=ctx.now,
        registered_by_user_id=by,
    )
    ctx.db.add(row)
    _add_member(ctx, row, user_id, qualified)
    await _flush_once(ctx)
    return row


async def _rename(ctx: Context, row: EntrantRow, name: str | None) -> None:
    if name is not None:
        await _refuse_taken(ctx, ContestId(row.contest_id), name, row.id)
    row.name = name
    await _flush_once(ctx)


async def _refuse_taken(
    ctx: Context, contest: ContestId, name: str, other_than: uuid.UUID | None
) -> None:
    query = select(EntrantRow.id).where(
        EntrantRow.contest_id == contest, func.lower(EntrantRow.name) == name.lower()
    )
    if other_than is not None:
        query = query.where(EntrantRow.id != other_than)
    if await ctx.db.scalar(query.limit(1)) is not None:
        raise EntrantNameTaken(f"This contest has an entrant named {name} already.")


async def _flush_once(ctx: Context) -> None:
    """Write what is pending; a name another entrant took in the same instant
    is refused as taken, and a person another change put in an entrant in
    the same instant as in one already.
    """
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError as exc:
        if "ix_entrants_contest_id_name" in str(exc.orig):
            raise EntrantNameTaken("This contest has an entrant of that name already.") from exc
        if "ix_entrant_members_one_entrant" in str(exc.orig):
            raise InEntrant("That person is in an entrant of this contest already.") from exc
        raise


def _add_member(ctx: Context, row: EntrantRow, user_id: int, qualified: Qualified) -> None:
    ctx.db.add(
        EntrantMember(
            entrant_id=row.id,
            contest_id=row.contest_id,
            user_id=user_id,
            status=MemberStatus.MEMBER,
            joined_at=ctx.now,
            way=qualified.way,
            way_name=qualified.name,
            way_kind=qualified.kind,
            approval=qualified.approval,
            eligibility=qualified.eligibility,
        )
    )


async def _join(
    ctx: Context,
    row: EntrantRow,
    settings: ContestDefinition,
    user_id: int,
    qualified: Qualified,
) -> None:
    """Put someone in an entrant the caller holds, within the contest's
    `entrants.most`: access to every part of its workspace first, then the
    member row, then the approval rule.
    """
    current = await member_ids(ctx, row.id)
    rules.refuse_full(len(current), settings.entrants.most)
    await ctx.forge.workspaces.share_workspace(workspace_of(ctx, row), [*current, user_id])
    _add_member(ctx, row, user_id, qualified)
    await _flush_once(ctx)
    await _after_join(ctx, row, settings, qualified.approval)


async def _after_join(
    ctx: Context, row: EntrantRow, settings: ContestDefinition, joined: Approval
) -> None:
    """Once someone joined by a way with `joined` approval: a pending
    entrant that is complete and whose every member came in by a way that
    approves straight away is approved, and an approved one that someone
    joined by a way an organiser approves is pending again.
    """
    status = Status(row.status)
    if status is Status.PENDING:
        approvals = await ctx.db.scalars(
            select(EntrantMember.approval).where(
                EntrantMember.entrant_id == row.id, EntrantMember.status == MemberStatus.MEMBER
            )
        )
        if rules.approved_on_its_own(settings, [Approval(found) for found in approvals]):
            await _approve(ctx, row, decided_by=None)
    elif status is Status.APPROVED and joined is Approval.MANUAL:
        row.status = Status.PENDING
        row.decided_at = None
        row.decided_by_user_id = None
        row.reason = None
        await ctx.db.flush()
        log.info("entrants.pending_again", entrant=str(row.id))


async def _approve(ctx: Context, row: EntrantRow, *, decided_by: int | None) -> None:
    _decide(ctx, row, Status.APPROVED, decided_by=decided_by, reason=None)
    await ctx.db.flush()
    places.ahead_for(ctx, ContestId(row.contest_id), row.id)
    log.info("entrants.approved", entrant=str(row.id), by=decided_by)


def _decide(
    ctx: Context, row: EntrantRow, status: Status, *, decided_by: int | None, reason: str | None
) -> None:
    row.status = status
    row.decided_at = ctx.now
    row.decided_by_user_id = decided_by
    row.reason = reason


async def _take_out(ctx: Context, row: EntrantRow, member: EntrantMember) -> bool:
    """End a membership: the person's access to the entrant's workspace taken
    away first, then the row `left`, and an entrant left with nobody and no
    submissions deleted. Whether the entrant is still there.
    """
    await _close(ctx, workspace_of(ctx, row), [member.user_id])
    member.status = MemberStatus.LEFT
    member.left_at = ctx.now
    await ctx.db.flush()
    if not await member_ids(ctx, row.id) and not await submitted(ctx, row):
        await _end(ctx, row)
        log.info("entrants.emptied", entrant=str(row.id))
        return False
    return True


async def _end(ctx: Context, row: EntrantRow) -> None:
    """Delete an entrant, its members' rows kept `left`, with when, so each
    person's uploads afterwards are told from its, and the invites into it
    still waiting withdrawn.
    """
    await ctx.db.execute(
        update(EntrantMember)
        .where(EntrantMember.entrant_id == row.id, EntrantMember.status == MemberStatus.MEMBER)
        .values(status=MemberStatus.LEFT, left_at=ctx.now)
    )
    await ctx.db.execute(
        update(InviteRow)
        .where(InviteRow.entrant_id == row.id, InviteRow.status == InviteStatus.PENDING)
        .values(status=InviteStatus.WITHDRAWN, decided_at=ctx.now)
    )
    await ctx.db.delete(row)
    await ctx.db.flush()


async def _close(ctx: Context, workspace: WorkspaceId, user_ids: Sequence[int]) -> None:
    """Take the people's access to the workspace away and keep what is in
    it; someone whose account is gone has no access left to take.
    """
    if not user_ids:
        return
    try:
        await ctx.forge.workspaces.close_workspace(workspace, builtins.list(user_ids))
    except NotFound:
        log.info("entrants.account_gone", workspace=workspace)
        return
    log.info("entrants.closed", workspace=workspace, user_ids=builtins.list(user_ids))


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
    members: dict[uuid.UUID, builtins.list[EntrantMember]] = {}
    for member in await ctx.db.scalars(
        select(EntrantMember)
        .where(EntrantMember.entrant_id.in_(ids), EntrantMember.status == MemberStatus.MEMBER)
        .order_by(EntrantMember.joined_at, EntrantMember.created_at, EntrantMember.user_id)
    ):
        members.setdefault(member.entrant_id, []).append(member)
    waiting: dict[uuid.UUID, builtins.list[Invited]] = {}
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
