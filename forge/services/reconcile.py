"""Mending the platform's records and the CI's after a restore. A database
restored from a backup lacks the gradings of every submission made since
the backup, and the forge is where submissions are kept, so `unicon-forge
reconcile` reads them there, once, when the operator runs it. The CI's own
database, restored from a dump taken at another moment or alone, may not
know a task made since, and nothing else would activate it again.

For each contest of every org the platform made and each task with a
publication, it first activates the task at the CI as the org's account,
which changes nothing for a task the CI knows. Then, for each person who
registered for the contest and each team of it, it lists their submissions
of the task and inserts, for any that has no grading at all, one queued
grading against the task's current publication, attempt 1, with the
idempotency key its protected version's note carries; its run starts once
it commits. A submit of the same submission sent again meanwhile cannot
make a second row: the key is unique for a workspace and task. A task the
CI cannot activate is logged and its gradings are still inserted, since a
grading whose run cannot start ends as a system error staff can retry.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Contestant, Grading
from forge.domain.errors import Forbidden, NotFound, PortError
from forge.domain.ids import ContestId, OrgId, WorkspaceId
from forge.domain.names import UserOwner
from forge.domain.roles import task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import gradings, org_accounts, published, teams
from forge.services.published import PublishedTask

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What a run did: how many contests it read, how many submissions it
    found at the forge, how many gradings it inserted for the ones that had
    none, and how many published tasks it activated that the CI did not
    know.
    """

    contests: int
    submissions: int
    inserted: int
    activated: int


async def reconcile(ctx: Context) -> Reconciled:
    """Activate every published task at the CI and give every submission
    without gradings its gradings, over every contest.
    """
    contests = seen = inserted = activated = 0
    for contest, settings in await published.every_contest(ctx):
        contests += 1
        try:
            tasks = await published.tasks(ctx, contest, settings)
        except PortError as exc:
            log.warning("reconcile.contest_unreadable", contest=contest, error=type(exc).__name__)
            continue
        workspaces = await _workspaces(ctx, contest)
        for task in tasks:
            activated += await _activate(ctx, task)
            found, made = await _task(ctx, task, workspaces)
            seen += found
            inserted += made
    log.info(
        "reconcile.done",
        contests=contests,
        submissions=seen,
        inserted=inserted,
        activated=activated,
    )
    return Reconciled(contests, seen, inserted, activated)


async def _activate(ctx: Context, task: PublishedTask) -> int:
    """1 when the CI did not know the task and now does, else 0. The org's
    account is signed in again once when the CI refuses it, as at the
    task's creation.
    """
    try:
        account = await org_accounts.identity(ctx, OrgId(task_scope(task.id).org))
        try:
            new = await ctx.forge.grading.activate(account, task.id)
        except Forbidden:
            account = await org_accounts.renew(ctx, account)
            new = await ctx.forge.grading.activate(account, task.id)
    except PortError as exc:
        log.warning("reconcile.activation_failed", task=task.id, error=type(exc).__name__)
        return 0
    if new:
        log.info("reconcile.activated", task=task.id)
    return int(new)


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
