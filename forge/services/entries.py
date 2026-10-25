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

Until the grading queue decides when an entry may reach its entrant
(phase 5.8), a value reading an `entered` block is served to staff alone
(`run_results.for_entrants`, which `scores.run_scored` applies).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Final

from sqlalchemy import or_, select

from forge.db.tables import Entry, Grading
from forge.domain.blocks.catalogue import ENTERED
from forge.domain.blocks.evaluate import blocks_in
from forge.domain.blocks.numbers import exact, written
from forge.domain.definitions import TaskDefinition
from forge.domain.errors import InvalidMark, NotARunTask, NotFound
from forge.domain.ids import TaskId
from forge.domain.names import WorkspaceOwner
from forge.domain.roles import Role, contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import published
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
    rows = await ctx.db.execute(
        select(Entry.value, Entry.mark)
        .where(
            Entry.task_id == task,
            or_(Entry.submission_id == submission, Entry.submission_id.is_(None)),
        )
        .order_by(Entry.seq)
    )
    found: dict[str, Fraction] = {}
    for value, mark in rows:
        if mark is None:
            # A start: the value was given its `entered` block again.
            found.pop(value, None)
            continue
        number = _plain(mark)
        if number is None:
            raise ValueError(f"An entry for {value} holds {mark!r}, which is no mark.")
        found[value] = number
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


__all__ = ["Entered", "enter", "entered_of", "entered_values", "start_again"]
