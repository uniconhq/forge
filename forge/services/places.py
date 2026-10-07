"""Making a contestant's place to submit a task: the repository at the forge
their submissions to it go in (`workspaces`). A place is made as the
platform, holding no connection while the forge makes it, which takes it a
quarter of a second or more, and the contestant's row is then held and read
again, so someone removed meanwhile has the access just given taken back.

With `places_ahead` on, places are made before anyone needs them: approving
someone starts making theirs at every task the contest has published, and a
task's first publication starts making it for everyone approved. Each starts
once the request that approved or published has committed, and that request
answers without waiting for it (`Context.in_background`). The work takes
`AHEAD_AT_ONCE` turns in a process, apart from the turns the first uploads
take (`submitters`), so a crowd approved at once never holds an upload back,
and the tasks it makes places at are read once a minute per contest and not
once per person. The first upload to a task makes its place whenever it finds
it missing, so work that a restart cuts short, or that stops at a forge that
does not answer, costs that upload the time it takes and nothing more.

In a contest whose settings turn teams on, places ahead are a team's: a
team's first member starts making its place at every task the contest has
published, and a task's first publication makes it for every team with
members. Approving someone there makes nothing ahead, since a person who
joins a team never uses a place of their own, and someone who enters alone
makes theirs at their first upload. A team's place is made with every
member on it, and once made it is held against the team's members as they
stand then, so someone who joined meanwhile is added and someone who left
loses the access just given (`teams.settle`).

Only a published contest that has not ended gets places ahead, and only at
a task with a publication that is released at the contest's start or has
been released already (`made_ahead`): a task released at the start is made
ahead before it, since that is the case this is for, while one whose
`release_at` is later, or that the contest does not list, gets none until it
is released, and a contest still a draft or over has nobody to submit.
Anyone the work misses makes their place at their first upload.

A person the forge refuses for a reason of their own, such as a repository
that is somebody else's, is passed over and the rest are made; a forge that
does not answer stops the work, since every call after would wait out its
timeout too. A process that stops lets the place being made finish and
makes no more (`ctx.stopping`).
"""

import uuid
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import datetime, timedelta

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.db.tables import Team as TeamRow
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import NotFound, PortError, TeamChanged, Unavailable
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import TeamOwner
from forge.domain.registration import Status
from forge.domain.roles import contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import published, teams, workspaces
from forge.services.turns import Turns

log = get_logger(__name__)

AHEAD_AT_ONCE = 2
"""How many pieces of work making places ahead run at once in a process."""
TASKS_KEPT = timedelta(minutes=1)
"""How long the tasks a contest makes places at are kept once read."""


_ahead = Turns(AHEAD_AT_ONCE)


async def make(
    ctx: Context,
    row: Contestant,
    task: TaskId,
    turn: AbstractAsyncContextManager[object] | None = None,
) -> bool:
    """Make the contestant's place to submit `task`, their team's when they
    are in one, the forge's part inside `turn`, then hold their row and read
    it again. False, with the access just given taken back, when they are no
    longer approved by then. `TeamChanged` when they left their team while
    it was made. `PortError` when the forge fails, with the row not held.
    """
    contest = ContestId(row.contest_id)
    standing = await teams.standing(ctx, contest, row.user_id)
    workspace = ctx.forge.workspaces.workspace_of(contest, standing.owner)
    await ctx.let_go()
    async with turn if turn is not None else nullcontext():
        await ctx.forge.workspaces.open_submission_place(workspace, task, list(standing.members))
    await ctx.db.refresh(row, with_for_update=True)
    members = standing.members
    if isinstance(standing.owner, TeamOwner):
        members = await teams.settle(ctx, standing.owner.team_id, workspace, standing.members)
    if row.status != Status.APPROVED:
        await workspaces.close(ctx, row)
        log.info("places.taken_back", task=task, user_id=row.user_id)
        return False
    if row.user_id not in members:
        raise TeamChanged("Your team changed while your place to submit was made; try again.")
    return True


async def make_for_team(
    ctx: Context,
    contest: ContestId,
    team: uuid.UUID,
    task: TaskId,
    turn: AbstractAsyncContextManager[object] | None = None,
) -> bool:
    """Make a team's place to submit `task` with every member on it, the
    forge's part inside `turn`, and hold it against the members as they are
    then. False for a team with nobody in it.
    """
    members = tuple(await teams.member_ids(ctx, team))
    if not members:
        return False
    owner = TeamOwner(team)
    workspace = ctx.forge.workspaces.workspace_of(contest, owner)
    await ctx.let_go()
    async with turn if turn is not None else nullcontext():
        await ctx.forge.workspaces.open_submission_place(workspace, task, members)
    await teams.settle(ctx, team, workspace, members)
    return True


def ahead_for(ctx: Context, row: Contestant) -> None:
    """Once this unit of work commits, start making the approved
    contestant's places at every task their contest has published.
    """
    if not ctx.settings.places_ahead:
        return
    contest, user_id = ContestId(row.contest_id), row.user_id

    async def work(later: Context) -> None:
        async with _ahead():
            settings = await _settings_if_open(later, contest)
            if settings is None or teams.is_on(settings):
                return
            tasks = await _tasks(later, contest)
            await _make_each(later, contest, [(task, user_id) for task in tasks])

    ctx.in_background(work)


def ahead_for_team(ctx: Context, contest: ContestId, team: uuid.UUID) -> None:
    """Once this unit of work commits, start making the team's places at
    every task its contest has published.
    """
    if not ctx.settings.places_ahead:
        return

    async def work(later: Context) -> None:
        async with _ahead():
            tasks = await _tasks(later, contest)
            await _make_each_team(later, contest, [(task, team) for task in tasks])

    ctx.in_background(work)


def ahead_at(ctx: Context, task: TaskId) -> None:
    """Once this unit of work commits, start making the task's place for
    every approved contestant of its contest, the earliest registered first.
    """
    if not ctx.settings.places_ahead:
        return
    contest = contest_id_of(task_scope(task))
    ctx.memo.forget(_tasks_name(contest))

    async def work(later: Context) -> None:
        async with _ahead():
            settings = await _settings_if_open(later, contest)
            if settings is None:
                return
            found = await published.task(later, task, settings)
            if found is None or not made_ahead(settings, found.name, later.now):
                return
            if teams.is_on(settings):
                every = await later.db.scalars(
                    select(TeamRow.id)
                    .where(TeamRow.contest_id == contest)
                    .order_by(TeamRow.created_at)
                )
                await _make_each_team(later, contest, [(task, team) for team in every.all()])
                return
            approved = (
                select(Contestant.user_id)
                .where(Contestant.contest_id == contest, Contestant.status == Status.APPROVED)
                .order_by(Contestant.registered_at)
            )
            user_ids = (await later.db.execute(approved)).scalars().all()
            await _make_each(later, contest, [(task, user_id) for user_id in user_ids])

    ctx.in_background(work)


async def _tasks(ctx: Context, contest: ContestId) -> tuple[TaskId, ...]:
    """Every task of the contest with a publication that `made_ahead` takes,
    kept for `TASKS_KEPT` so a crowd approved together reads them once, and
    forgotten when a task is first published; none for a contest that is
    gone, a draft, archived or over.
    """

    async def read() -> tuple[TaskId, ...]:
        settings = await _settings_if_open(ctx, contest)
        if settings is None:
            return ()
        return tuple(
            found.id
            for found in await published.tasks(ctx, contest, settings)
            if made_ahead(settings, found.name, ctx.now)
        )

    return await ctx.memo.remembered(_tasks_name(contest), TASKS_KEPT, read)


def made_ahead(settings: ContestDefinition, task: str, now: datetime) -> bool:
    """Whether places are made ahead at the task, by name: the contest lists
    it, and its `release_at` is the contest's start or has passed.
    """
    entry = settings.entry(task)
    if entry is None:
        return False
    released_at = settings.release_of(entry)
    return released_at <= max(settings.start, now)


def _tasks_name(contest: ContestId) -> str:
    return f"places.tasks.{contest}"


async def _settings_if_open(ctx: Context, contest: ContestId) -> ContestDefinition | None:
    try:
        settings = await published.contest(ctx, contest)
    except NotFound:
        return None
    if settings.state is not State.PUBLISHED or ctx.now >= settings.end:
        return None
    return settings


async def _make_each_team(
    ctx: Context, contest: ContestId, wanted: list[tuple[TaskId, uuid.UUID]]
) -> None:
    """Make each team's place in turn, as `_make_each` makes a person's."""
    made = 0
    for done, (task, team) in enumerate(wanted):
        if ctx.stopping():
            log.info("places.ahead_cut_short", contest=contest, made=made, left=len(wanted) - done)
            return
        try:
            if await make_for_team(ctx, contest, team, task):
                made += 1
        except Unavailable as exc:
            await ctx.db.rollback()
            log.warning(
                "places.ahead_stopped",
                contest=contest,
                made=made,
                left=len(wanted) - done,
                error=type(exc).__name__,
                detail=exc.detail,
            )
            return
        except PortError as exc:
            await ctx.db.rollback()
            log.warning(
                "places.ahead_passed_over",
                task=task,
                team=str(team),
                error=type(exc).__name__,
                detail=exc.detail,
            )
        await ctx.db.commit()
    if wanted:
        log.info("places.ahead_made", contest=contest, made=made, of=len(wanted))


async def _make_each(ctx: Context, contest: ContestId, wanted: list[tuple[TaskId, int]]) -> None:
    """Make each place in turn, committing after each so no row stays held,
    for whoever is still approved when their turn comes. A person the forge
    refuses is passed over; a forge that does not answer, or a process that
    stops, ends the rest, which their first uploads make.
    """
    made = 0
    for done, (task, user_id) in enumerate(wanted):
        if ctx.stopping():
            log.info("places.ahead_cut_short", contest=contest, made=made, left=len(wanted) - done)
            return
        row = (
            await ctx.db.execute(
                select(Contestant).where(
                    Contestant.contest_id == contest,
                    Contestant.user_id == user_id,
                    Contestant.status == Status.APPROVED,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        try:
            if await make(ctx, row, task):
                made += 1
        except Unavailable as exc:
            await ctx.db.rollback()
            log.warning(
                "places.ahead_stopped",
                contest=contest,
                made=made,
                left=len(wanted) - done,
                error=type(exc).__name__,
                detail=exc.detail,
            )
            return
        except PortError as exc:
            await ctx.db.rollback()
            log.warning(
                "places.ahead_passed_over",
                task=task,
                user_id=user_id,
                error=type(exc).__name__,
                detail=exc.detail,
            )
        await ctx.db.commit()
    if wanted:
        log.info("places.ahead_made", contest=contest, made=made, of=len(wanted))
