"""A regrade of many and what it changed (TASK-FORMAT.md section 3.5).

A rejudge of a task and the regrade every submission to it gets when a save
publishes a change to how it grades are each one regrade, by what started
it (`RegradeCause`). Each submission's latest attempt of it is compared with
the result that counted for that submission when the regrade began, the
latest earlier attempt that finished with one, both as organisers see
results: every group, nothing held back, the late factor applied
(`Side`). The earlier one is scored as it counted just before the regrade
began: with the scoring of the publication then the task's latest
(`counted_under`), and under relative credit against the best among the
results that counted then. What it changed is one `ChangeKind`:

- `up` and `down`, its points went up or down; on a task that gives no
  points, a larger or smaller share of its tests accepted, or with the same
  share, its values with a declared direction: down when any got worse, up
  when none did and one got better, so a regrade that makes anything worse
  is never read as an improvement; a value one side has and the other has
  not is the worst where it is missing, as a board reads it;
- `stopped`, it now stops before its tests where the result it replaced did
  not: in the classic workflow, it now fails to compile;
- `unchanged`, all of that the same;
- `new`, it had no result to compare with, or one of the two could not be
  scored, its publication no longer reading;
- `running`, still being graded; `failed`, no result yet, its latest
  attempt a system error or cancelled; and `over_limit`, ended without
  being graded since its entrant had no room left under `submissions.max`.

A regrade's summary counts its submissions by change, `graded` being those
that finished with a result.

A run task's regrade is summed up by what it changed, not by a direction
(SIMPLER-CORE.md 11.8, stage 5 section 6 item 4): each value of a
submission whose number changed, old to new, and each run whose verdict
changed, so a submission that now fails to compile is told
(`run_changes`). Its values have no direction (#490): up and down are a
row's place on a board, which the board's sort says, and each board that
reads the task gives its rows before and after (`BoardDiff`, from phase
5.5).
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction

from forge.domain.blocks.evaluate import RunResult
from forge.domain.blocks.numbers import Better
from forge.domain.boards import Board, Counted, Header
from forge.domain.ids import PublicationId
from forge.domain.names import WorkspaceOwner
from forge.domain.publications import Publication
from forge.domain.run_results import Worked


class RegradeCause(StrEnum):
    """What started a regrade of many: staff's `rejudge` of the task, or the
    `save` that published a change to how it grades.
    """

    REJUDGE = "rejudge"
    SAVE = "save"


class ChangeKind(StrEnum):
    """What a regrade did to one submission, as the module says."""

    UP = "up"
    DOWN = "down"
    STOPPED = "stopped"
    UNCHANGED = "unchanged"
    NEW = "new"
    RUNNING = "running"
    FAILED = "failed"
    OVER_LIMIT = "over_limit"


GRADED = (ChangeKind.UP, ChangeKind.DOWN, ChangeKind.STOPPED, ChangeKind.UNCHANGED, ChangeKind.NEW)
"""The changes of a submission whose regrade finished with a result."""


@dataclass(frozen=True, slots=True)
class Side:
    """One result a regrade compares, as organisers see it: its attempt,
    its points, none on a task that gives no points, how many of its tests
    were accepted of how many it has, the outcome that stopped its run
    before its tests, none when nothing did, each value with a fold and a
    declared direction, folded over every test, and that direction.
    """

    attempt: int
    points: Fraction | None
    accepted: int
    tests: int
    stopped: str | None = None
    values: Mapping[str, Fraction] = field(default_factory=dict)
    better: Mapping[str, Better] = field(default_factory=dict)

    @property
    def share(self) -> Fraction | None:
        """The part of its tests accepted, none with no tests."""
        return Fraction(self.accepted, self.tests) if self.tests else None


@dataclass(frozen=True, slots=True)
class Compared:
    """What a regrade did to one submission: the change, the result it
    replaced, none when there was none or it could not be scored, and its
    own, none while it has none or it could not be scored.
    """

    change: ChangeKind
    earlier: Side | None
    now: Side | None


def counted_under(
    cause: RegradeCause, against: PublicationId, publications: Sequence[Publication]
) -> PublicationId:
    """The publication that was the task's latest just before a regrade
    against `against` began, of the task's `publications`: `against` itself
    for a rejudge, which publishes nothing; for a save's regrade, the latest
    publication before the one the save made, or `against` when there is
    none. A result that counted then is scored as that publication scored it
    (`showing.under`).
    """
    if cause is RegradeCause.REJUDGE:
        return against
    own = next((each for each in publications if each.id == against), None)
    before = [each for each in publications if own is not None and each.number < own.number]
    return max(before, key=lambda each: each.number).id if before else against


def change_of(earlier: Side | None, now: Side | None) -> ChangeKind:
    """What a regrade's result `now` changed against `earlier`, the result it
    replaced; `new` when there is no earlier one, or either could not be
    scored (none). Only a finished result is asked about here.
    """
    if earlier is None or now is None:
        return ChangeKind.NEW
    if now.stopped is not None and earlier.stopped is None:
        return ChangeKind.STOPPED
    if now.points is not None and earlier.points is not None:
        return _by(now.points - earlier.points)
    if now.share is not None and earlier.share is not None and now.share != earlier.share:
        return _by(now.share - earlier.share)
    worse = better = False
    for name in now.values.keys() | earlier.values.keys():
        before, value = earlier.values.get(name), now.values.get(name)
        direction = now.better.get(name) or earlier.better.get(name)
        if direction is None or before == value:
            continue
        if before is None or value is None:
            # A missing value is the worst, as a board reads it.
            improved = before is None
        else:
            improved = value > before if direction is Better.HIGHER else value < before
        better |= improved
        worse |= not improved
    if worse:
        return ChangeKind.DOWN
    return ChangeKind.UP if better else ChangeKind.UNCHANGED


def _by(difference: Fraction) -> ChangeKind:
    if difference > 0:
        return ChangeKind.UP
    if difference < 0:
        return ChangeKind.DOWN
    return ChangeKind.UNCHANGED


def counted(changes: Iterable[ChangeKind]) -> dict[ChangeKind, int]:
    """How many submissions each change counts, every change named, 0 where
    none.
    """
    found = Counter(changes)
    return {change: found[change] for change in ChangeKind}


@dataclass(frozen=True, slots=True)
class ValueChange:
    """One value of a run task's submission that a regrade changed: its
    name, its number before and after, none where it had none or waits,
    and whether it waits after, for a run or for staff."""

    name: str
    before: Fraction | None
    after: Fraction | None
    waiting: bool = False


@dataclass(frozen=True, slots=True)
class VerdictChange:
    """One run whose verdict a regrade changed: its id and its verdict
    before and after, none where the grading did not hold the run; or,
    with no id, every one of the submission's `runs` runs, each of which
    changed from the same verdict to the same other one."""

    run: str | None
    before: str | None
    after: str | None
    runs: int = 1


VERDICTS_SHOWN = 20
"""The most runs a submission's change lists one by one; the rest are
counted."""


@dataclass(frozen=True, slots=True)
class CellDiff:
    """One displayed cell of a row, its number before the regrade and
    after, none for missing, and the submissions it left out after as
    still waiting, as the live board says."""

    column: str
    before: Fraction | None
    after: Fraction | None
    waiting: tuple[Counted, ...] = ()

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True, slots=True)
class RowDiff:
    """One row of a board before the regrade and after: who, its name, its
    place on each side, none on a board with no `sort`, its displayed
    cells, and its submissions the regrade is still grading, whose result
    from before it the after side reads meanwhile, so the row may still
    move."""

    owner: WorkspaceOwner
    name: str
    place_before: int | None
    place_after: int | None
    cells: tuple[CellDiff, ...]
    regrading: tuple[Counted, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardDiff:
    """One board that reads a regraded task, as staff read it, every row
    and cell and no hold (stage 5 section 6, items 4 and 71): its key and
    title, its displayed columns' headers as they stand after, the keys it
    sorts by, and each row in its order after the regrade, with its place
    and cells before and after; or, `unread`, no headers and no rows,
    since the publication the results counted under before no longer
    reads, as the summary counts those submissions `unread`."""

    board: str
    title: str
    headers: tuple[Header, ...]
    sort: tuple[tuple[str, str], ...]
    rows: tuple[RowDiff, ...]
    unread: bool = False


def board_diff(
    before: Board, after: Board, regrading: Mapping[WorkspaceOwner, tuple[Counted, ...]]
) -> BoardDiff:
    """A board worked out before a regrade and after, row by row,
    `regrading` each row's submissions the regrade is still grading."""
    was = {row.owner: row for row in before.rows}
    rows: list[RowDiff] = []
    for row in after.rows:
        then = was.get(row.owner)
        cells = tuple(
            CellDiff(
                cell.column,
                old.number if then is not None and (old := then.cell(cell.column)) else None,
                cell.number,
                cell.waiting,
            )
            for cell in row.cells
        )
        rows.append(
            RowDiff(
                row.owner,
                row.name,
                then.place if then is not None else None,
                row.place,
                cells,
                regrading.get(row.owner, ()),
            )
        )
    return BoardDiff(after.key, after.title, after.headers, after.sort, tuple(rows))


@dataclass(frozen=True, slots=True)
class RunChanges:
    """What a regrade changed of one submission: its values, its verdicts,
    at most `VERDICTS_SHOWN` of them, and how many more runs changed."""

    values: tuple[ValueChange, ...]
    verdicts: tuple[VerdictChange, ...]
    more_verdicts: int = 0

    def __bool__(self) -> bool:
        return bool(self.values or self.verdicts)


def run_changes(before: Worked | None, after: Worked) -> RunChanges:
    """What a regrade's grading `after` changed against `before`, the one
    that counted when the regrade began, none when none did, each as
    organisers read it: every value whose number moved, or that now waits
    or no longer does, in the order the task lists them, and every run
    whose verdict moved, by id, the first `VERDICTS_SHOWN` of them and the
    rest counted; one line for them all when every run changed from the
    same verdict to the same other one."""
    then = before.outcomes if before is not None else {}
    values: list[ValueChange] = []
    for name in [*after.outcomes, *(each for each in then if each not in after.outcomes)]:
        was, now = then.get(name), after.outcomes.get(name)
        old = was.number if was is not None else None
        new = now.number if now is not None else None
        waits = now is not None and now.waiting
        if old != new or (was is not None and was.waiting) != waits:
            values.append(ValueChange(name, old, new, waits))
    read_before = before.grading.read if before is not None else {}
    read_after = after.grading.read
    verdicts: list[VerdictChange] = []
    every = sorted(read_before.keys() | read_after.keys(), key=_run_order)
    for run in every:
        verdict = VerdictChange(run, _verdict(read_before, run), _verdict(read_after, run))
        if verdict.before != verdict.after:
            verdicts.append(verdict)
    pairs = {(each.before, each.after) for each in verdicts}
    if len(verdicts) > 1 and len(verdicts) == len(every) and len(pairs) == 1:
        [(first, last)] = pairs
        return RunChanges(tuple(values), (VerdictChange(None, first, last, len(verdicts)),))
    return RunChanges(
        tuple(values),
        tuple(verdicts[:VERDICTS_SHOWN]),
        max(0, len(verdicts) - VERDICTS_SHOWN),
    )


def _verdict(read: Mapping[str, RunResult], run: str) -> str | None:
    found = read.get(run)
    return found.verdict if found is not None else None


def _run_order(run: str) -> tuple[int, str]:
    """A run id by its number, r2 before r10."""
    digits = run.lstrip("r")
    return (int(digits), run) if digits.isdigit() else (0, run)
