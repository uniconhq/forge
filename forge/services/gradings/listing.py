import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, not_, or_, select, tuple_
from sqlalchemy.orm import aliased

from forge.db.tables import Entrant, Grading
from forge.db.tables.gradings import SAVE
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import (
    Forbidden,
    LogTooLarge,
    Misconfigured,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
)
from forge.domain.grading import RUN_LOG_MAX, WAITING, GradingStatus, log_key
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import EntrantOwner, is_username
from forge.domain.roles import (
    Role,
    ScopeKind,
    contest_id_of,
    contest_scope,
    holds,
    task_id_of,
    task_scope,
)
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrant_view, entrants, names
from forge.services.access import Organiser, require
from forge.services.gradings.attempt_history import last_goods, latest_of, record
from forge.services.gradings.lookups import find
from forge.services.gradings.records import (
    LIST_LIMIT,
    LOG_STORE_UNAVAILABLE,
    NO_LOG,
    NO_SUCH_GRADING,
    FeedEntry,
    GradingRecord,
    QueueDepth,
    Submitter,
)
from forge.services.gradings.room import retry_limits
from forge.services.gradings.row_state import _may_read_as, lost, status_of
from forge.services.gradings.watchdog import notice

log = get_logger(__name__)


@action
async def list_gradings(
    ctx: Context, organiser: Organiser, task: TaskId, *, limit: int = 100
) -> tuple[FeedEntry, ...]:
    """The task's gradings, newest first, at most `limit` of them and never
    more than 500, each with who submitted it, as the feed gives them, for
    an organiser observing the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task, Grading.pipeline != SAVE)
                .order_by(Grading.queued_at.desc(), Grading.id.desc())
                .limit(max(1, min(limit, LIST_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    last = await latest_of(ctx, rows)
    goods = await last_goods(ctx, rows)
    gone = await lost(ctx, rows)
    await notice(ctx, [row for row in rows if row.id in last], gone)
    limits = await retry_limits(ctx, [row for row in rows if row.id in last], gone)
    return await entries(
        ctx,
        contest_id_of(task_scope(task)),
        [
            record(
                ctx,
                row,
                gone,
                latest=row.id in last,
                last_good=goods.get(row.id),
                retry_limit=limits.get(row.id),
            )
            for row in rows
        ],
    )


@action
async def feed(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    *,
    task: TaskId | None = None,
    user: str | None = None,
    entrant: uuid.UUID | None = None,
    status: GradingStatus | None = None,
    latest: bool = False,
    limit: int = 100,
) -> tuple[FeedEntry, ...]:
    """The gradings of the contest's tasks the organiser observes, newest
    first, at most `limit` of them and never more than 500: of one `task`,
    of the submissions the member whose username is `user` made, of the
    entrant `entrant`, and reading as `status`, each filter when given, and
    with `latest` only each submission's latest attempt, so `status`
    `system_error` with `latest` gives the ones `queue_depth` counts as
    `failed`. A task, person or entrant the organiser cannot see there
    gives none, and so does a `user` that breaks the forge's username rule
    (`is_username`). `Forbidden` for someone holding no role in the
    contest.
    """
    observed = observed_tasks(organiser, contest)
    where = [in_contest(contest, observed), Grading.pipeline != SAVE]
    if task is not None:
        if observed is not None and task not in observed:
            return ()
        where.append(Grading.task_id == task)
    whose = await narrowed(ctx, contest, user=user, entrant=entrant)
    if whose is None:
        return ()
    where.extend(whose)
    if status is not None:
        where.append(_may_read_as(status, ctx.now))
    if latest:
        where.append(_is_latest())
    size = max(1, min(limit, LIST_LIMIT))
    kept: list[GradingRecord] = []
    after: tuple[datetime, uuid.UUID] | None = None
    # Whether a `dispatched` row's run is lost is known only once the CI is
    # asked, so a filter by status reads a page at a time until the page is
    # full; the rest of what a status reads as is told in the query.
    while len(kept) < size:
        query = select(Grading).where(*where)
        if after is not None:
            query = query.where(tuple_(Grading.queued_at, Grading.id) < after)
        rows = (
            (
                await ctx.db.execute(
                    query.order_by(Grading.queued_at.desc(), Grading.id.desc()).limit(size)
                )
            )
            .scalars()
            .all()
        )
        last = await latest_of(ctx, rows)
        goods = await last_goods(ctx, rows)
        gone = await lost(ctx, rows)
        await notice(ctx, [row for row in rows if row.id in last], gone)
        shown = [row for row in rows if status is None or status_of(ctx, row, gone) == status]
        limits = await retry_limits(ctx, [row for row in shown if row.id in last], gone)
        kept.extend(
            record(
                ctx,
                row,
                gone,
                latest=row.id in last,
                last_good=goods.get(row.id),
                retry_limit=limits.get(row.id),
            )
            for row in shown
        )
        if len(rows) < size:
            break
        after = (rows[-1].queued_at, rows[-1].id)
    return await entries(ctx, contest, kept[:size])


@action
async def queue_depth(ctx: Context, organiser: Organiser, contest: ContestId) -> QueueDepth:
    """How many gradings of the contest's tasks the organiser observes wait
    for a machine, by status, and how many failed and wait for staff, read
    in one go. `Forbidden` for someone holding no role in the contest.
    """
    observed = observed_tasks(organiser, contest)
    failing = and_(_is_latest(), _may_read_as(GradingStatus.SYSTEM_ERROR, ctx.now))
    rows = (
        (
            await ctx.db.execute(
                select(Grading).where(
                    in_contest(contest, observed),
                    Grading.pipeline != SAVE,
                    or_(Grading.status.in_(WAITING), failing),
                )
            )
        )
        .scalars()
        .all()
    )
    last = await latest_of(ctx, rows)
    gone = await lost(ctx, rows)
    await notice(ctx, [row for row in rows if row.id in last], gone)
    read = [(row, status_of(ctx, row, gone)) for row in rows]
    counted = Counter(status for _, status in read)
    failed = sum(
        1 for row, status in read if status is GradingStatus.SYSTEM_ERROR and row.id in last
    )
    return QueueDepth(
        queued=counted[GradingStatus.QUEUED],
        dispatched=counted[GradingStatus.DISPATCHED],
        failed=failed,
    )


def observed_tasks(organiser: Organiser, contest: ContestId) -> frozenset[TaskId] | None:
    """The contest's tasks the organiser observes: none to mean every one,
    for an observer of the contest or above, and otherwise the tasks they
    hold a role at. `Forbidden` when that is no task at all.
    """
    scope = contest_scope(contest)
    if holds(organiser.grants, scope, Role.OBSERVER):
        return None
    tasks = frozenset(
        task_id_of(grant.scope)
        for grant in organiser.grants
        if grant.scope.kind is ScopeKind.TASK
        and (grant.scope.org, grant.scope.contest) == (scope.org, scope.contest)
    )
    if not tasks:
        named = organiser.scope if organiser.scope == scope else scope
        raise Forbidden(
            f"This needs the observer role at {named.name} or at one of its tasks. Ask an "
            f"admin of {named.name} if you need it."
        )
    return tasks


async def narrowed(
    ctx: Context, contest: ContestId, *, user: str | None, entrant: uuid.UUID | None
) -> list[ColumnElement[bool]] | None:
    """What narrows a contest's gradings to the submissions the member whose
    username is `user` made and to the entrant `entrant`, each when given;
    none when no grading can match, a `user` that breaks the forge's
    username rule (`is_username`) or that no one holds among them.
    """
    where: list[ColumnElement[bool]] = []
    if user is not None:
        # A name no one can hold at the forge matches nobody, and is never
        # sent there.
        if not is_username(user):
            return None
        try:
            found = await ctx.forge.identity.find_user_by_username(user)
        except NotFound:
            return None
        where.append(Grading.submitted_by == found.id)
    if entrant is not None:
        where.append(Grading.workspace_id == _workspace(ctx, contest, EntrantOwner(entrant)))
    return where


def _is_latest() -> ColumnElement[bool]:
    """The rows that are their line's latest attempt, told in the query: no
    attempt of the same submission and pipeline after them.
    """
    later = aliased(Grading)
    return not_(
        exists().where(
            later.submission_id == Grading.submission_id,
            later.pipeline == Grading.pipeline,
            later.attempt > Grading.attempt,
        )
    )


def in_contest(contest: ContestId, observed: frozenset[TaskId] | None) -> ColumnElement[bool]:
    if observed is None:
        return Grading.task_id.startswith(f"{contest}/", autoescape=True)
    return Grading.task_id.in_(sorted(observed))


def _workspace(ctx: Context, contest: ContestId, owner: EntrantOwner) -> WorkspaceId:
    return ctx.forge.workspaces.workspace_of(contest, owner)


async def entries(
    ctx: Context, contest: ContestId, records: Sequence[GradingRecord]
) -> tuple[FeedEntry, ...]:
    """Each grading with whose it is and its task's name and label: the
    names and the entrants read first, and the usernames and the contest's
    settings from the forge once the connection is let go of.
    """
    tasks = await names.names_of(ctx, {found.task for found in records})
    by = await _submitters(ctx, records)
    settings = await _settings(ctx, contest) if records else None
    return tuple(
        FeedEntry(
            found,
            by[found.id],
            task_name=(name := tasks.get(found.task)),
            label=settings.label_of(name) if settings is not None and name is not None else None,
        )
        for found in records
    )


async def _settings(ctx: Context, contest: ContestId) -> ContestDefinition | None:
    """The contest's settings, or none when they do not read or the forge
    does not answer.
    """
    try:
        return await entrant_view.contest(ctx, contest)
    except PortError:
        return None


async def _submitters(ctx: Context, records: Sequence[GradingRecord]) -> dict[uuid.UUID, Submitter]:
    """Whose each grading is, with the names an organiser knows them by: the
    entrants read first, and the usernames from the forge once the
    connection is let go of, a forge that does not say leaving the name out.
    """
    owners = {
        found.id: ctx.forge.workspaces.owner_of(found.workspace).entrant_id for found in records
    }
    ids = sorted(set(owners.values()))
    rows = (await ctx.db.scalars(select(Entrant).where(Entrant.id.in_(ids)))).all() if ids else []
    shown = await entrants.display_names(ctx, rows)
    users: dict[int, str | None] = {}
    for found in records:
        if found.submitted_by is not None and found.submitted_by not in users:
            users[found.submitted_by] = await entrants.username_of(ctx, found.submitted_by) or None
    return {
        found.id: Submitter(
            entrant=owners[found.id],
            entrant_name=shown.get(owners[found.id]),
            user_id=found.submitted_by,
            username=users.get(found.submitted_by) if found.submitted_by is not None else None,
        )
        for found in records
    }


@action
async def run_log(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> bytes:
    """The log of the grading's run, for an organiser observing its task.
    `NotFound` for a grading whose task they do not observe, or one with no
    log, `LogTooLarge` for a log over `RUN_LOG_MAX` bytes, which is never
    read whole, and `Unavailable` when the store fails.
    """
    row = await find(ctx, grading)
    if row is None or not holds(organiser.grants, task_scope(TaskId(row.task_id)), Role.OBSERVER):
        raise NotFound(NO_SUCH_GRADING)
    if row.log_key is None:
        raise NotFound(NO_LOG)
    key = log_key(row.id, row.attempt)
    await ctx.let_go()
    try:
        return await ctx.forge.objects.read(key, max_size=RUN_LOG_MAX)
    except NotFound as exc:
        log.warning("gradings.log_missing", grading=str(grading))
        raise NotFound(NO_LOG) from exc
    except Misconfigured as exc:
        raise _log_failure(exc, grading) from None
    except Rejected as exc:
        log.warning("gradings.log_too_large", grading=str(grading), detail=exc.detail)
        raise LogTooLarge(
            f"The grading's log is larger than the {RUN_LOG_MAX} bytes Unicon shows.",
            limit=RUN_LOG_MAX,
        ) from None
    except PortError as exc:
        raise _log_failure(exc, grading) from None


def _log_failure(exc: PortError, grading: uuid.UUID) -> Unavailable:
    """What an organiser is told when the log store fails, in fixed words.
    What the store said goes to the log; its own codes name buckets and
    keys.
    """
    log.warning(
        "gradings.log_unreadable", grading=str(grading), error=type(exc).__name__, detail=exc.detail
    )
    return Unavailable(LOG_STORE_UNAVAILABLE)
