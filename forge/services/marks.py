"""The submissions a row marks for the `marked` boards (TASK-FORMAT.md
section 1.1, Marks): up to the task's `marks` of the row's own submissions,
graded or not, one set shared by every board and, for a team, by every
member. Marks change until the row's close on the task, its extension
included, and are frozen after it. Marking grades nothing; a board reads a
mark only on a candidate.

Marks of one row on one task change one after another, under a Postgres
advisory lock held until the unit of work ends, so a row never holds more
than it may. A mark already held is marked again without a change, and a
mark not held unmarked the same way.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, text

from forge.db.tables import Grading, Mark
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import MarkLimit, MarksFrozen, MarksOff, NotApproved, NotFound
from forge.domain.ids import TaskId, WorkspaceId
from forge.domain.registration import Status
from forge.domain.release import close_of
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import submitters
from forge.services.submitters import Entrant

log = get_logger(__name__)

MARKS_LOCK = 0x4D41524B
"""The first key of every lock on a row's marks on a task, the second being
the workspace and task hashed by Postgres."""
NO_SUCH_SUBMISSION = "There is no such submission."
NOT_MARKED = "The task takes no marks: no board with select: marked covers it."


@dataclass(frozen=True, slots=True)
class Marks:
    """The marks the row holds on a task: the numbers of its marked
    submissions, the most it may hold, and its close, after which they are
    frozen.
    """

    task: TaskId
    numbers: tuple[int, ...]
    most: int
    closes_at: datetime
    frozen: bool


@action
async def held(ctx: Context, session: Session, task: TaskId) -> Marks:
    """The marks the signed-in person's row holds on the task. `MarksOff`
    for a task no marked board covers, and `NotApproved` for a person who is
    no approved contestant.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace, most, closes_at = await _place(ctx, entrant)
    return await _marks(ctx, entrant, workspace, most, closes_at)


@action
async def mark(ctx: Context, session: Session, task: TaskId, number: int) -> Marks:
    """Mark one of the row's own submissions to the task. `NotFound` for a
    submission that is not the row's, `MarksFrozen` once the row's close
    has passed, and `MarkLimit` when the row already holds as many marks as
    it may.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace, most, closes_at = await _place(ctx, entrant)
    await _change(ctx, entrant, workspace, closes_at)
    if not await _submitted(ctx, workspace, task, number):
        raise NotFound(NO_SUCH_SUBMISSION)
    marked = await numbers_of(ctx, workspace, task)
    if number not in marked:
        if len(marked) >= most:
            raise MarkLimit(
                f"Your row holds {most} {'mark' if most == 1 else 'marks'} on this task, "
                "the most it may; unmark one first.",
                limit=most,
            )
        ctx.db.add(
            Mark(
                workspace_id=workspace,
                task_id=task,
                submission_number=number,
                marked_by=entrant.session.user_id,
            )
        )
        await ctx.db.flush()
        log.info("marks.marked", task=task, workspace=workspace, number=number)
    return await _marks(ctx, entrant, workspace, most, closes_at)


@action
async def unmark(ctx: Context, session: Session, task: TaskId, number: int) -> Marks:
    """Take a mark off one of the row's submissions to the task.
    `MarksFrozen` once the row's close has passed.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace, most, closes_at = await _place(ctx, entrant)
    await _change(ctx, entrant, workspace, closes_at)
    await ctx.db.execute(
        delete(Mark).where(
            Mark.workspace_id == workspace,
            Mark.task_id == task,
            Mark.submission_number == number,
        )
    )
    log.info("marks.unmarked", task=task, workspace=workspace, number=number)
    return await _marks(ctx, entrant, workspace, most, closes_at)


async def numbers_of(ctx: Context, workspace: WorkspaceId, task: TaskId) -> set[int]:
    """The numbers of the row's marked submissions to the task."""
    return set(
        (
            await ctx.db.scalars(
                select(Mark.submission_number).where(
                    Mark.workspace_id == workspace, Mark.task_id == task
                )
            )
        ).all()
    )


async def of_tasks(
    ctx: Context, tasks: Collection[TaskId]
) -> dict[tuple[WorkspaceId, TaskId], set[int]]:
    """Every row's marked submissions to each of `tasks`."""
    if not tasks:
        return {}
    found: dict[tuple[WorkspaceId, TaskId], set[int]] = {}
    rows = await ctx.db.execute(
        select(Mark.workspace_id, Mark.task_id, Mark.submission_number).where(
            Mark.task_id.in_(sorted(tasks))
        )
    )
    for workspace, task, number in rows:
        found.setdefault((WorkspaceId(workspace), TaskId(task)), set()).add(number)
    return found


async def most_held(ctx: Context, task: TaskId) -> tuple[int, int]:
    """The most marks any row holds on the task, and how many rows hold that
    many; `(0, 0)` when none holds any.
    """
    counts = (
        await ctx.db.execute(
            select(func.count()).where(Mark.task_id == task).group_by(Mark.workspace_id)
        )
    ).scalars()
    held = sorted(counts, reverse=True)
    if not held:
        return 0, 0
    return held[0], held.count(held[0])


async def _place(ctx: Context, entrant: Entrant) -> tuple[WorkspaceId, int, datetime]:
    """The row's workspace, how many marks the task takes, and the row's
    close on it.
    """
    settings: ContestDefinition = entrant.settings
    entry = settings.entry(entrant.published.name)
    most = settings.marks_of(entry) if entry is not None else None
    if entry is None or most is None:
        raise MarksOff(NOT_MARKED)
    row = entrant.row
    if row is None or row.status != Status.APPROVED or entrant.workspace is None:
        raise NotApproved("Only an approved contestant marks submissions.")
    extension = await submitters.extension(ctx, entrant)
    return entrant.workspace, most, close_of(settings, entry, extension)


async def _change(
    ctx: Context, entrant: Entrant, workspace: WorkspaceId, closes_at: datetime
) -> None:
    """Refuse a change after the row's close, and hold the row's marks on
    the task, and the person's team, until the unit of work ends.
    """
    if ctx.now >= closes_at:
        raise MarksFrozen("Your close on this task has passed, so your marks are frozen.")
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:target))"),
        {"space": MARKS_LOCK, "target": f"{workspace}|{entrant.task}"},
    )
    await submitters.hold_standing(ctx, entrant)


async def _submitted(ctx: Context, workspace: WorkspaceId, task: TaskId, number: int) -> bool:
    found = await ctx.db.scalar(
        select(Grading.id)
        .where(
            Grading.workspace_id == workspace,
            Grading.task_id == task,
            Grading.submission_number == number,
        )
        .limit(1)
    )
    return found is not None


async def _marks(
    ctx: Context, entrant: Entrant, workspace: WorkspaceId, most: int, closes_at: datetime
) -> Marks:
    numbers = await numbers_of(ctx, workspace, entrant.task)
    return Marks(entrant.task, tuple(sorted(numbers)), most, closes_at, ctx.now >= closes_at)
