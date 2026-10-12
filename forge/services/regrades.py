"""What a regrade of many changed, for the organisers (TASK-FORMAT.md section
3.5, #448).

A rejudge and the regrade a save that changes how a task grades starts are
each kept as one `Regrade` (`gradings.regrade`), and every attempt one
makes, and each retry of those, points at it. A regrade is read per
submission by its latest attempt of it, compared with the result that
counted for the submission when the regrade began: the latest earlier
attempt that finished with one. Both are scored as organisers see them,
every group, nothing held back, the late factor applied, by the scoring the
boards use (`scores.Scorer`), and `forge.domain.regrades.change_of` says
what changed.

- `latest` gives, for each task of the contest the organiser observes, its
  latest regrade with its counts by change, how many it graded, and whether
  it is still running, until a newer regrade of the task replaces it or an
  organiser dismisses it: a summary that went on a timer would hide a
  broken checker from an organiser who was away;
- `dismiss` takes a task's regrade off the contest's page, for a manager of
  the task;
- `gradings` gives a regrade's gradings, one per submission, its latest
  attempt of the regrade, as the feed gives gradings, each with what the
  regrade did to it (`GradingRecord.change`), narrowed to one change and
  to a member's or an entrant's submissions when asked.

Each reads where an attempt stands as every read does, so a failure it
finds starts that attempt's retry by itself (`gradings.notice`).
"""

import builtins
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from fractions import Fraction

from sqlalchemy import select

from forge.db.tables import Grading, Regrade
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import NotFound, PortError
from forge.domain.grading import UNFINISHED, GradingStatus
from forge.domain.ids import ContestId, PublicationId, TaskId, WorkspaceId
from forge.domain.regrades import (
    GRADED,
    Change,
    Compared,
    RegradeCause,
    Side,
    change_of,
    counted,
)
from forge.domain.roles import Role, contest_id_of, task_scope
from forge.domain.scoring import ACCEPTED, EVERYTHING, everywhere
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrants, gradings, names, published, scores, timelines
from forge.services.access import Organiser, require
from forge.services.gradings import FeedEntry, GradingRecord

log = get_logger(__name__)

NO_SUCH_REGRADE = "There is no such regrade."


@dataclass(frozen=True, slots=True)
class Summary:
    """A task's latest regrade of many as the contest's page shows it: its
    id, its task by id, name and letter, the name none for a task the
    platform has no name for and the letter none once the contest no longer
    lists it, what started it, the publication it grades against, when and
    by whom, the organiser's username none when the forge does not say;
    how many submissions it took, how many of them it graded, finishing with
    a result, whether any is still running, and how many each change counts.
    """

    id: uuid.UUID
    task: TaskId
    task_name: str | None
    label: str | None
    cause: RegradeCause
    publication: PublicationId
    started_at: datetime
    started_by: int | None
    started_by_name: str | None
    total: int
    graded: int
    running: bool
    counts: Mapping[Change, int]


@dataclass(frozen=True, slots=True)
class _Read:
    """One submission of a regrade: its latest attempt of it, and what the
    regrade did to it.
    """

    row: Grading
    compared: Compared


@action
async def latest(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Summary, ...]:
    """The latest regrade of each of the contest's tasks the organiser
    observes, newest first, but one an organiser dismissed. `Forbidden` for
    someone holding no role in the contest.
    """
    observed = gradings.observed_tasks(organiser, contest)
    query = select(Regrade).where(
        Regrade.task_id.startswith(f"{contest}/", autoescape=True)
        if observed is None
        else Regrade.task_id.in_(sorted(observed))
    )
    newest: dict[str, Regrade] = {}
    for row in await ctx.db.scalars(query.order_by(Regrade.started_at.desc(), Regrade.id.desc())):
        newest.setdefault(row.task_id, row)
    shown = [row for row in newest.values() if row.dismissed_at is None]
    if not shown:
        return ()
    tasks = await names.names_of(ctx, {row.task_id for row in shown})
    settings = await _settings(ctx, contest)
    found: builtins.list[Summary] = []
    for row in sorted(shown, key=lambda each: (each.started_at, each.id), reverse=True):
        read = await _read(ctx, row, settings)
        changes = [each.compared.change for each in read]
        name = tasks.get(row.task_id)
        found.append(
            Summary(
                id=row.id,
                task=TaskId(row.task_id),
                task_name=name,
                label=settings.label_of(name) if settings is not None and name else None,
                cause=RegradeCause(row.cause),
                publication=PublicationId(row.publication_id),
                started_at=row.started_at,
                started_by=row.started_by,
                started_by_name=(
                    await entrants.username_of(ctx, row.started_by) or None
                    if row.started_by is not None
                    else None
                ),
                total=len(read),
                graded=sum(1 for change in changes if change in GRADED),
                running=Change.RUNNING in changes,
                counts=counted(changes),
            )
        )
    return tuple(found)


@action
async def dismiss(ctx: Context, organiser: Organiser, task: TaskId, regrade: uuid.UUID) -> None:
    """Take the task's regrade off the contest's page, for everyone, for a
    manager of the task; one already dismissed stays as it was. `NotFound`
    for a regrade of another task, or none.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    row = await ctx.db.get(Regrade, regrade, with_for_update=True)
    if row is None or row.task_id != task:
        raise NotFound(NO_SUCH_REGRADE)
    if row.dismissed_at is None:
        row.dismissed_at = ctx.now
        row.dismissed_by = organiser.user.id
        await ctx.db.flush()
        log.info("regrades.dismissed", regrade=str(regrade), user_id=organiser.user.id)


@action
async def gradings_of(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    regrade: uuid.UUID,
    *,
    change: Change | None = None,
    user: str | None = None,
    entrant: uuid.UUID | None = None,
    limit: int = 100,
) -> tuple[FeedEntry, ...]:
    """The regrade's gradings, one per submission, its latest attempt of the
    regrade, newest first by when each was queued, at most `limit` and never
    more than 500, each with what the regrade did to it; narrowed to one
    `change`, and to the submissions the member `user` made or the entrant
    `entrant`'s, as the feed narrows. A regrade of a task the organiser
    does not observe, of another contest, or none gives none. `Forbidden`
    for someone holding no role in the contest.
    """
    observed = gradings.observed_tasks(organiser, contest)
    row = await ctx.db.get(Regrade, regrade)
    if (
        row is None
        or contest_id_of(task_scope(TaskId(row.task_id))) != contest
        or (observed is not None and row.task_id not in observed)
    ):
        return ()
    whose = await gradings.narrowed(ctx, contest, user=user, entrant=entrant)
    if whose is None:
        return ()
    kept: set[uuid.UUID] | None = None
    if whose:
        kept = set(
            await ctx.db.scalars(select(Grading.id).where(Grading.regrade_id == regrade, *whose))
        )
    read = [
        each
        for each in await _read(ctx, row, await _settings(ctx, contest))
        if (kept is None or each.row.id in kept)
        and (change is None or each.compared.change is change)
    ]
    read.sort(key=lambda each: (each.row.queued_at, each.row.id), reverse=True)
    read = read[: max(1, min(limit, gradings.LIST_LIMIT))]
    rows = [each.row for each in read]
    last = await gradings.latest_of(ctx, rows)
    goods = await gradings.last_goods(ctx, rows)
    gone = await gradings.lost(ctx, rows)
    limits = await gradings.retry_limits(ctx, [found for found in rows if found.id in last], gone)
    records: builtins.list[GradingRecord] = [
        replace(
            gradings.record(
                ctx,
                each.row,
                gone,
                latest=each.row.id in last,
                last_good=goods.get(each.row.id),
                retry_limit=limits.get(each.row.id),
            ),
            change=each.compared,
        )
        for each in read
    ]
    return await gradings.entries(ctx, contest, records)


async def _settings(ctx: Context, contest: ContestId) -> ContestDefinition | None:
    """The contest's settings, or none when they do not read or the forge
    does not answer.
    """
    try:
        return await published.contest(ctx, contest)
    except PortError:
        return None


async def _read(
    ctx: Context, regrade: Regrade, settings: ContestDefinition | None
) -> builtins.list[_Read]:
    """Each submission the regrade took, by its latest attempt of it, and
    what the regrade did to it. A failure found among the attempts that are
    their submission's latest starts its retry by itself.
    """
    mine = builtins.list(
        await ctx.db.scalars(select(Grading).where(Grading.regrade_id == regrade.id))
    )
    if not mine:
        return []
    attempts: dict[str, builtins.list[Grading]] = {}
    for row in await ctx.db.scalars(
        select(Grading).where(
            Grading.submission_id.in_(sorted({row.submission_id for row in mine}))
        )
    ):
        attempts.setdefault(row.submission_id, []).append(row)
    ours: dict[str, builtins.list[Grading]] = {}
    for row in mine:
        ours.setdefault(row.submission_id, []).append(row)
    newest = {
        submission: max(rows, key=lambda row: row.attempt) for submission, rows in ours.items()
    }
    gone = await gradings.lost(ctx, newest.values())
    await gradings.notice(
        ctx,
        [
            row
            for submission, row in newest.items()
            if row.attempt == max(each.attempt for each in attempts[submission])
        ],
        gone,
    )
    task = TaskId(regrade.task_id)
    current = await published.task(ctx, task, settings) if settings is not None else None
    scorer = scores.Scorer(ctx, current) if current is not None else None
    factors = await _factors(ctx, current, settings, builtins.list(newest.values()))
    found: builtins.list[_Read] = []
    for submission, row in newest.items():
        first = min(each.attempt for each in ours[submission])
        earlier = gradings.latest_good(
            each for each in attempts[submission] if each.attempt < first
        )
        status = gradings.status_of(ctx, row, gone)
        before = await _side(scorer, earlier, factors) if earlier is not None else None
        if status in UNFINISHED:
            compared = Compared(Change.RUNNING, before, None)
        elif status is GradingStatus.OVER_LIMIT:
            compared = Compared(Change.OVER_LIMIT, before, None)
        elif not gradings.good(row):
            compared = Compared(Change.FAILED, before, None)
        else:
            now = await _side(scorer, row, factors)
            compared = Compared(change_of(before, now), before, now)
        found.append(_Read(row, compared))
    return found


async def _factors(
    ctx: Context,
    current: published.PublishedTask | None,
    settings: ContestDefinition | None,
    rows: Sequence[Grading],
) -> dict[str, Fraction]:
    """The late factor of each of the rows' submissions, by workspace and
    number, as organisers see it, none while the task does not read.
    """
    if current is None or settings is None or not rows:
        return {}
    contest = contest_id_of(task_scope(current.id))
    owners = {
        row.workspace_id: ctx.forge.workspaces.owner_of(WorkspaceId(row.workspace_id))
        for row in rows
    }
    extensions = await timelines.of_owners(ctx, contest, set(owners.values()))
    return {
        row.submission_id: scores.factor(
            settings, current, extensions[owners[row.workspace_id]], row.submitted_at
        )
        for row in rows
    }


async def _side(
    scorer: scores.Scorer | None, row: Grading, factors: Mapping[str, Fraction]
) -> Side | None:
    """A finished attempt as a regrade compares it, or none when it cannot
    be scored: the task or the publication it ran under does not read.
    """
    if scorer is None:
        return None
    graded = await scorer.graded(PublicationId(row.publication_id))
    if graded is None:
        return None
    factor = factors.get(row.submission_id)
    if factor is None:
        return None
    scored = await scorer.scored(row, graded, factor)
    tests = [test for group in scored.groups for test in group.tests]
    values = scored.values(EVERYTHING)
    better = {
        name: measure.better
        for name, measure in scored.measures.items()
        if measure.better is not None and name in values
    }
    return Side(
        attempt=row.attempt,
        points=scored.points(everywhere, EVERYTHING),
        accepted=sum(1 for test in tests if test.outcome == ACCEPTED),
        tests=len(tests),
        stopped=(row.result or {}).get("stopped"),
        values={name: values[name] for name in better},
        better=better,
    )
