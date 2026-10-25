"""What a regrade of many changed, for the organisers (TASK-FORMAT.md section
3.5, #448).

A rejudge and the regrade a save that changes how a task grades starts are
each kept as one `Regrade` (`gradings.regrade`), and every attempt one
makes, and each retry of those, points at it. A regrade is read per
submission by its latest attempt of it, compared with the result that
counted for the submission when the regrade began: the latest earlier
attempt that finished with one. Both are scored as organisers see them,
every group, nothing held back, the late factor applied, by the scoring the
boards use (`scores.Scorer`), the earlier one as it counted just before the
regrade began (`scores.Before`, `forge.domain.regrades.counted_under`), the
new one under the publication the regrade grades against, against the
results that counted by its last attempt, so a later save of points or a
later better best leaves a finished regrade reading as it did; and
`forge.domain.regrades.change_of` says what changed.

- `latest` gives, for each task of the contest the organiser observes, its
  latest regrade with its counts by change, how many it graded, and whether
  it is still running, until a newer regrade of the task replaces it or an
  organiser dismisses it: a summary that went on a timer would hide a
  broken checker from an organiser who was away;
- `dismiss` takes a task's regrade off the contest's page, for a manager of
  the task;
- `gradings_of` gives a regrade's gradings, one per submission, its latest
  attempt of the regrade, as the feed gives gradings, each with what the
  regrade did to it (`GradingRecord.change`), narrowed to one change and
  to a member's or an entrant's submissions when asked;
- `run_summary` gives a run task's regrade by what it changed: each
  submission whose values or verdicts it changed, old to new, worked out
  on read as every value is, the earlier attempt under the publication
  it counted under then, a bounded few and the rest counted; how many
  are still running, and how many it cannot compare; and, for a
  manager or admin of the contest alone, each board that reads the task
  before and after (`BoardDiff`, from phase 5.5).

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
from forge.db.tables.gradings import FRONT
from forge.domain.board_checks import boards_of
from forge.domain.boards import Counted
from forge.domain.definitions import TASK_FILE, ContestDefinition, parse_task
from forge.domain.errors import NotFound, PortError
from forge.domain.grading import UNFINISHED, GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, PublicationId, TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner
from forge.domain.regrades import (
    GRADED,
    BoardDiff,
    ChangeKind,
    Compared,
    RegradeCause,
    Side,
    ValueChange,
    VerdictChange,
    board_diff,
    change_of,
    counted,
    counted_under,
    run_changes,
)
from forge.domain.roles import Role, contest_id_of, contest_scope, holds, task_scope
from forge.domain.scoring import ACCEPTED, EVERYTHING, everywhere
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    boards,
    entrants,
    entries,
    gradings,
    names,
    published,
    scores,
    timelines,
)
from forge.services.access import Organiser, require
from forge.services.gradings import FeedEntry, GradingRecord

log = get_logger(__name__)

NO_SUCH_REGRADE = "There is no such regrade."
UNREAD_TASK = "The task or its contest no longer reads, so its regrade cannot be summed up."
NOT_RUN_TASK = (
    "This regrade is of a task graded test by test, whose gradings say what it changed. "
    "Only a run task's regrade is summed up by its values."
)


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
    counts: Mapping[ChangeKind, int]


@dataclass(frozen=True, slots=True)
class SubmissionChange:
    """One submission a run task's regrade changed: its latest attempt of
    the regrade, the submission by id and number, its workspace, and each
    value and verdict the regrade changed."""

    grading: uuid.UUID
    submission: str
    number: int
    workspace: WorkspaceId
    values: tuple[ValueChange, ...]
    verdicts: tuple[VerdictChange, ...]
    more_verdicts: int = 0


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A run task's regrade by what it changed: its id, task, cause and the
    publication it grades against; how many submissions it took, how many
    are still running, how many ended with no result, and how many it
    cannot compare since the publication their result counted under no
    longer reads; the first `CHANGED_SHOWN` submissions it changed, and
    how many more it did; and each board that reads the task before and
    after, none for an organiser who does not manage the contest."""

    id: uuid.UUID
    task: TaskId
    cause: RegradeCause
    publication: PublicationId
    total: int
    running: int
    failed: int
    unread: int
    changed: tuple[SubmissionChange, ...]
    more_changed: int
    boards: tuple[BoardDiff, ...] | None


CHANGED_SHOWN = 100
"""The most submissions a run task's regrade summary lists; the rest it
changed are counted."""


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
                running=ChangeKind.RUNNING in changes,
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
    change: ChangeKind | None = None,
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


@action
async def run_summary(
    ctx: Context, organiser: Organiser, task: TaskId, regrade: uuid.UUID
) -> RunSummary:
    """What a run task's regrade changed (SIMPLER-CORE.md 11.8), for a
    manager of the task: each submission whose values or verdicts its
    latest attempt changed against the attempt that counted when the
    regrade began, the latest earlier one that finished with a result,
    worked out under the publication the task's latest then
    (`counted_under`), and the new one under the regrade's; a submission
    still running, or whose attempt ended with no result, is counted, not
    compared, and so is one whose earlier result counted under a
    publication that no longer reads, never listed as changing everything.
    Submissions come in the order they were made, at most `CHANGED_SHOWN`
    of those changed. Both are worked out on read, every value as
    organisers read it, the extensions and marks as they stand now on both
    sides, so nothing is stored (stage 5 section 6 items 1 and 67). The
    late rows and marks of every submission are read in one go. The boards that
    read the task are a contest manager's or admin's alone, since a board
    diff shows every cell. `Forbidden` below a manager of the task,
    `NotFound` for a regrade of another task, or none, and for a task
    graded test by test or whose publication does not read.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    row = await ctx.db.get(Regrade, regrade)
    if row is None or row.task_id != task:
        raise NotFound(NO_SUCH_REGRADE)
    contest = contest_id_of(task_scope(task))
    settings = await _settings(ctx, contest)
    current = await published.task(ctx, task, settings) if settings is not None else None
    if settings is None or current is None:
        raise NotFound(UNREAD_TASK)
    if current.definition.scoring is None:
        raise NotFound(NOT_RUN_TASK)
    cause, against = RegradeCause(row.cause), PublicationId(row.publication_id)
    then = await _as_of(ctx, current, counted_under(cause, against, current.publications))
    now = await _as_of(ctx, current, against) or current
    mine = builtins.list(
        await ctx.db.scalars(select(Grading).where(Grading.regrade_id == regrade, FRONT))
    )
    attempts, ours, newest, gone = await _attempts(ctx, mine) if mine else ({}, {}, {}, frozenset())
    owners = {
        each.workspace_id: ctx.forge.workspaces.owner_of(WorkspaceId(each.workspace_id))
        for each in newest.values()
    }
    extensions = await timelines.of_owners(ctx, contest, set(owners.values()))
    earliers = {
        submission: gradings.latest_good(
            each
            for each in attempts[submission]
            if each.attempt < min(own.attempt for own in ours[submission])
        )
        for submission in newest
    }
    lates = await scores.lates_of(
        ctx,
        [
            *(each for each in newest.values() if gradings.good(each)),
            *(each for each in earliers.values() if each is not None),
        ],
    )
    marks = await entries.entered_for(ctx, task, newest)
    running = failed = unread = more = 0
    regrading: dict[WorkspaceOwner, builtins.list[Counted]] = {}
    changed: builtins.list[SubmissionChange] = []
    for submission, latest_row in sorted(
        newest.items(), key=lambda each: (each[1].submitted_at, each[0])
    ):
        status = gradings.status_of(ctx, latest_row, gone)
        late = lates.get(latest_row.id) if gradings.good(latest_row) else None
        if status in UNFINISHED or (
            late is not None and gradings.status_of(ctx, late) in UNFINISHED
        ):
            running += 1
            regrading.setdefault(owners[latest_row.workspace_id], []).append(
                Counted(current.name, latest_row.submission_number)
            )
            continue
        if not gradings.good(latest_row):
            failed += 1
            continue
        earlier = earliers[submission]
        if earlier is not None and then is None:
            # Its result counted under a publication that no longer reads,
            # so there is nothing to compare it with.
            unread += 1
            continue
        extension = extensions[owners[latest_row.workspace_id]]
        held = marks[submission]
        after = await scores.run_scored(ctx, now, settings, extension, latest_row, late, held)
        before = None
        if earlier is not None and then is not None:
            stood = await scores.run_scored(
                ctx, then, settings, extension, earlier, lates.get(earlier.id), held
            )
            before = stood.whole
        found = run_changes(before, after.whole)
        if not found:
            continue
        if len(changed) >= CHANGED_SHOWN:
            more += 1
            continue
        changed.append(
            SubmissionChange(
                grading=latest_row.id,
                submission=submission,
                number=latest_row.submission_number,
                workspace=WorkspaceId(latest_row.workspace_id),
                values=found.values,
                verdicts=found.verdicts,
                more_verdicts=found.more_verdicts,
            )
        )
    boards = (
        await _board_diffs(
            ctx,
            contest,
            settings,
            row,
            then,
            now,
            {owner: tuple(each) for owner, each in regrading.items()},
        )
        if holds(organiser.grants, contest_scope(contest), Role.MANAGER)
        else None
    )
    return RunSummary(
        id=row.id,
        task=task,
        cause=cause,
        publication=against,
        total=len(newest),
        running=running,
        failed=failed,
        unread=unread,
        changed=tuple(changed),
        more_changed=more,
        boards=boards,
    )


async def _as_of(
    ctx: Context, current: published.PublishedTask, publication: PublicationId
) -> published.PublishedTask | None:
    """The task as `publication` froze it, its settings and runs read at
    that version, or none when it is gone or its settings no longer read."""
    found = next((each for each in current.publications if each.id == publication), None)
    if found is None:
        return None
    if found.id == current.publication.id:
        return current
    held = await ctx.forge.content.read_file(PLATFORM, current.id, TASK_FILE, at=found.version)
    try:
        definition = parse_task(held.content)
    except InvalidDefinition:
        return None
    if definition.scoring is None:
        return None
    return replace(current, publication=found, definition=definition)


async def _board_diffs(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    regrade: Regrade,
    then: published.PublishedTask | None,
    now: published.PublishedTask,
    regrading: Mapping[WorkspaceOwner, tuple[Counted, ...]],
) -> tuple[BoardDiff, ...]:
    """Each board that reads the regrade's task, directly or through
    another board's column, worked out for staff before the regrade and
    after (`boards.regrade_sides`), in the file's order: `then` the task as
    it scored before, `now` as the regrade grades it, and `regrading` each
    row's submissions the regrade is still grading. When `then` no longer
    reads, each board is given `unread`, with no rows, as the summary
    counts its submissions (stage 5 section 6, item 71)."""
    checked = boards_of(settings)
    keys = [key for key in checked.reading(now.name) if key in checked.readable]
    if not keys:
        return ()
    if then is None:
        return tuple(
            BoardDiff(key, board.title_or(key), (), board.sort, (), unread=True)
            for key, board in settings.leaderboards.items()
            if key in keys
        )
    before_side, after_side = boards.regrade_sides(
        regrade.id, regrade.started_at, now.id, then, now
    )
    before = await boards.worked_out(ctx, contest, settings, keys, before_side)
    after = await boards.worked_out(ctx, contest, settings, keys, after_side)
    return tuple(board_diff(was, now_, regrading) for was, now_ in zip(before, after, strict=True))


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
        await ctx.db.scalars(select(Grading).where(Grading.regrade_id == regrade.id, FRONT))
    )
    if not mine:
        return []
    attempts, ours, newest, gone = await _attempts(ctx, mine)
    task = TaskId(regrade.task_id)
    current = await published.task(ctx, task, settings) if settings is not None else None
    scorer = (
        scores.Scorer(
            ctx,
            current,
            scores.Before(
                PublicationId(regrade.publication_id), max(row.queued_at for row in mine)
            ),
        )
        if current is not None
        else None
    )
    stood = (
        scores.Scorer(
            ctx,
            current,
            scores.Before(
                counted_under(
                    RegradeCause(regrade.cause),
                    PublicationId(regrade.publication_id),
                    current.publications,
                ),
                regrade.started_at,
                regrade.id,
            ),
        )
        if current is not None
        else None
    )
    factors = await _factors(ctx, current, settings, builtins.list(newest.values()))
    found: builtins.list[_Read] = []
    for submission, row in newest.items():
        first = min(each.attempt for each in ours[submission])
        earlier = gradings.latest_good(
            each for each in attempts[submission] if each.attempt < first
        )
        status = gradings.status_of(ctx, row, gone)
        before = await _side(stood, earlier, factors) if earlier is not None else None
        if status in UNFINISHED:
            compared = Compared(ChangeKind.RUNNING, before, None)
        elif status is GradingStatus.OVER_LIMIT:
            compared = Compared(ChangeKind.OVER_LIMIT, before, None)
        elif not gradings.good(row):
            compared = Compared(ChangeKind.FAILED, before, None)
        else:
            now = await _side(scorer, row, factors)
            compared = Compared(change_of(before, now), before, now)
        found.append(_Read(row, compared))
    return found


async def _attempts(
    ctx: Context, mine: Sequence[Grading]
) -> tuple[
    dict[str, builtins.list[Grading]],
    dict[str, builtins.list[Grading]],
    dict[str, Grading],
    frozenset[uuid.UUID],
]:
    """Of a regrade's rows `mine`: every attempt of each of their
    submissions, the regrade's own, its latest of each, and the rows the CI
    lost. A failure found among the latest that are their submission's
    latest starts its retry by itself.
    """
    attempts: dict[str, builtins.list[Grading]] = {}
    for row in await ctx.db.scalars(
        select(Grading).where(
            Grading.submission_id.in_(sorted({row.submission_id for row in mine})), FRONT
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
    return attempts, ours, newest, gone


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
