"""A regrade of many and what it changed (TASK-FORMAT.md section 3.5).

A rejudge of a task and the regrade every submission to it gets when a save
publishes a change to how it grades are each one regrade, by what started
it (`RegradeCause`). Each submission's latest attempt of it is set beside
the result that counted for that submission when the regrade began, the
latest earlier attempt that finished with one, and where it stands is one
`ChangeKind`:

- `regraded`, it finished with a result and had one before, whatever its
  values did, which `run_changes` says;
- `new`, it finished with a result and had none to compare with;
- `running`, still being graded; `failed`, no result yet, its latest
  attempt a system error or cancelled; and `over_limit`, ended without
  being graded since its entrant had no room left under `submissions.max`.

A regrade's summary counts its submissions by change, `graded` being those
that finished with a result.

What a regrade changed is summed up by what it changed, not by a direction
(SIMPLER-CORE.md 11.8, stage 5 section 6 item 4): each value of a
submission whose number changed, old to new, and each run whose verdict
changed, so a submission that now fails to compile is told
(`run_changes`). Its values have no direction (#490): up and down are a
row's place on a leaderboard, which the leaderboard's sort says, and each leaderboard that
reads the task gives its rows before and after (`LeaderboardDiff`, from phase
5.5).
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from forge.domain.blocks.evaluate import RunResult
from forge.domain.ids import PublicationId
from forge.domain.leaderboards import Counted, Header, LeaderboardView
from forge.domain.names import WorkspaceOwner
from forge.domain.publication_record import Publication
from forge.domain.run_results import Worked


class RegradeCause(StrEnum):
    """What started a regrade of many: staff's `rejudge` of the task, or the
    `save` that published a change to how it grades.
    """

    REJUDGE = "rejudge"
    SAVE = "save"


class ChangeKind(StrEnum):
    """What a regrade did to one submission, as the module says."""

    REGRADED = "regraded"
    NEW = "new"
    RUNNING = "running"
    FAILED = "failed"
    OVER_LIMIT = "over_limit"


GRADED = (ChangeKind.REGRADED, ChangeKind.NEW)
"""The changes of a submission whose regrade finished with a result."""


@dataclass(frozen=True, slots=True)
class Compared:
    """What a regrade did to one submission: the change, the attempt whose
    result it replaced, none when there was none, and its own attempt, none
    while it has no result.
    """

    change: ChangeKind
    earlier: int | None
    now: int | None


def counted_under(
    cause: RegradeCause, against: PublicationId, publications: Sequence[Publication]
) -> PublicationId:
    """The publication that was the task's latest just before a regrade
    against `against` began, of the task's `publications`: `against` itself
    for a rejudge, which publishes nothing; for a save's regrade, the latest
    publication before the one the save made, or `against` when there is
    none.
    """
    if cause is RegradeCause.REJUDGE:
        return against
    own = next((each for each in publications if each.id == against), None)
    before = [each for each in publications if own is not None and each.number < own.number]
    return max(before, key=lambda each: each.number).id if before else against


def change_of(earlier: int | None) -> ChangeKind:
    """Where a regrade's finished result stands beside `earlier`, the
    attempt whose result it replaced: `new` when there is none. Only a
    finished result is asked about here.
    """
    return ChangeKind.NEW if earlier is None else ChangeKind.REGRADED


def counted(changes: Iterable[ChangeKind]) -> dict[ChangeKind, int]:
    """How many submissions each change counts, every change named, 0 where
    none.
    """
    found = Counter(changes)
    return {change: found[change] for change in ChangeKind}


@dataclass(frozen=True, slots=True)
class ValueChange:
    """One value of a task's submission that a regrade changed: its
    name, its number before and after, none where it had none or waits,
    and whether it waits after, for a run or for staff."""

    name: str
    before: Fraction | None
    after: Fraction | None
    waiting: bool = False
    text_before: str | None = None
    text_after: str | None = None


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
    still waiting, as the live leaderboard says."""

    column: str
    before: Fraction | None
    after: Fraction | None
    waiting: tuple[Counted, ...] = ()
    text_before: str | None = None
    text_after: str | None = None

    @property
    def changed(self) -> bool:
        return self.before != self.after or self.text_before != self.text_after


@dataclass(frozen=True, slots=True)
class RowDiff:
    """One row of a leaderboard before the regrade and after: who, its name, its
    place on each side, none on a leaderboard with no `sort`, its displayed
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
class LeaderboardDiff:
    """One leaderboard that reads a regraded task, as staff read it, every row
    and cell and no hold (stage 5 section 6, items 4 and 71): its key and
    title, its displayed columns' headers as they stand after, the keys it
    sorts by, and each row in its order after the regrade, with its place
    and cells before and after; or, `unread`, no headers and no rows,
    since the publication the results counted under before no longer
    reads, as the summary counts those submissions `unread`."""

    leaderboard: str
    title: str
    headers: tuple[Header, ...]
    sort: tuple[tuple[str, str], ...]
    rows: tuple[RowDiff, ...]
    unread: bool = False


def leaderboard_diff(
    before: LeaderboardView,
    after: LeaderboardView,
    regrading: Mapping[WorkspaceOwner, tuple[Counted, ...]],
) -> LeaderboardDiff:
    """A leaderboard worked out before a regrade and after, row by row,
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
                old.text if then is not None and (old := then.cell(cell.column)) else None,
                cell.text,
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
    return LeaderboardDiff(after.key, after.title, after.headers, after.sort, tuple(rows))


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
        old_text = was.text if was is not None else None
        new_text = now.text if now is not None else None
        waits = now is not None and now.waiting
        if old != new or old_text != new_text or (was is not None and was.waiting) != waits:
            values.append(ValueChange(name, old, new, waits, old_text, new_text))
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
