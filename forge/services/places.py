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

A contest that is archived or past its end gets no places ahead: anyone with
time left makes theirs at their first upload.
"""

import asyncio
import weakref
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import timedelta

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import NotFound, PortError
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import UserOwner
from forge.domain.registration import Status
from forge.domain.roles import contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import published, workspaces

log = get_logger(__name__)

AHEAD_AT_ONCE = 2
"""How many pieces of work making places ahead run at once in a process."""
TASKS_KEPT = timedelta(minutes=1)
"""How long the tasks a contest makes places at are kept once read."""


class Turns:
    """At most `at_once` holders at a time in a process, the rest waiting
    their turn in order, with one set of turns per event loop, since a
    semaphore belongs to the loop it is first used on.
    """

    def __init__(self, at_once: int) -> None:
        self.at_once = at_once
        self._by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )

    def __call__(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        turns = self._by_loop.get(loop)
        if turns is None:
            turns = self._by_loop[loop] = asyncio.Semaphore(self.at_once)
        return turns


_ahead = Turns(AHEAD_AT_ONCE)


async def make(
    ctx: Context,
    row: Contestant,
    task: TaskId,
    turn: AbstractAsyncContextManager[object] | None = None,
) -> bool:
    """Make the contestant's place to submit `task`, the forge's part inside
    `turn`, then hold their row and read it again. False, with the access
    just given taken back, when they are no longer approved by then.
    `PortError` when the forge fails, with the row not held.
    """
    workspace = ctx.forge.workspaces.workspace_of(ContestId(row.contest_id), UserOwner(row.user_id))
    await ctx.let_go()
    async with turn if turn is not None else nullcontext():
        await ctx.forge.workspaces.open_submission_place(workspace, task, [row.user_id])
    await ctx.db.refresh(row, with_for_update=True)
    if row.status != Status.APPROVED:
        await workspaces.close(ctx, row)
        log.info("places.taken_back", task=task, user_id=row.user_id)
        return False
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
            tasks = await _tasks(later, contest)
            await _make_each(later, contest, [(task, user_id) for task in tasks])

    ctx.in_background(work)


def ahead_at(ctx: Context, task: TaskId) -> None:
    """Once this unit of work commits, start making the task's place for
    every approved contestant of its contest, the earliest registered first.
    """
    if not ctx.settings.places_ahead:
        return
    contest = contest_id_of(task_scope(task))

    async def work(later: Context) -> None:
        async with _ahead():
            if await _settings_if_open(later, contest) is None:
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
    """Every task of the contest with a publication, kept for `TASKS_KEPT`
    so a crowd approved together reads them once; none for a contest that
    is gone, archived or over.
    """

    async def read() -> tuple[TaskId, ...]:
        settings = await _settings_if_open(ctx, contest)
        if settings is None:
            return ()
        return tuple(found.id for found in await published.tasks(ctx, contest, settings))

    return await ctx.memo.remembered(f"places.tasks.{contest}", TASKS_KEPT, read)


async def _settings_if_open(ctx: Context, contest: ContestId) -> ContestDefinition | None:
    try:
        settings = await published.contest(ctx, contest)
    except NotFound:
        return None
    if settings.state is State.ARCHIVED or ctx.now >= settings.end:
        return None
    return settings


async def _make_each(ctx: Context, contest: ContestId, wanted: list[tuple[TaskId, int]]) -> None:
    """Make each place in turn, committing after each so no row stays held,
    for whoever is still approved when their turn comes. A person whose
    account is gone is passed over; a forge that fails stops the rest, which
    their first uploads make.
    """
    made = 0
    for done, (task, user_id) in enumerate(wanted):
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
        except NotFound:
            log.info("places.ahead_passed_over", task=task, user_id=user_id)
        except PortError as exc:
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
        await ctx.db.commit()
    if wanted:
        log.info("places.ahead_made", contest=contest, made=made, of=len(wanted))
