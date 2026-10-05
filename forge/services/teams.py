"""Teams: people in a contest whose settings turn teams on, entering it
together and counting as one contestant (`forge.domain.teams` holds the
rules). A team is a `teams` row with its leader and a `team_members` row per
person; nothing about it is a forge role.

Everyone in a team is a contestant of the contest first, registered and
approved by the contest's own rules, so an invite-only contest, a code, an
email pattern and the capacity hold for every person. An approved contestant
creates a team and leads it, or asks to join one; the leader asks people in,
approves requests and removes members, up to the contest's `max_size`; a
member leaves. Organisers of the contest create and delete teams, move
people between them, remove a member and change a leader, under their own
role at the contest. Someone who has submitted to the contest on their own
joins no team (`submitted_alone`), since those results are theirs; a member
who leaves keeps nothing of the team's and submits on their own from then.

Once in a team, the team is the person's contestant: their workspace is the
team's (`TeamOwner`), so its gradings, its limits and its questions are the
team's, and every member sees all of them. Who may reach the team's
workspace changes in the request that changes the membership, before the
membership is written: someone who joins is given access to every part of it
already made, and someone who leaves, is removed or is moved loses it. A
call to the forge that fails fails the request, and asking again finishes
it, since giving or taking access twice changes nothing. A part made later
is made with whoever is a member then (`settle`), so a place to submit is
finished only once every member can write it.

A change takes the contestant row of each person it moves, then the team
rows it touches in the order of their ids, so two changes of one person or
one team happen one after another and a removal from the contest never
passes a join; a first upload making a place and a submit take the same
rows in the same order. A contestant's changes stop once the contest ends
or is archived, since a team's membership stands from then on; organisers
can still mend teams, and let members go after teams are turned off.
"""

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Contestant, Grading, TeamMember
from forge.db.tables import Team as TeamRow
from forge.domain import teams as rules
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import (
    Forbidden,
    InTeam,
    NotApproved,
    NotFound,
    PortError,
    SubmittedAlone,
    TeamHasSubmissions,
    TeamNameTaken,
    TeamsOff,
)
from forge.domain.identity import User
from forge.domain.ids import ContestId, WorkspaceId, new_id
from forge.domain.names import TeamOwner, UserOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.roles import Role, contest_scope
from forge.domain.sessions import Session
from forge.domain.teams import MemberStatus
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import places, published, release
from forge.services.access import Organiser, require

log = get_logger(__name__)

NO_SUCH_TEAM = "There is no such team in this contest."
NOT_LEADER = "Only the team's leader may do this."


@dataclass(frozen=True, slots=True)
class Member:
    """Someone in a team or asking or asked to be: who they are at the forge,
    or none once their account is gone, where they stand, and since when.
    """

    user_id: int
    user: User | None
    status: MemberStatus
    since: datetime


@dataclass(frozen=True, slots=True)
class Team:
    """A team: its id and name, its leader, its members, the people asked in
    or asking, and whether it has submitted anything.
    """

    id: uuid.UUID
    name: str
    leader: int | None
    members: tuple[Member, ...]
    pending: tuple[Member, ...]
    submitted: bool


@dataclass(frozen=True, slots=True)
class Listed:
    """A team as a contestant choosing one sees it: its name, its leader and
    how many are in it, of how many the contest allows.
    """

    id: uuid.UUID
    name: str
    leader: User | None
    size: int
    max_size: int


@dataclass(frozen=True, slots=True)
class Mine:
    """The signed-in person's place among the contest's teams: the team they
    are in, if any, and the teams they have asked to join or are asked into.
    """

    team: Team | None
    invited_to: tuple[Listed, ...]
    requested: tuple[Listed, ...]
    max_size: int


@dataclass(frozen=True, slots=True)
class Standing:
    """Whose workspace a contestant works in, and who its members are: their
    team's, with every member, or their own. `since` is when that began,
    which tells a first upload whether its place was made with them on it.
    """

    owner: WorkspaceOwner
    members: tuple[int, ...]
    since: datetime | None


# The contestant's actions.


@action
async def create(ctx: Context, session: Session, contest: ContestId, name: str) -> Team:
    """Make a team with the signed-in contestant as its leader and only
    member.
    """
    await _contestant_settings(ctx, session, contest)
    checked = rules.checked_name(name)
    await _hold_people(ctx, contest, [session.user_id])
    await _refuse_joining(ctx, contest, session.user_id)
    team = await _new_team(ctx, contest, checked)
    team.leader_user_id = session.user_id
    _add_member(ctx, team, session.user_id)
    await _drop_pending(ctx, contest, session.user_id)
    await _flush_once(ctx)
    _ahead(ctx, team)
    log.info("teams.created", team=str(team.id), contest=contest, by_user_id=session.user_id)
    return await _view(ctx, team)


@action
async def request(ctx: Context, session: Session, contest: ContestId, team: uuid.UUID) -> Team:
    """Ask to join a team, or accept the leader's invitation into it when
    there is one.
    """
    settings = await _contestant_settings(ctx, session, contest)
    await _hold_people(ctx, contest, [session.user_id])
    found = await _hold_team(ctx, contest, team)
    await _refuse_joining(ctx, contest, session.user_id)
    row = await _live_row(ctx, found.id, session.user_id)
    if row is not None and row.status == MemberStatus.INVITED:
        await _admit(ctx, found, row, settings)
        log.info("teams.joined", team=str(found.id), user_id=session.user_id)
    elif row is None:
        ctx.db.add(
            TeamMember(
                team_id=found.id,
                contest_id=contest,
                user_id=session.user_id,
                status=MemberStatus.REQUESTED,
            )
        )
        await ctx.db.flush()
        log.info("teams.requested", team=str(found.id), user_id=session.user_id)
    else:
        raise InTeam("You have asked to join this team already.")
    shown = await _view(ctx, found)
    if session.user_id not in {member.user_id for member in shown.members}:
        shown = replace(shown, pending=())
    return shown


@action
async def cancel(ctx: Context, session: Session, contest: ContestId, team: uuid.UUID) -> None:
    """Take back a request to join a team, or decline its invitation."""
    await _contestant_settings(ctx, session, contest, need_approved=False)
    await _hold_people(ctx, contest, [session.user_id])
    found = await _hold_team(ctx, contest, team)
    row = await _live_row(ctx, found.id, session.user_id)
    if row is None or row.status not in rules.PENDING:
        raise NotFound("You have no request or invitation for this team.")
    await ctx.db.delete(row)
    await ctx.db.flush()
    log.info("teams.cancelled", team=str(found.id), user_id=session.user_id)


@action
async def leave(ctx: Context, session: Session, contest: ContestId) -> None:
    """Leave the team the signed-in person is in. Their access to its
    workspace goes; what the team made stays the team's.
    """
    await _contestant_settings(ctx, session, contest, need_approved=False, even_off=True)
    await _hold_people(ctx, contest, [session.user_id])
    row = await _membership(ctx, contest, session.user_id)
    if row is None:
        raise NotFound("You are not in a team in this contest.")
    team = await _hold_team(ctx, contest, row.team_id)
    await _remove(ctx, team, row)
    log.info("teams.left", team=str(team.id), user_id=session.user_id)


# The leader's actions.


@action
async def invite(
    ctx: Context, session: Session, contest: ContestId, team: uuid.UUID, username: str
) -> Team:
    """Ask an approved contestant into the leader's team, or approve their
    request when they have asked already.
    """
    settings = await _contestant_settings(ctx, session, contest)
    user = await _person(ctx, username)
    await _hold_people(ctx, contest, sorted({session.user_id, user.id}))
    found = await _hold_team(ctx, contest, team)
    _refuse_not_leader(found, session.user_id)
    await _refuse_joining(ctx, contest, user.id)
    row = await _live_row(ctx, found.id, user.id)
    if row is not None and row.status == MemberStatus.REQUESTED:
        await _admit(ctx, found, row, settings)
        log.info("teams.joined", team=str(found.id), user_id=user.id, by_user_id=session.user_id)
    elif row is None:
        rules.refuse_full(await _size(ctx, found.id), settings.teams.max_size)
        ctx.db.add(
            TeamMember(
                team_id=found.id, contest_id=contest, user_id=user.id, status=MemberStatus.INVITED
            )
        )
        await ctx.db.flush()
        log.info("teams.invited", team=str(found.id), user_id=user.id, by_user_id=session.user_id)
    else:
        raise InTeam(f"{user.username} has been asked into this team already.")
    return await _view(ctx, found)


@action
async def approve(
    ctx: Context, session: Session, contest: ContestId, team: uuid.UUID, user_id: int
) -> Team:
    """Let in someone who asked to join the leader's team."""
    settings = await _contestant_settings(ctx, session, contest)
    await _hold_people(ctx, contest, sorted({session.user_id, user_id}))
    found = await _hold_team(ctx, contest, team)
    _refuse_not_leader(found, session.user_id)
    row = await _live_row(ctx, found.id, user_id)
    if row is None or row.status != MemberStatus.REQUESTED:
        raise NotFound("That person has not asked to join this team.")
    await _refuse_joining(ctx, contest, user_id)
    await _admit(ctx, found, row, settings)
    log.info("teams.joined", team=str(found.id), user_id=user_id, by_user_id=session.user_id)
    return await _view(ctx, found)


@action
async def remove(
    ctx: Context, session: Session, contest: ContestId, team: uuid.UUID, user_id: int
) -> Team:
    """Take someone out of the leader's team, or turn down their request or
    take back their invitation. The leader leaves with `leave`.
    """
    await _contestant_settings(ctx, session, contest)
    await _hold_people(ctx, contest, sorted({session.user_id, user_id}))
    found = await _hold_team(ctx, contest, team)
    _refuse_not_leader(found, session.user_id)
    if user_id == session.user_id:
        raise Forbidden("A leader leaves their team rather than removing themself.")
    await _take_out(ctx, found, user_id)
    log.info("teams.removed", team=str(found.id), user_id=user_id, by_user_id=session.user_id)
    return await _view(ctx, found)


# The organisers' actions.


@action
async def organise_create(
    ctx: Context, organiser: Organiser, contest: ContestId, name: str, *, leader: str | None = None
) -> Team:
    """Make a team, empty or led by the named approved contestant. Needs the
    manager role at the contest.
    """
    await _organiser_settings(ctx, organiser, contest)
    checked = rules.checked_name(name)
    user = await _person(ctx, leader) if leader is not None else None
    if user is not None:
        await _hold_people(ctx, contest, [user.id])
        await _refuse_joining(ctx, contest, user.id)
    team = await _new_team(ctx, contest, checked)
    if user is not None:
        team.leader_user_id = user.id
        _add_member(ctx, team, user.id)
        await _drop_pending(ctx, contest, user.id)
    await _flush_once(ctx)
    _ahead(ctx, team)
    log.info("teams.created", team=str(team.id), contest=contest, by_user_id=organiser.user.id)
    return await _view(ctx, team)


@action
async def organise_delete(
    ctx: Context, organiser: Organiser, contest: ContestId, team: uuid.UUID
) -> None:
    """Delete a team that has submitted nothing, taking every member's access
    to its workspace away. Needs the manager role at the contest, and works
    with teams turned off since, so its members can be let go.
    """
    await _organiser_settings(ctx, organiser, contest, even_off=True)
    members = [row.user_id for row in await _rows(ctx, team, [MemberStatus.MEMBER])]
    await _hold_people(ctx, contest, sorted(members))
    found = await _hold_team(ctx, contest, team)
    if await _submitted(ctx, contest, found.id):
        raise TeamHasSubmissions("This team has submitted, so it stays with its results.")
    current = await member_ids(ctx, found.id)
    if current:
        await ctx.forge.workspaces.close_workspace(_workspace(ctx, found), current)
    await _end_team(ctx, found)
    log.info("teams.deleted", team=str(team), contest=contest, by_user_id=organiser.user.id)


@action
async def organise_move(
    ctx: Context, organiser: Organiser, contest: ContestId, user_id: int, team: uuid.UUID
) -> Team:
    """Put an approved contestant in a team, out of whichever team they are
    in. Needs the manager role at the contest.
    """
    settings = await _organiser_settings(ctx, organiser, contest)
    await _hold_people(ctx, contest, [user_id])
    current = await _membership(ctx, contest, user_id)
    teams = sorted({team, *([current.team_id] if current is not None else [])})
    held = {found.id: found for found in [await _hold_team(ctx, contest, one) for one in teams]}
    target = held[team]
    if current is not None and current.team_id == team:
        return await _view(ctx, target)
    await _refuse_approved(ctx, contest, user_id)
    if await _submitted_alone(ctx, contest, user_id):
        raise SubmittedAlone(
            "That person has submitted on their own, so their results stay theirs."
        )
    rules.refuse_full(await _size(ctx, target.id), settings.teams.max_size)
    row = await _live_row(ctx, target.id, user_id)
    if current is not None:
        await _remove(ctx, held[current.team_id], current)
    if row is None:
        row = TeamMember(
            team_id=target.id, contest_id=contest, user_id=user_id, status=MemberStatus.INVITED
        )
        ctx.db.add(row)
    await _admit(ctx, target, row, settings)
    log.info("teams.moved", team=str(team), user_id=user_id, by_user_id=organiser.user.id)
    return await _view(ctx, target)


@action
async def organise_remove(
    ctx: Context, organiser: Organiser, contest: ContestId, team: uuid.UUID, user_id: int
) -> Team:
    """Take someone out of a team, or drop their request or invitation.
    Needs the manager role at the contest, with teams on or turned off since.
    """
    await _organiser_settings(ctx, organiser, contest, even_off=True)
    await _hold_people(ctx, contest, [user_id])
    found = await _hold_team(ctx, contest, team)
    await _take_out(ctx, found, user_id)
    log.info("teams.removed", team=str(team), user_id=user_id, by_user_id=organiser.user.id)
    return await _view(ctx, found)


@action
async def organise_lead(
    ctx: Context, organiser: Organiser, contest: ContestId, team: uuid.UUID, user_id: int
) -> Team:
    """Make a member the team's leader. Needs the manager role at the
    contest.
    """
    await _organiser_settings(ctx, organiser, contest)
    found = await _hold_team(ctx, contest, team)
    if user_id not in await member_ids(ctx, found.id):
        raise NotFound("That person is not a member of this team.")
    found.leader_user_id = user_id
    await ctx.db.flush()
    log.info("teams.led", team=str(team), user_id=user_id, by_user_id=organiser.user.id)
    return await _view(ctx, found)


# Reading.


@action
async def mine(ctx: Context, session: Session, contest: ContestId) -> Mine:
    """The signed-in person's team in the contest, and the teams they asked
    to join or are asked into.
    """
    settings, _ = await release.seen(ctx, session, contest)
    _refuse_off(settings)
    rows = (
        await ctx.db.execute(
            select(TeamMember).where(
                TeamMember.contest_id == contest,
                TeamMember.user_id == session.user_id,
                TeamMember.status != MemberStatus.LEFT,
            )
        )
    ).scalars()
    team: Team | None = None
    invited: list[Listed] = []
    requested: list[Listed] = []
    for row in builtins.list(rows):
        found = await ctx.db.get(TeamRow, row.team_id)
        if found is None:
            continue
        if row.status == MemberStatus.MEMBER:
            team = await _view(ctx, found)
        elif row.status == MemberStatus.INVITED:
            invited.append(await _listed(ctx, found, settings))
        else:
            requested.append(await _listed(ctx, found, settings))
    return Mine(team, tuple(invited), tuple(requested), settings.teams.max_size)


@action
async def listed(ctx: Context, session: Session, contest: ContestId) -> tuple[Listed, ...]:
    """Every team of the contest by name, for an approved contestant choosing
    one to join.
    """
    settings = await _contestant_settings(ctx, session, contest)
    found = (
        await ctx.db.execute(
            select(TeamRow).where(TeamRow.contest_id == contest).order_by(func.lower(TeamRow.name))
        )
    ).scalars()
    return tuple([await _listed(ctx, team, settings) for team in found])


@action
async def every(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Team, ...]:
    """Every team of the contest by name, with its members and the people
    asking or asked in. Needs the observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    found = (
        await ctx.db.execute(
            select(TeamRow).where(TeamRow.contest_id == contest).order_by(func.lower(TeamRow.name))
        )
    ).scalars()
    return tuple([await _view(ctx, team) for team in found])


# Building blocks for the services that work in a contestant's workspace.


async def standing(ctx: Context, contest: ContestId, user_id: int) -> Standing:
    """Whose workspace the person works in for the contest: their team's,
    with every member, while they are in one, and their own otherwise.
    """
    row = await _membership(ctx, contest, user_id)
    if row is not None:
        return Standing(
            TeamOwner(row.team_id), tuple(await member_ids(ctx, row.team_id)), row.joined_at
        )
    left = await ctx.db.scalar(
        select(func.max(TeamMember.left_at)).where(
            TeamMember.contest_id == contest, TeamMember.user_id == user_id
        )
    )
    return Standing(UserOwner(user_id), (user_id,), left)


async def everyone_in(ctx: Context, team: uuid.UUID) -> frozenset[int]:
    """Everyone who is or was a member of the team, whose messages on its
    desk are the team's.
    """
    found = await ctx.db.scalars(
        select(TeamMember.user_id).where(
            TeamMember.team_id == team,
            TeamMember.status.in_([MemberStatus.MEMBER, MemberStatus.LEFT]),
        )
    )
    return frozenset(found)


async def team_ids(ctx: Context, user_id: int) -> frozenset[str]:
    """The id of every team the person is a member of, in any contest."""
    found = await ctx.db.scalars(
        select(TeamMember.team_id).where(
            TeamMember.user_id == user_id, TeamMember.status == MemberStatus.MEMBER
        )
    )
    return frozenset(str(team) for team in found)


async def workspaces_of(ctx: Context, contest: ContestId) -> builtins.list[WorkspaceId]:
    """The workspace of every team of the contest."""
    found = await ctx.db.scalars(select(TeamRow.id).where(TeamRow.contest_id == contest))
    return [ctx.forge.workspaces.workspace_of(contest, TeamOwner(team)) for team in found.all()]


async def settle(
    ctx: Context, team: uuid.UUID, workspace: WorkspaceId, made_with: Sequence[int]
) -> tuple[int, ...]:
    """After a part of the team's workspace was made with `made_with` and no
    lock held, hold the team and, when its members have changed meanwhile,
    make exactly the members now the people who reach every part of it, so
    someone who joined is added and someone who left, or was left behind by
    a change that failed halfway, is taken off. The members now. The caller
    holds whatever contestant rows it needs first.
    """
    held = (
        await ctx.db.execute(
            select(TeamRow)
            .where(TeamRow.id == team)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current = tuple(await member_ids(ctx, team)) if held is not None else ()
    if set(current) != set(made_with):
        await ctx.forge.workspaces.share_workspace(workspace, current)
        log.info("teams.access_settled", team=str(team), members=list(current))
    return current


async def on_removed(ctx: Context, row: Contestant) -> None:
    """A contestant removed from the contest leaves their team, and asks and
    is asked into no other. The caller holds their contestant row.
    """
    contest = ContestId(row.contest_id)
    membership = await _membership(ctx, contest, row.user_id)
    if membership is not None:
        try:
            team = await _hold_team(ctx, contest, membership.team_id)
        except NotFound:
            team = None
        if team is not None:
            await _remove(ctx, team, membership)
    await _drop_pending(ctx, contest, row.user_id)


def is_on(settings: ContestDefinition) -> bool:
    return settings.teams.enabled


# The pieces.


async def _contestant_settings(
    ctx: Context,
    session: Session,
    contest: ContestId,
    *,
    need_approved: bool = True,
    even_off: bool = False,
) -> ContestDefinition:
    """The contest's settings, once it is one the person sees, its teams are
    on unless `even_off`, it has not ended or been archived, since a team's
    membership stands from then on, and, unless told otherwise, the person
    is its approved contestant.
    """
    settings, person = await release.seen(ctx, session, contest)
    if not even_off:
        _refuse_off(settings)
    if settings.state is State.ARCHIVED or ctx.now >= settings.end:
        raise Forbidden("Teams stand as they are once the contest ends; ask the organisers.")
    if need_approved and (person.row is None or person.row.status != Status.APPROVED):
        raise NotApproved("Only an approved contestant of the contest is in a team.")
    return settings


async def _organiser_settings(
    ctx: Context, organiser: Organiser, contest: ContestId, *, even_off: bool = False
) -> ContestDefinition:
    require(organiser, contest_scope(contest), Role.MANAGER)
    settings = await published.contest(ctx, contest)
    if not even_off:
        _refuse_off(settings)
    return settings


def _refuse_off(settings: ContestDefinition) -> None:
    if not settings.teams.enabled:
        raise TeamsOff("This contest has no teams.")


def _refuse_not_leader(team: TeamRow, user_id: int) -> None:
    if team.leader_user_id != user_id:
        raise Forbidden(NOT_LEADER)


async def _person(ctx: Context, username: str) -> User:
    try:
        return await ctx.forge.identity.find_user_by_username(username.strip())
    except NotFound as exc:
        raise NotFound(f"There is no user named {username!r} at the forge.") from exc


async def _hold_people(ctx: Context, contest: ContestId, user_ids: Sequence[int]) -> None:
    """Hold the contestant row of each person, in the order of their ids,
    until the unit of work ends.
    """
    for user_id in sorted(user_ids):
        await ctx.db.execute(
            select(Contestant.id)
            .where(Contestant.contest_id == contest, Contestant.user_id == user_id)
            .with_for_update()
        )


async def _hold_team(ctx: Context, contest: ContestId, team: uuid.UUID) -> TeamRow:
    found = (
        await ctx.db.execute(
            select(TeamRow)
            .where(TeamRow.id == team, TeamRow.contest_id == contest)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFound(NO_SUCH_TEAM)
    return found


async def _refuse_approved(ctx: Context, contest: ContestId, user_id: int) -> None:
    status = await ctx.db.scalar(
        select(Contestant.status).where(
            Contestant.contest_id == contest, Contestant.user_id == user_id
        )
    )
    if status != Status.APPROVED:
        raise NotApproved("Only an approved contestant of the contest is in a team.")


async def _refuse_joining(ctx: Context, contest: ContestId, user_id: int) -> None:
    """Refuse someone who may not join a team: not an approved contestant, a
    member of a team in the contest already, or someone who has submitted on
    their own.
    """
    await _refuse_approved(ctx, contest, user_id)
    if await _membership(ctx, contest, user_id) is not None:
        raise InTeam("That person is in a team in this contest already.")
    if await _submitted_alone(ctx, contest, user_id):
        raise SubmittedAlone(
            "That person has submitted on their own, so their results stay theirs."
        )


async def _submitted_alone(ctx: Context, contest: ContestId, user_id: int) -> bool:
    workspace = ctx.forge.workspaces.workspace_of(contest, UserOwner(user_id))
    found = await ctx.db.scalar(
        select(Grading.id).where(Grading.workspace_id == workspace).limit(1)
    )
    return found is not None


async def _submitted(ctx: Context, contest: ContestId, team: uuid.UUID) -> bool:
    workspace = ctx.forge.workspaces.workspace_of(contest, TeamOwner(team))
    found = await ctx.db.scalar(
        select(Grading.id).where(Grading.workspace_id == workspace).limit(1)
    )
    return found is not None


async def _membership(ctx: Context, contest: ContestId, user_id: int) -> TeamMember | None:
    return (
        await ctx.db.execute(
            select(TeamMember).where(
                TeamMember.contest_id == contest,
                TeamMember.user_id == user_id,
                TeamMember.status == MemberStatus.MEMBER,
            )
        )
    ).scalar_one_or_none()


async def _live_row(ctx: Context, team: uuid.UUID, user_id: int) -> TeamMember | None:
    return (
        await ctx.db.execute(
            select(TeamMember).where(
                TeamMember.team_id == team,
                TeamMember.user_id == user_id,
                TeamMember.status != MemberStatus.LEFT,
            )
        )
    ).scalar_one_or_none()


async def _rows(
    ctx: Context, team: uuid.UUID, statuses: Sequence[MemberStatus]
) -> builtins.list[TeamMember]:
    found = await ctx.db.execute(
        select(TeamMember)
        .where(TeamMember.team_id == team, TeamMember.status.in_(list(statuses)))
        .order_by(TeamMember.joined_at, TeamMember.created_at, TeamMember.user_id)
    )
    return builtins.list(found.scalars())


async def member_ids(ctx: Context, team: uuid.UUID) -> builtins.list[int]:
    return [row.user_id for row in await _rows(ctx, team, [MemberStatus.MEMBER])]


async def _size(ctx: Context, team: uuid.UUID) -> int:
    return len(await member_ids(ctx, team))


async def _new_team(ctx: Context, contest: ContestId, name: str) -> TeamRow:
    taken = await ctx.db.scalar(
        select(TeamRow.id).where(
            TeamRow.contest_id == contest, func.lower(TeamRow.name) == name.lower()
        )
    )
    if taken is not None:
        raise TeamNameTaken(f"This contest has a team named {name} already.")
    team = TeamRow(id=new_id(), contest_id=contest, name=name)
    ctx.db.add(team)
    return team


async def _flush_once(ctx: Context) -> None:
    """Write what is pending; a name another team took in the same instant is
    refused as taken.
    """
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError as exc:
        if "ix_teams_contest_id_name" not in str(exc.orig):
            raise
        raise TeamNameTaken("This contest has a team of that name already.") from exc


def _add_member(ctx: Context, team: TeamRow, user_id: int) -> None:
    ctx.db.add(
        TeamMember(
            team_id=team.id,
            contest_id=team.contest_id,
            user_id=user_id,
            status=MemberStatus.MEMBER,
            joined_at=ctx.now,
        )
    )


async def _admit(ctx: Context, team: TeamRow, row: TeamMember, settings: ContestDefinition) -> None:
    """Make someone asked in, or asking, a member: within the contest's size,
    with access to every part of the team's workspace given first, and their
    other requests and invitations in the contest dropped.
    """
    current = await member_ids(ctx, team.id)
    rules.refuse_full(len(current), settings.teams.max_size)
    await ctx.forge.workspaces.share_workspace(_workspace(ctx, team), [*current, row.user_id])
    row.status = MemberStatus.MEMBER
    row.joined_at = ctx.now
    if team.leader_user_id is None:
        team.leader_user_id = row.user_id
    await ctx.db.flush()
    await _drop_pending(ctx, ContestId(team.contest_id), row.user_id)
    if not current:
        _ahead(ctx, team)


async def _take_out(ctx: Context, team: TeamRow, user_id: int) -> None:
    row = await _live_row(ctx, team.id, user_id)
    if row is None:
        raise NotFound("That person is not in this team and has not asked to join it.")
    if row.status in rules.PENDING:
        await ctx.db.delete(row)
        await ctx.db.flush()
        return
    await _remove(ctx, team, row)


async def _remove(ctx: Context, team: TeamRow, row: TeamMember) -> None:
    """End a membership: the person's access to the team's workspace taken
    away first, then the row `left`, the leadership passed to the member who
    joined earliest when it was theirs, and a team left with nobody and no
    submissions deleted.
    """
    await ctx.forge.workspaces.close_workspace(_workspace(ctx, team), [row.user_id])
    row.status = MemberStatus.LEFT
    row.left_at = ctx.now
    await ctx.db.flush()
    remaining = await member_ids(ctx, team.id)
    if team.leader_user_id == row.user_id:
        team.leader_user_id = remaining[0] if remaining else None
    if not remaining and not await _submitted(ctx, ContestId(team.contest_id), team.id):
        await _end_team(ctx, team)
        log.info("teams.emptied", team=str(team.id))
    await ctx.db.flush()


async def _end_team(ctx: Context, team: TeamRow) -> None:
    """Delete a team, its members' rows kept `left`, with when, so each
    person's own uploads afterwards are told from the team's, and the
    people asking or asked in dropped.
    """
    await ctx.db.execute(
        update(TeamMember)
        .where(TeamMember.team_id == team.id, TeamMember.status == MemberStatus.MEMBER)
        .values(status=MemberStatus.LEFT, left_at=ctx.now)
    )
    await ctx.db.execute(
        delete(TeamMember).where(
            TeamMember.team_id == team.id,
            TeamMember.status.in_([status.value for status in rules.PENDING]),
        )
    )
    await ctx.db.delete(team)
    await ctx.db.flush()


async def _drop_pending(ctx: Context, contest: ContestId, user_id: int) -> None:
    await ctx.db.execute(
        delete(TeamMember).where(
            TeamMember.contest_id == contest,
            TeamMember.user_id == user_id,
            TeamMember.status.in_([status.value for status in rules.PENDING]),
        )
    )


def _workspace(ctx: Context, team: TeamRow) -> WorkspaceId:
    return ctx.forge.workspaces.workspace_of(ContestId(team.contest_id), TeamOwner(team.id))


def _ahead(ctx: Context, team: TeamRow) -> None:
    if team.leader_user_id is not None:
        places.ahead_for_team(ctx, ContestId(team.contest_id), team.id)


async def _user(ctx: Context, user_id: int) -> User | None:
    """Who someone is at the forge, or none when their account is gone or
    the forge did not answer: a name shown never fails a change already
    made at the forge.
    """
    try:
        return await ctx.forge.identity.find_user(user_id)
    except PortError:
        return None


async def _view(ctx: Context, team: TeamRow) -> Team:
    rows = await _rows(
        ctx, team.id, [MemberStatus.MEMBER, MemberStatus.INVITED, MemberStatus.REQUESTED]
    )
    members: list[Member] = []
    pending: list[Member] = []
    for row in rows:
        shown = Member(
            user_id=row.user_id,
            user=await _user(ctx, row.user_id),
            status=MemberStatus(row.status),
            since=row.joined_at or row.created_at,
        )
        (members if row.status == MemberStatus.MEMBER else pending).append(shown)
    return Team(
        id=team.id,
        name=team.name,
        leader=team.leader_user_id,
        members=tuple(members),
        pending=tuple(pending),
        submitted=await _submitted(ctx, ContestId(team.contest_id), team.id),
    )


async def _listed(ctx: Context, team: TeamRow, settings: ContestDefinition) -> Listed:
    return Listed(
        id=team.id,
        name=team.name,
        leader=await _user(ctx, team.leader_user_id) if team.leader_user_id is not None else None,
        size=await _size(ctx, team.id),
        max_size=settings.teams.max_size,
    )
