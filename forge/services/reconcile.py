"""Mending the platform's records, the forge's and the CI's, after a restore
or an upgrade. A database
restored from a backup lacks the gradings of every submission made since
the backup, and the forge is where submissions are kept, so `unicon-forge
reconcile` reads them there, once, when the operator runs it. The CI's own
database, restored from a dump taken at another moment or alone, may not
know a task made since, and nothing else would activate it again. And a
contest or task made before a change to what keeps a place right at the
forge, such as the platform writing its files alone (#443), is not made
again, so this puts that right too.

First, every contest and every task of every org the platform made is
secured again (`content.secure`): the roles that reach it, its history kept
from being rewritten, its files written by the platform alone, and a task's
publications reserved for the platform, each put back only where it is
missing or wrong. A place the forge fails on is logged and the rest still
run.

For each contest of every org the platform made and each task with a
publication, it first activates the task at the CI as the org's account,
which changes nothing for a task the CI knows. Then, for each entrant of
the contest, whatever became of it, it lists its submissions of the task
and inserts, for any that has no grading at all, one queued grading against
the task's current publication, attempt 1, with the idempotency key its
protected version's note carries and nobody named as its submitter, which
the forge's list does not say; its run starts once it commits. A submit
of the same submission sent again meanwhile cannot make a second row: the
key is unique for a workspace and task. A task the
CI cannot activate is logged and its gradings are still inserted, since a
grading whose run cannot start ends as a system error, which the first
read a minute later retries once by itself and staff after that.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Grading
from forge.domain.errors import Forbidden, NotFound, PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgId, TaskId, WorkspaceId
from forge.domain.roles import task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrants, gradings, org_accounts, published
from forge.services.published import PublishedTask

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What a run did: how many contests it read, how many submissions it
    found at the forge, how many gradings it inserted for the ones that had
    none, how many published tasks it activated that the CI did not
    know, and how many contests and tasks it had to secure again.
    """

    contests: int
    submissions: int
    inserted: int
    activated: int
    secured: int = 0


async def reconcile(ctx: Context) -> Reconciled:
    """Secure every contest and task again, activate every published task
    at the CI and give every submission without gradings its gradings, over
    every contest.
    """
    secured = await _secure_every_place(ctx)
    contests = seen = inserted = activated = 0
    for contest, settings in await published.every_contest(ctx):
        contests += 1
        try:
            tasks = await published.tasks(ctx, contest, settings)
        except PortError as exc:
            log.warning("reconcile.contest_unreadable", contest=contest, error=type(exc).__name__)
            continue
        workspaces = await entrants.workspaces_of(ctx, contest)
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
        secured=secured,
    )
    return Reconciled(contests, seen, inserted, activated, secured)


async def _secure_every_place(ctx: Context) -> int:
    """How many contests and tasks had something put back when each was
    secured again, read as the platform.
    """
    secured = 0
    orgs = await org_accounts.org_ids(ctx)
    await ctx.let_go()
    for org in orgs:
        try:
            contests = await ctx.forge.content.list_contests(PLATFORM, org)
        except PortError as exc:
            log.warning("reconcile.org_unreadable", org=org, error=type(exc).__name__)
            continue
        for contest in contests:
            try:
                tasks = await ctx.forge.content.list_tasks(PLATFORM, contest)
            except PortError as exc:
                log.warning(
                    "reconcile.contest_unreadable", contest=contest, error=type(exc).__name__
                )
                continue
            places: list[ContestId | TaskId] = [contest, *tasks]
            for place in places:
                try:
                    put_back = await ctx.forge.content.secure(place)
                except PortError as exc:
                    log.warning("reconcile.secure_failed", place=place, error=type(exc).__name__)
                    continue
                if put_back:
                    secured += 1
                    log.info("reconcile.secured", place=place, put_back=put_back)
    return secured


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
                        submitted_by=None,
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
