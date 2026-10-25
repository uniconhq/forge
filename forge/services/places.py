"""Making an entrant's place to submit a task: the repository at the forge its
submissions to it go in, with every member of the entrant on it. A place is
made as the platform, holding no connection while the forge makes it, which
takes it a quarter of a second or more, and the entrant's row is then held
and read again: the place is held against the members as they stand then,
so someone who joined meanwhile is added and someone who left loses the
access just given (`member_lookup.settle`), and an entrant removed, rejected or deleted
meanwhile has the access just given taken back from everyone.

With `places_ahead` on, places are made before anyone needs them: approving
an entrant starts making its places at every task the contest has
published, and a task's first live publication starts making it for every
approved entrant: the save that publishes it, or, for one checked first,
the take-over of its passed check (`pipelines.take_over`). Each starts
once the request that approved, published or took over has committed, and
that request answers without waiting for it (`Context.in_background`).
The work takes `AHEAD_AT_ONCE` turns in a process, apart from the turns the
first uploads take (`submitters`), so a crowd approved at once never holds
an upload back, and the tasks it makes
places at are read once a minute per contest and not once per entrant. The
first upload to a task makes its place whenever it finds it missing, so
work that a restart cuts short, or that stops at a forge that does not
answer, costs that upload the time it takes and nothing more.

Only a published contest that has not ended gets places ahead, and only at
a task with a publication that is released at the contest's start or has
been released already (`made_ahead`): a task released at the start is made
ahead before it, since that is the case this is for, while one whose
`release_at` is later, or that the contest does not list, gets none until it
is released, and a contest still a draft or over has nobody to submit.
Anyone the work misses makes their place at their first upload.

An entrant the forge refuses for a reason of its own is passed over and the
rest are made; a forge that
does not answer stops the work, since every call after would wait out its
timeout too. A process that stops lets the place being made finish and
makes no more (`ctx.stopping`).
"""

import uuid
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import datetime, timedelta

from sqlalchemy import select

from forge.db.tables import Entrant as EntrantRow
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import EntrantChanged, NotFound, PortError, Unavailable
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import EntrantOwner
from forge.domain.registration import Status
from forge.domain.roles import contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrant_view, member_lookup
from forge.services.turns import Turns

log = get_logger(__name__)

AHEAD_AT_ONCE = 2
"""How many pieces of work making places ahead run at once in a process."""
TASKS_KEPT = timedelta(minutes=1)
"""How long the tasks a contest makes places at are kept once read."""


_ahead = Turns(AHEAD_AT_ONCE)


async def make(
    ctx: Context,
    contest: ContestId,
    entrant: uuid.UUID,
    task: TaskId,
    turn: AbstractAsyncContextManager[object] | None = None,
    *,
    for_user: int | None = None,
) -> bool:
    """Make the entrant's place to submit `task` with every member on it,
    the forge's part inside `turn`, then hold the entrant and settle its
    members. False for an entrant with nobody in it, or that is no longer
    approved by then, one removed, rejected or gone having the access just given taken
    back. `EntrantChanged` when `for_user`, who asked for it, left the
    entrant while it was made. `PortError` when the forge fails, with the
    entrant not held.
    """
    members = tuple(await member_lookup.member_ids(ctx, entrant))
    if not members:
        return False
    workspace = ctx.forge.workspaces.workspace_of(contest, EntrantOwner(entrant))
    await ctx.let_go()
    async with turn if turn is not None else nullcontext():
        await ctx.forge.workspaces.open_submission_place(workspace, task, list(members))
    current = await member_lookup.settle(ctx, entrant, workspace, members)
    status = await ctx.db.scalar(select(EntrantRow.status).where(EntrantRow.id == entrant))
    if status is None or status in (Status.REMOVED, Status.REJECTED):
        await ctx.forge.workspaces.close_workspace(workspace, sorted({*members, *current}))
        log.info("places.taken_back", task=task, entrant=str(entrant))
        return False
    if for_user is not None and for_user not in current:
        raise EntrantChanged(
            "Your team changed while your place to submit was being set up. Try again."
        )
    return status == Status.APPROVED


def ahead_for(ctx: Context, contest: ContestId, entrant: uuid.UUID) -> None:
    """Once this unit of work commits, start making the approved entrant's
    places at every task its contest has published.
    """
    if not ctx.settings.places_ahead:
        return

    async def work(later: Context) -> None:
        async with _ahead():
            tasks = await _tasks(later, contest)
            await _make_each(later, contest, [(task, entrant) for task in tasks])

    ctx.in_background(work)


def forget_tasks(ctx: Context, contest: ContestId) -> None:
    """Drop this process's list of the contest's tasks places are made
    ahead at, after a task left it.
    """
    ctx.memo.forget(_tasks_name(contest))


def ahead_at(ctx: Context, task: TaskId) -> None:
    """Once this unit of work commits, start making the task's place for
    every approved entrant of its contest, the earliest registered first.
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
            found = await entrant_view.task(later, task, settings)
            if found is None or not made_ahead(settings, found.name, later.now):
                return
            approved = await later.db.scalars(
                select(EntrantRow.id)
                .where(EntrantRow.contest_id == contest, EntrantRow.status == Status.APPROVED)
                .order_by(EntrantRow.registered_at, EntrantRow.id)
            )
            await _make_each(later, contest, [(task, entrant) for entrant in approved.all()])

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
            for found in await entrant_view.tasks(ctx, contest, settings)
            if made_ahead(settings, found.name, ctx.now)
        )

    return await ctx.memo.remembered(_tasks_name(contest), TASKS_KEPT, read)


def made_ahead(settings: ContestDefinition, task: str, now: datetime) -> bool:
    """Whether places are made ahead at the task, by name: the contest lists
    it, not withdrawn, and its `release_at` is the contest's start or has
    passed.
    """
    entry = settings.entry(task)
    if entry is None or entry.withdrawn:
        return False
    released_at = settings.release_of(entry)
    return released_at <= max(settings.start, now)


def _tasks_name(contest: ContestId) -> str:
    return f"places.tasks.{contest}"


async def _settings_if_open(ctx: Context, contest: ContestId) -> ContestDefinition | None:
    try:
        settings = await entrant_view.contest(ctx, contest)
    except NotFound:
        return None
    if settings.state is not State.PUBLISHED or ctx.now >= settings.end:
        return None
    return settings


async def _make_each(
    ctx: Context, contest: ContestId, wanted: list[tuple[TaskId, uuid.UUID]]
) -> None:
    """Make each place in turn, committing after each so no row stays held,
    for whichever entrant is still approved when its turn comes. An entrant
    the forge refuses is passed over; a forge that does not answer, or a
    process that stops, ends the rest, which their first uploads make.
    """
    made = 0
    for done, (task, entrant) in enumerate(wanted):
        if ctx.stopping():
            log.info("places.ahead_cut_short", contest=contest, made=made, left=len(wanted) - done)
            return
        status = await ctx.db.scalar(select(EntrantRow.status).where(EntrantRow.id == entrant))
        if status != Status.APPROVED:
            continue
        try:
            if await make(ctx, contest, entrant, task):
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
                entrant=str(entrant),
                error=type(exc).__name__,
                detail=exc.detail,
            )
        await ctx.db.commit()
    if wanted:
        log.info("places.ahead_made", contest=contest, made=made, of=len(wanted))
