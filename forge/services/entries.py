"""The marks staff enter for a submission's `entered` values
(SIMPLER-CORE.md 14.2, stage 5 section 6 items 8 and 9).

A value whose tree holds an `entered` block, one at most, is missing until
a mark is entered for it. Only a manager or admin of the task enters one,
a role at its contest or org included: an observer reads the task's
results and enters nothing. A mark is a number checked against the bounds
the live publication's `entered` block gives, exactly, and refused for a
value that publication gives no such block. Each entry is one `entries`
row, never changed or deleted, and the latest for a submission's value is
its mark (`entered_of`), so who changed a mark stays known. A regrade
never touches an entry: a mark is the submission's, not its grading's.
A save that gives a value an `entered` block the live version does not
starts it with no marks (`start_again`), so a value removed and added
again never takes back its old marks.

**The grading queue** (`queue`, #505) is a staff read for an observer of
the task and above, who read it and nothing more: entering a mark from it
stays a manager's or admin's. For each row once the task has closed for
it, its extension included, it lists the row's submissions to the task
that a cell of any leaderboard counted, as staff read every board, every
row and no hold (`boards.read`), each with the boards and columns that
counted it and the mark of each `entered` value, or none while missing.
It is worked out from what each cell counted on every read; no task
names a board. A row with nothing counted is left out. A caller with no
role at the contest, an observer of the task alone, is listed only the
cells reading this task and no other (`alone`), since which submission a
cell reading another task counted can tell of that task's values; and
the read starts no retry of a failure it finds, which only staff's board
read does (stage 5 section 6, items 100 and 119).

**When an entry shows.** A mark reaches an entrant only once a board
cell shown to them counted the submission, and `show` lists the value
then, before the reveal or after it; one no shown cell counted never
does (`forge.domain.boards.entries_open`, which the submission's page
reads). Until then the submission's values are worked out as if no mark
were entered, so it reads as one with none. So a mark appearing on one
submission, or held back from it, never tells its entrant which
submission a hidden cell chose. Staff read every entry at once. Entering
one nudges nobody, which would tell a mark was entered before it may
show.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Final

from sqlalchemy import or_, select

from forge.db.tables import Entry, Grading
from forge.db.tables.gradings import FRONT
from forge.domain.blocks.catalogue import ENTERED
from forge.domain.blocks.evaluate import blocks_in
from forge.domain.blocks.numbers import exact, written
from forge.domain.board_checks import boards_of
from forge.domain.boards import STAFF, alone, counted_in
from forge.domain.definitions import TaskDefinition
from forge.domain.errors import InvalidMark, NotARunTask, NotFound
from forge.domain.ids import TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner
from forge.domain.release import close_of
from forge.domain.roles import Role, contest_id_of, contest_scope, holds, task_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import boards, published, timelines
from forge.services.access import Organiser, require

log = get_logger(__name__)

NO_SUCH_SUBMISSION: Final = "There is no such submission."
NOT_A_RUN_TASK: Final = "Only a task that grades by runs has values a mark is entered for."
MOST_MARK_CHARACTERS: Final = 40
"""The longest mark's text, sign, digits and point together."""


@dataclass(frozen=True, slots=True)
class Entered:
    """One mark entered for a submission's value: the value, the mark as
    the plain decimal it was entered as, who entered it by user id, and
    when.
    """

    value: str
    mark: str
    by: int
    at: datetime


@dataclass(frozen=True, slots=True)
class Queued:
    """A submission a row's cells counted: its number, the board and column
    of each cell that counted it, and each `entered` value of the task with
    its mark, none while missing, in the task's order."""

    number: int
    counted_by: tuple[tuple[str, str], ...]
    marks: tuple[tuple[str, Fraction | None], ...]


@dataclass(frozen=True, slots=True)
class QueueRow:
    """One row in the grading queue: who, its name as shown, when the task
    closed for it, and the submissions its cells counted, oldest first."""

    owner: WorkspaceOwner
    name: str
    closed_at: datetime
    submissions: tuple[Queued, ...]


@action
async def queue(ctx: Context, organiser: Organiser, task: TaskId) -> tuple[QueueRow, ...]:
    """The grading queue of the task (#505): each row the task has closed
    for, its extension included, by name, with the submissions to it that
    a cell of any of the contest's boards counted, as staff read them, each
    with the marks of the task's `entered` values; for a caller with no
    role at the contest, only the cells reading this task alone. Needs the
    observer role at the task. `NotFound` for a task with no publication,
    `NotARunTask` for one graded test by test.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    contest = contest_id_of(task_scope(task))
    settings = await published.contest(ctx, contest)
    current = await published.task(ctx, task, settings)
    if current is None:
        raise NotFound(published.NO_SUCH_TASK)
    scoring = current.definition.scoring
    if scoring is None:
        raise NotARunTask(NOT_A_RUN_TASK)
    entry = current.entry
    found = await boards.read(ctx, contest, settings, list(settings.leaderboards), STAFF)
    cells = None
    if not holds(organiser.grants, contest_scope(contest), Role.OBSERVER):
        cells = alone(boards_of(settings), current.name)
    counted = counted_in(found, current.name, cells)
    if entry is None or not counted:
        return ()
    names = {row.owner: row.name for board in found for row in board.rows}
    extensions = await timelines.of_owners(ctx, contest, list(counted))
    closed = {owner: close_of(settings, entry, extensions[owner]) for owner in counted}
    rows = sorted(
        (owner for owner in counted if ctx.now >= closed[owner]),
        key=lambda owner: (names.get(owner, ""), str(owner)),
    )
    workspaces = {ctx.forge.workspaces.workspace_of(contest, owner): owner for owner in rows}
    ids: dict[tuple[WorkspaceOwner, int], str] = {}
    if workspaces:
        for workspace, number, submission in await ctx.db.execute(
            select(Grading.workspace_id, Grading.submission_number, Grading.submission_id)
            .where(Grading.task_id == task, Grading.workspace_id.in_(sorted(workspaces)), FRONT)
            .distinct()
        ):
            ids[(workspaces[WorkspaceId(workspace)], number)] = submission
    marks = await entered_for(ctx, task, ids.values())
    values = [name for name in scoring.values if name in entered_values(current.definition)]

    def queued(owner: WorkspaceOwner, number: int, by: tuple[tuple[str, str], ...]) -> Queued:
        held = marks.get(ids.get((owner, number), ""), {})
        return Queued(number, by, tuple((value, held.get(value)) for value in values))

    return tuple(
        QueueRow(
            owner,
            names.get(owner, ""),
            closed[owner],
            tuple(queued(owner, number, by) for number, by in sorted(counted[owner].items())),
        )
        for owner in rows
    )


@action
async def enter(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    owner: WorkspaceOwner,
    number: int,
    value: str,
    mark: str,
) -> Entered:
    """Enter `mark` for the value `value` of the submission numbered
    `number` of the entrant `owner`. Needs the manager role at the task.
    `InvalidMark` for a value the task's live publication gives no
    `entered` block, or a mark that is not a number within its bounds;
    `NotFound` for a task with no publication or a row with no such
    submission.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    settings = await published.contest(ctx, contest_id_of(task_scope(task)))
    current = await published.task(ctx, task, settings)
    if current is None:
        raise NotFound(NO_SUCH_SUBMISSION)
    scoring = current.definition.scoring
    if scoring is None:
        raise NotARunTask(NOT_A_RUN_TASK)
    tree = scoring.values.get(value)
    found = blocks_in(tree, ENTERED) if tree is not None else []
    if not found:
        raise InvalidMark(
            f"{value} takes no entered mark: the task's live version gives it no entered block."
        )
    number_of = _mark(mark)
    least, most = exact(found[0]["at_least"]), exact(found[0]["at_most"])
    assert least is not None and most is not None, "the save checks an entered block's bounds"
    if number_of is None or not least <= number_of <= most:
        raise InvalidMark(
            f"Enter a number from {written(least)} to {written(most)} for {value}, such as "
            f"{written(least)}."
        )
    workspace = ctx.forge.workspaces.workspace_of(contest_id_of(task_scope(task)), owner)
    submission = await ctx.db.scalar(
        select(Grading.submission_id)
        .where(
            Grading.workspace_id == workspace,
            Grading.task_id == task,
            Grading.submission_number == number,
        )
        .limit(1)
    )
    if submission is None:
        raise NotFound(NO_SUCH_SUBMISSION)
    entered = Entered(value, _stored(mark), organiser.user.id, ctx.now)
    ctx.db.add(
        Entry(
            task_id=task,
            submission_id=submission,
            workspace_id=workspace,
            submission_number=number,
            value=value,
            mark=entered.mark,
            entered_by=entered.by,
            entered_at=entered.at,
        )
    )
    await ctx.db.flush()
    log.info(
        "entries.entered",
        task=task,
        number=number,
        value=value,
        user_id=organiser.user.id,
    )
    return entered


async def entered_of(ctx: Context, task: TaskId, submission: str) -> dict[str, Fraction]:
    """The mark entered for each of the submission's values, the latest
    row by the order the rows were written (`entries.seq`), by the value's
    name; a value with none is left out.
    """
    return (await entered_for(ctx, task, [submission]))[submission]


async def entered_for(
    ctx: Context, task: TaskId, submissions: Iterable[str]
) -> dict[str, dict[str, Fraction]]:
    """`entered_of` for each of `submissions` at once, read in one go."""
    wanted = sorted(set(submissions))
    found: dict[str, dict[str, Fraction]] = {submission: {} for submission in wanted}
    if not wanted:
        return found
    rows = await ctx.db.execute(
        select(Entry.submission_id, Entry.value, Entry.mark)
        .where(
            Entry.task_id == task,
            or_(Entry.submission_id.in_(wanted), Entry.submission_id.is_(None)),
        )
        .order_by(Entry.seq)
    )
    for submission, value, mark in rows:
        marks = [found[submission]] if submission is not None else list(found.values())
        if mark is None:
            # A start: the value was given its `entered` block again.
            for each in marks:
                each.pop(value, None)
            continue
        number = _plain(mark)
        if number is None:
            raise ValueError(f"An entry for {value} holds {mark!r}, which is no mark.")
        for each in marks:
            each[value] = number
    return found


async def start_again(ctx: Context, task: TaskId, values: Iterable[str], by: int) -> None:
    """Start each of `values` with no marks, since the save by the user
    `by` gives it an `entered` block the task's live version does not:
    one row each with no submission and no mark, before which no entry
    of the value counts. Written before the version is published, so any
    mark entered under it comes after.
    """
    for value in sorted(set(values)):
        ctx.db.add(Entry(task_id=task, value=value, entered_by=by, entered_at=ctx.now))
    await ctx.db.flush()


def entered_values(definition: TaskDefinition | None) -> frozenset[str]:
    """The values of `definition` that hold an `entered` block."""
    scoring = definition.scoring if definition is not None else None
    if scoring is None:
        return frozenset()
    return frozenset(name for name, tree in scoring.values.items() if blocks_in(tree, ENTERED))


def _mark(text: str) -> Fraction | None:
    """The exact number a mark's text is, a plain decimal such as `7` or
    `-2.5`, or none for anything else: an exponent, a word, too long.
    """
    if not isinstance(text, str) or len(text) > MOST_MARK_CHARACTERS:
        return None
    return _plain(text)


def _plain(text: str) -> Fraction | None:
    """The exact number plain decimal text is, of any length, or none."""
    if not text or any(each not in "+-.0123456789" for each in text):
        return None
    try:
        return exact(Decimal(text))
    except InvalidOperation:
        return None


def _stored(text: str) -> str:
    """A mark's text as it is kept: the same number, exactly, with no
    sign but a minus, no leading or trailing zeros past the point, and
    `0` for zero; at most one character longer than what was entered.
    """
    number = Decimal(text)
    if number == 0:
        return "0"
    kept = format(number, "f")
    return kept.rstrip("0").rstrip(".") if "." in kept else kept


__all__ = [
    "Entered",
    "QueueRow",
    "Queued",
    "enter",
    "entered_for",
    "entered_of",
    "entered_values",
    "queue",
    "start_again",
]
