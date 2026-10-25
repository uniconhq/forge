import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence

from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.gradings import EARLY, FRONT
from forge.domain.grading import UNFINISHED, GradingStatus, Place, room
from forge.domain.ids import TaskId
from forge.runtime.context import Context
from forge.services import entrant_view
from forge.services.gradings.row_state import latest_with_result, status_of


def place_of(ctx: Context, attempts: Sequence[Grading], lost: Collection[uuid.UUID] = ()) -> Place:
    """Where one submission stands against its task's `submissions.max`, by
    its attempts: it holds a place once one finished with a result, and
    while its latest is still being graded; it is delayed while it has no
    result and its latest reads as a system error; and it holds none once
    that is cancelled or over the limit.
    """
    if latest_with_result(attempts) is not None:
        return Place.HOLDS
    status = status_of(ctx, max(attempts, key=lambda row: row.attempt), lost)
    if status in UNFINISHED or status is GradingStatus.DONE:
        return Place.HOLDS
    if status is GradingStatus.SYSTEM_ERROR:
        return Place.DELAYED
    return Place.NONE


def waits_for_room(
    ctx: Context, attempts: Sequence[Grading], lost: Collection[uuid.UUID] = ()
) -> bool:
    """Whether a submission is graded again only while its entrant has room
    under the task's `submissions.max`: none of its attempts finished with a
    result, and its latest reads as a system error, delayed, or ended over
    the limit.
    """
    if latest_with_result(attempts) is not None:
        return False
    latest = max(attempts, key=lambda row: row.attempt)
    return status_of(ctx, latest, lost) in (GradingStatus.SYSTEM_ERROR, GradingStatus.OVER_LIMIT)


async def task_limit(ctx: Context, task: TaskId) -> int | None:
    """The task's `submissions.max` as its latest publication says, none
    while it has none.
    """
    found = await entrant_view.task(ctx, task)
    return found.definition.submissions.max if found is not None else None


async def _places(
    ctx: Context, workspace: str, task: str, lost: Collection[uuid.UUID] = ()
) -> dict[int, Place]:
    """Where each of the workspace's submissions to the task stands against
    its `submissions.max` (`place_of`), by number.
    """
    grouped: dict[int, list[Grading]] = {}
    for row in await ctx.db.scalars(
        select(Grading).where(Grading.workspace_id == workspace, Grading.task_id == task, FRONT)
    ):
        grouped.setdefault(row.submission_number, []).append(row)
    return {number: place_of(ctx, attempts, lost) for number, attempts in grouped.items()}


async def _over_limit(
    ctx: Context, row: Grading, attempts: Sequence[Grading], lost: Collection[uuid.UUID]
) -> int | None:
    """The task's `submissions.max` when grading `row`'s submission again now,
    `attempts` being all of its attempts, would end it over the limit: it
    waits for room (`waits_for_room`) and its entrant has none left for it
    (`room`); none otherwise, and always for a run task's late row, which
    holds no place of its own. Read under the task's hold, so no submit or
    other retry takes a place meanwhile.
    """
    if row.pipeline != EARLY or not waits_for_room(ctx, attempts, lost):
        return None
    limit = await task_limit(ctx, TaskId(row.task_id))
    if limit is None:
        return None
    places = await _places(ctx, row.workspace_id, row.task_id, lost)
    number = row.submission_number
    return None if number in room(places, limit, [number]) else limit


async def retry_limits(
    ctx: Context, rows: Iterable[Grading], lost: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """For each of `rows`, latest attempts, whose retry now would end over
    the limit, the task's `submissions.max` it would end against: read
    without a hold, so it says what a retry would do as things stand.
    """
    waiting: dict[tuple[str, str], list[Grading]] = {}
    for row in rows:
        if row.pipeline == EARLY and GradingStatus(row.status) is not GradingStatus.DONE:
            waiting.setdefault((row.task_id, row.workspace_id), []).append(row)
    if not waiting:
        return {}
    found: dict[uuid.UUID, int] = {}
    limits: dict[str, int | None] = {}
    for (task, workspace), latest in waiting.items():
        attempts: dict[int, list[Grading]] = {}
        for row in await ctx.db.scalars(
            select(Grading).where(Grading.workspace_id == workspace, Grading.task_id == task, FRONT)
        ):
            attempts.setdefault(row.submission_number, []).append(row)
        asking = [
            row
            for row in latest
            if waits_for_room(ctx, attempts.get(row.submission_number, [row]), lost)
        ]
        if not asking:
            continue
        if task not in limits:
            limits[task] = await task_limit(ctx, TaskId(task))
        limit = limits[task]
        if limit is None:
            continue
        places = {number: place_of(ctx, each, lost) for number, each in attempts.items()}
        for row in asking:
            if row.submission_number not in room(places, limit, [row.submission_number]):
                found[row.id] = limit
    return found


def _without_room(
    ctx: Context,
    regraded: Collection[str],
    attempts: Mapping[str, Sequence[Grading]],
    lost: Collection[uuid.UUID],
    limit: int,
) -> frozenset[str]:
    """Which of the `regraded` submissions, each with its `attempts`, wait
    for room under a `submissions.max` of `limit` (`waits_for_room`) and
    find none: each entrant's delayed and over-the-limit submissions are
    taken in the order they were made, after the places its others hold
    (`room`).
    """
    entrants: dict[str, dict[int, str]] = {}
    for submission, each in attempts.items():
        entrants.setdefault(each[0].workspace_id, {})[each[0].submission_number] = submission
    refused: set[str] = set()
    for numbered in entrants.values():
        asking = [
            number
            for number, submission in numbered.items()
            if submission in regraded and waits_for_room(ctx, attempts[submission], lost)
        ]
        if not asking:
            continue
        places = {
            number: place_of(ctx, attempts[submission], lost)
            for number, submission in numbered.items()
        }
        admitted = room(places, limit, asking)
        refused.update(numbered[number] for number in asking if number not in admitted)
    return frozenset(refused)
