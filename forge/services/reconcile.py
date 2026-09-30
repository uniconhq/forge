"""The reconcile pass: every submission at the forge has its gradings. A
submission is named at the forge before its grading rows are inserted, so
a submit that stopped between the two, and is never sent again, leaves a
submission nobody grades; and a database restored from a backup lacks the
gradings of every submission made since. The forge is where submissions
are kept, so the pass reads them there.

For each contest, each task with a publication, and each workspace opened
in the contest, it lists the workspace's submissions of the task and
inserts, for any that has no grading at all, one queued grading per stage
the task grades on submit, against the task's current publication, attempt
1, with the idempotency key its protected version's note carries, so the
submit sent again later finds the rows. A submission found without rows is
checked again under the lock a submit of that workspace to that task holds,
so a submit still under way is waited for rather than raced.

The timed pass, `gradings.reconcile`, runs every ten minutes over the
contests that are running, and those that ended within the last day, the
end counted with the longest time extension any contestant of the contest
has, since such a contestant submits after it. With `every_contest` it runs
the same over every contest of every org the platform made, which is what
`unicon-forge reconcile --all` does after a restore.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Contestant, Grading
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import NotFound, PortError
from forge.domain.ids import ContestId, WorkspaceId
from forge.domain.submissions import Submitted
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import gradings, published
from forge.services.published import PublishedTask
from forge.services.submissions import SUBMIT_LOCK

log = get_logger(__name__)

INTERVAL = timedelta(minutes=10)
AFTER_END = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What one pass did: how many contests it read, how many submissions it
    found at the forge, and how many gradings it inserted for the ones that
    had none.
    """

    contests: int
    submissions: int
    inserted: int


async def timed(ctx: Context) -> None:
    """The timed pass, run on the pass's unit of work under its advisory lock."""
    await reconcile(ctx, every_contest=False)


async def reconcile(ctx: Context, *, every_contest: bool) -> Reconciled:
    """Give every submission without gradings its gradings, over the running
    contests, or over every contest when `every_contest`.
    """
    contests = seen = inserted = 0
    for contest, settings in await published.every_contest(ctx):
        if not every_contest and not await _recent(ctx, contest, settings):
            continue
        contests += 1
        try:
            tasks = await published.tasks(ctx, contest, settings)
        except PortError as exc:
            log.warning("reconcile.contest_unreadable", contest=contest, error=type(exc).__name__)
            continue
        workspaces = await _workspaces(ctx, contest)
        for task in tasks:
            found, made = await _task(ctx, task, workspaces)
            seen += found
            inserted += made
    if inserted:
        log.warning("reconcile.inserted", gradings=inserted)
    log.info("reconcile.done", contests=contests, submissions=seen, inserted=inserted)
    return Reconciled(contests, seen, inserted)


async def _recent(ctx: Context, contest: ContestId, settings: ContestDefinition) -> bool:
    """Whether the contest is running, or ended for its last contestant
    within `AFTER_END`.
    """
    if settings.state is not State.PUBLISHED or ctx.now < settings.start:
        return False
    if ctx.now < settings.end + AFTER_END:
        return True
    longest = (
        await ctx.db.execute(
            select(func.max(Contestant.time_extension_seconds)).where(
                Contestant.contest_id == contest
            )
        )
    ).scalar()
    return ctx.now < settings.end + timedelta(seconds=longest or 0) + AFTER_END


async def _workspaces(ctx: Context, contest: ContestId) -> list[WorkspaceId]:
    found = await ctx.db.execute(
        select(Contestant.workspace_id)
        .where(Contestant.contest_id == contest, Contestant.workspace_id.is_not(None))
        .order_by(Contestant.workspace_id)
    )
    return [WorkspaceId(workspace) for workspace in found.scalars() if workspace is not None]


async def _task(
    ctx: Context, task: PublishedTask, workspaces: list[WorkspaceId]
) -> tuple[int, int]:
    graded = set(
        (
            await ctx.db.execute(
                select(Grading.submission_id).where(Grading.task_id == task.id).distinct()
            )
        ).scalars()
    )
    seen = inserted = 0
    for workspace in workspaces:
        try:
            submissions = await ctx.forge.workspaces.list_submissions(workspace, task.id)
        except NotFound:
            continue
        except PortError as exc:
            log.warning(
                "reconcile.place_unreadable",
                workspace=workspace,
                task=task.id,
                error=type(exc).__name__,
            )
            continue
        seen += len(submissions)
        missing = [made for made in submissions if made.id not in graded]
        if missing:
            inserted += await _insert(ctx, task, workspace, missing)
    return seen, inserted


async def _insert(
    ctx: Context, task: PublishedTask, workspace: WorkspaceId, missing: list[Submitted]
) -> int:
    """The gradings of the submissions that have none, checked again under
    the lock a submit of the workspace to the task holds.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:target))"),
        {"space": SUBMIT_LOCK, "target": f"{workspace}|{task.id}"},
    )
    graded = set(
        (
            await ctx.db.execute(
                select(Grading.submission_id).where(
                    Grading.submission_id.in_([made.id for made in missing])
                )
            )
        ).scalars()
    )
    inserted = 0
    for made in missing:
        if made.id in graded:
            continue
        try:
            async with ctx.db.begin_nested():
                rows = gradings.queue_submission(
                    ctx,
                    task=task.id,
                    workspace=workspace,
                    submission=made,
                    publication=task.publication,
                    definition=task.definition,
                    key=made.key,
                    at=made.at,
                )
                await ctx.db.flush()
        except IntegrityError:
            log.warning(
                "reconcile.key_taken", task=task.id, workspace=workspace, number=made.number
            )
            continue
        inserted += len(rows)
        log.warning(
            "reconcile.graded",
            task=task.id,
            workspace=workspace,
            number=made.number,
            gradings=len(rows),
        )
    return inserted
