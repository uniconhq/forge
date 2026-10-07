"""Giving every submission at the forge its gradings, after a restore. A
database restored from a backup lacks the gradings of every submission made
since the backup, and the forge is where submissions are kept, so
`unicon-forge reconcile` reads them there, once, when the operator runs it.

For each contest of every org the platform made, each task with a
publication, and each person who registered for the contest and each team
of it, it lists their submissions of the task and inserts, for any that has
no grading at all, one queued grading against the task's current
publication, attempt 1, with the idempotency key its protected version's
note carries; its run starts once it commits. A submit of the same
submission sent again meanwhile cannot make a second row: the key is unique
for a workspace and task.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Contestant, Grading
from forge.domain.errors import NotFound, PortError
from forge.domain.ids import ContestId, WorkspaceId
from forge.domain.names import UserOwner
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import gradings, published, teams
from forge.services.published import PublishedTask

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What a run did: how many contests it read, how many submissions it
    found at the forge, and how many gradings it inserted for the ones that
    had none.
    """

    contests: int
    submissions: int
    inserted: int


async def reconcile(ctx: Context) -> Reconciled:
    """Give every submission without gradings its gradings, over every
    contest.
    """
    contests = seen = inserted = 0
    for contest, settings in await published.every_contest(ctx):
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
    log.info("reconcile.done", contests=contests, submissions=seen, inserted=inserted)
    return Reconciled(contests, seen, inserted)


async def _workspaces(ctx: Context, contest: ContestId) -> list[WorkspaceId]:
    """The workspace of everyone who registered for the contest, whatever
    became of their registration, since a removed contestant's submissions
    are kept, and of every team of it.
    """
    users = await ctx.db.execute(
        select(Contestant.user_id)
        .where(Contestant.contest_id == contest)
        .order_by(Contestant.user_id)
    )
    people = [
        ctx.forge.workspaces.workspace_of(contest, UserOwner(user)) for user in users.scalars()
    ]
    return [*people, *await teams.workspaces_of(ctx, contest)]


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
        for made in submissions:
            if made.id in graded:
                continue
            try:
                async with ctx.db.begin_nested():
                    gradings.queue_submission(
                        ctx,
                        task=task.id,
                        workspace=workspace,
                        submission=made,
                        publication=task.publication,
                        key=made.key,
                        at=made.at,
                    )
                    await ctx.db.flush()
            except IntegrityError:
                log.warning(
                    "reconcile.key_taken", task=task.id, workspace=workspace, number=made.number
                )
                continue
            inserted += 1
            log.info("reconcile.graded", task=task.id, workspace=workspace, number=made.number)
    return seen, inserted
