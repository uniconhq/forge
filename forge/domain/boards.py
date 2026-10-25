"""A leaderboard as one viewer reads it (SIMPLER-CORE.md 10.2, the
leaderboards note's 5.1, 5.7 and 6.3): its columns' headers and its rows,
each row's cells worked out from its own results
(`forge.domain.blocks.board_evaluate`), hidden where the viewer may not see
what they read, ranked by the keys the viewer is shown, and cut to the
board's audience, never stored.

- **Who reads.** Staff, an observer of the contest or above, read every
  board, every row and every cell at once, with no hold. Anyone else reads
  a board whose `who` lets them (`sees`): `everyone`, anyone the contest
  shows itself to; `entrants`, the members of an approved entrant;
  `staff`, nobody else.
- **What a row holds for a viewer** (`results_seen`, `grading_seen`): each
  of its submissions' values and `compiled` as its own entrant is shown
  them now (`forge.domain.run_showing.shown`), since a value the task shows
  is no longer secret (rule 3), and nothing it does not show; a value
  listed now but not yet shown, its runs still running or faulted, waiting
  rather than hidden, so no viewer is told which rows still grade;
  `attempt`, `seconds`, `seconds_late` and `marked` with the row.
- **The hold** (`holding`, `held_back`): from its `from` until its `until`
  or an early end, every viewer but staff and the row's own entrant reads
  each list with only the submissions made before `from`, the rest
  counted as held, and a cell on such a row that reads `marked`, or a
  value reading an entered mark, is hidden: a pick there may take another
  submission than the entrant's own row does, and its mark would tell
  what a cell hidden from the entrant chose (stage 5 section 6, item 117).
- **A cell is hidden** when it read anything held back (guarantee 2), and
  when it reads another board's column, directly or through others, on a
  row that board does not serve the viewer (rule 5), so `rows: own` and
  staff's carry through. A hidden cell gives no number.
- **A column is not listed** for anyone but staff when it reads results
  and every task it reads, directly or through other columns, is withdrawn
  or not yet released (rule 8); a column listed names no self-reported
  value of such a task in its header.
- **Ranking.** Rows are ordered key by key, `desc` the highest first and
  `asc` the lowest, a missing key after every row with one; rows equal on
  every key share a place, 1, 1, 3 (guarantee 7), and are listed by name. A
  sort key hidden on a row served, or not listed, is dropped for that
  viewer: the rows are sorted by the rest and given no places (rule 4). A
  board with no `sort`, or one showing anyone but staff only their own row,
  gives no places, since a place among rows not served would tell of them.
- **The audience cut:** `rows: own` keeps the viewer's own row; a number
  keeps that many from the top, those tied with the last on the keys left,
  and the viewer's own.
- **A cell answered** is the column's number, missing as none, and, on
  the viewer's own row and for staff, whether it is hidden, the
  submissions it counted and those it left out as still waiting, each by
  task and number (guarantee 6), so on another row a cell with no number
  reads the same whether it is hidden, waiting or missing (item 127); a
  row answers how many of its submissions are held, and, told as that
  is, how many still grade (`still_grading`).
- **What the cells counted** of a task, row by row (`counted_in`), is
  what the grading queue lists, and says when a mark entered for a
  submission reaches its entrant (`entries_open`): only once a cell shown
  to them counted the submission, before the reveal or after it
  (SIMPLER-CORE.md 14), so a mark never tells which submission a hidden
  cell chose. The cells reading one task alone
  (`alone`) are those whose choices tell nothing of another task.
- **A header** says what a listed column is, the same for every row: its
  name, the decimal places it is shown to, the least and most it can give
  where both are known (stage 5 section 6, item 5), and the self-reported
  values it reads, each by task (item 7). For anyone but staff the bounds
  are known only from what the viewer is shown now (`seen_bounds`): a
  task released to them, its values `show` lists at a time that has come
  and the groups whose runs are all shown then, so "0 to 7" never tells
  the size of a hidden group (item 126).
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from fractions import Fraction

from forge.domain.blocks.board_check import BoardColumn, BoardsCheck
from forge.domain.blocks.board_evaluate import Cell, Submitted
from forge.domain.blocks.bounds import Bounds, value_bounds
from forge.domain.blocks.catalogue import ALL, SHOWN_FACTS
from forge.domain.blocks.evaluate import Column, RunResult
from forge.domain.definitions import BoardAudience, Hold, Leaderboard, Scoring
from forge.domain.names import WorkspaceOwner
from forge.domain.run_showing import ValueShown

MARKED = "marked"
"""The fact a hold hides on another row: a mark may change until the close."""


@dataclass(frozen=True, slots=True)
class Header:
    """A listed column: its name, its decimal places, none when the board
    gives none, its least and most, both none unless both are known, and
    each self-reported value it reads, by task."""

    name: str
    places: int | None = None
    at_least: Fraction | None = None
    at_most: Fraction | None = None
    self_reported: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Counted:
    """A submission a cell counted or left out, by its task and number."""

    task: str
    number: int


@dataclass(frozen=True, slots=True)
class BoardCell:
    """One listed column of a row: its number, none when missing or hidden;
    whether it is hidden from the viewer, told as its counts are, so a
    cell with no number on another row says no more; the submissions it
    counted; and those it left out as still waiting, both empty unless
    told."""

    column: str
    number: Fraction | None
    counted: tuple[Counted, ...] = ()
    waiting: tuple[Counted, ...] = ()
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One entrant's row: who, its name as shown, its place, none when the
    viewer is given none, its listed cells in order, how many of its
    submissions the hold keeps from the viewer, whether what each cell
    counted and left out is told, on the viewer's own row and to staff,
    and, told as that is, 0 otherwise, how many of its submissions to the
    tasks the board reads still grade."""

    owner: WorkspaceOwner
    name: str
    place: int | None
    cells: tuple[BoardCell, ...]
    held: int = 0
    told: bool = True
    grading: int = 0

    def cell(self, column: str) -> BoardCell | None:
        return next((each for each in self.cells if each.column == column), None)


@dataclass(frozen=True, slots=True)
class Board:
    """A leaderboard as one viewer reads it: its key, its title, who it is
    for, its listed columns' headers, the keys it was sorted by for them,
    the sort keys dropped for them, its rows in order, and the moment the
    hold holds other rows from, none when it holds nothing for them."""

    key: str
    title: str
    audience: BoardAudience
    headers: tuple[Header, ...]
    sort: tuple[tuple[str, str], ...]
    rows: tuple[BoardRow, ...]
    dropped: tuple[str, ...] = ()
    held_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who reads a board: staff, or anyone else, with the row of the
    approved entrant they are in, none when they are in none."""

    staff: bool = False
    own: WorkspaceOwner | None = None


STAFF = Viewer(staff=True)


@dataclass(frozen=True, slots=True)
class Loaded:
    """One submission of a row as a board loads it: when it was made, and
    its results as the viewer reads them before the hold."""

    made: datetime
    results: Submitted


@dataclass(frozen=True, slots=True)
class Row:
    """One entrant's row for a viewer: who, its name, every column of the
    boards read worked out for it, and how many of its submissions to each
    task the hold keeps from the viewer and how many still grade."""

    owner: WorkspaceOwner
    name: str
    cells: Mapping[BoardColumn, Cell]
    held: Mapping[str, int] = field(default_factory=dict)
    grading: Mapping[str, int] = field(default_factory=dict)


def sees(board: Leaderboard, viewer: Viewer) -> bool:
    """Whether the viewer reads the board at all (rule 9)."""
    who = board.audience.who
    return viewer.staff or who == "everyone" or (who == "entrants" and viewer.own is not None)


def holding(hold: Hold | None, now: datetime, ended: bool) -> datetime | None:
    """The moment the contest's hold holds other rows from at `now`: from
    its `from` until its `until`, a time or staff's end, `ended` saying
    staff ended it early; none when it holds nothing."""
    if hold is None or ended or now < hold.from_:
        return None
    if not isinstance(hold.until, str) and now >= hold.until:
        return None
    return hold.from_


def held_back(
    lists: Mapping[str, Sequence[Loaded]],
    since: datetime | None,
    entered: Mapping[str, Collection[str]],
) -> tuple[dict[str, list[Submitted]], dict[str, int]]:
    """A row's lists by task as a viewer the hold covers reads them: only
    the submissions made before `since`, `marked` and each task's values
    reading an entered mark, `entered`, held back on each, and how many
    each list holds back; every submission when `since` is none."""
    found: dict[str, list[Submitted]] = {}
    held: dict[str, int] = {}
    for task, listed in lists.items():
        if since is None:
            found[task] = [each.results for each in listed]
            continue
        kept = [each.results for each in listed if each.made < since]
        hide = {MARKED, *entered.get(task, ())}
        found[task] = [replace(each, hidden=each.hidden | hide) for each in kept]
        held[task] = len(listed) - len(kept)
    return found, held


def still_grading(lists: Mapping[str, Sequence[Submitted]]) -> dict[str, int]:
    """How many of a row's submissions to each task, as the viewer reads
    them, still wait on a value: still grading, or waiting for staff."""
    return {task: sum(1 for each in listed if each.waiting) for task, listed in lists.items()}


def listed_now(scoring: Scoring, times: Collection[str]) -> tuple[frozenset[str], bool]:
    """What `show` lists of a task at the `times` that have come, as anyone
    but staff may read it on a board: its values, one reading an entered
    mark among them, since a cell reads a mark only on a submission it
    counted (item 117), and whether it lists `compiled`, or a group, which
    shows `compiled` with its runs (items 82 and 124)."""
    listed = {name for time in times for name in scoring.show.lists()[time]}
    values = frozenset(name for name in scoring.values if name in listed)
    return values, any(
        name == ALL or name in scoring.groups or name in SHOWN_FACTS for name in listed
    )


def seen_bounds(
    scoring: Scoring,
    groups: Mapping[str, Sequence[RunResult]],
    columns: Mapping[str, Column],
    times: Collection[str],
) -> dict[str, Bounds]:
    """Each value of a task `show` lists at the `times` that have come,
    with its bounds as anyone but staff may know them (item 126): worked
    out over the values listed then and the groups, of `groups`, whose
    runs the groups listed then all show, so a value reading anything else
    has none, and a header never tells what a hidden group or value
    holds, such as how many runs a group has. `columns` are each report's
    column as the plan gives it."""
    listed = {name for time in times for name in scoring.show.lists()[time]}
    shown = {run.id for name in listed if name in groups for run in groups[name]}
    seen = {name: runs for name, runs in groups.items() if {run.id for run in runs} <= shown}
    values = {name: tree for name, tree in scoring.values.items() if name in listed}
    return value_bounds(values, seen, columns)


def counted_in(
    found: Sequence[Board], task: str, cells: Collection[BoardColumn] | None = None
) -> dict[WorkspaceOwner, dict[int, tuple[tuple[str, str], ...]]]:
    """Each row's submissions to `task` that a cell of the boards `found`
    counted, by number, each with the board and column of every cell that
    counted it, in the boards' order: what each cell is told counting, so
    a hidden cell, and a row whose counts are not told, count nothing;
    with `cells`, only those cells count."""
    counted: dict[WorkspaceOwner, dict[int, tuple[tuple[str, str], ...]]] = {}
    for board in found:
        for row in board.rows:
            for cell in row.cells:
                if cells is not None and (board.key, cell.column) not in cells:
                    continue
                for each in cell.counted:
                    if each.task != task:
                        continue
                    by = counted.setdefault(row.owner, {}).setdefault(each.number, ())
                    counted[row.owner][each.number] = (*by, (board.key, cell.column))
    return counted


def entries_open(number: int, shown: Collection[int]) -> bool:
    """Whether the marks entered for an entrant's submission `number` count
    in what they are served (SIMPLER-CORE.md 14), which is worked out as if
    none were entered until then: only once a cell shown to them counted
    it, one of `shown`, before the reveal or after it. A submission no
    shown cell counted never opens, since one opening when no cell counted
    it would leave the one a hidden cell chose as the one with no mark
    (stage 5 section 6, item 9)."""
    return number in shown


def alone(checked: BoardsCheck, task: str) -> frozenset[BoardColumn]:
    """The columns reading the results of `task` and of no other task,
    directly or through the columns they read: which of its submissions
    such a column counts is chosen by what `task` gives alone."""
    tasks, _ = _what_columns_read(checked)
    return frozenset(column for column, read in tasks.items() if read == {task})


def results_seen(
    number: int,
    own: Mapping[str, Fraction],
    values: Sequence[ValueShown],
    compiled: int | None,
    every: Collection[str],
    listed: Collection[str],
    compiled_listed: bool,
) -> Submitted:
    """A graded submission as anyone but staff reads it: `own` the facts
    shown with the row, `values` what its entrant is shown of the task's
    `every` value now, a value waiting for staff among them, and `compiled`
    none while not shown. A value `listed` now that is not shown waits on
    runs still running or faulted, so it is waiting, as `compiled` is while
    it or a group is listed, `compiled_listed`: none tells which rows still
    grade (item 97). Every other value is held back."""
    numbers = dict(own)
    waiting: set[str] = set()
    for value in values:
        if value.waiting:
            waiting.add(value.name)
        elif value.number is not None:
            numbers[value.name] = value.number
    shown = {value.name for value in values}
    waiting |= {name for name in every if name in listed and name not in shown}
    hidden = {name for name in every if name not in shown and name not in listed}
    if compiled is not None:
        numbers["compiled"] = Fraction(compiled)
    else:
        (waiting if compiled_listed else hidden).add("compiled")
    return Submitted(number, numbers, frozenset(waiting), frozenset(hidden))


def grading_seen(
    number: int,
    own: Mapping[str, Fraction],
    every: Collection[str],
    listed: Collection[str],
    compiled_listed: bool,
) -> Submitted:
    """A submission still grading as anyone but staff reads it: each value
    `listed` at a time that has come waiting, and `compiled` too when it
    or a group is, `compiled_listed`; every other value held back."""
    waiting = {name for name in every if name in listed}
    hidden = {name for name in every if name not in listed}
    (waiting if compiled_listed else hidden).add("compiled")
    return Submitted(number, dict(own), frozenset(waiting), frozenset(hidden))


def headers_of(
    key: str,
    board: Leaderboard,
    bounds: Mapping[tuple[str, str], Bounds],
    reported: Mapping[tuple[str, str], tuple[tuple[str, str], ...]],
) -> tuple[Header, ...]:
    """The headers of `board`'s displayed columns, in order."""
    found: list[Header] = []
    for name, places in board.display:
        held = bounds.get((key, name))
        least, most = held if held is not None else (None, None)
        found.append(Header(name, places, least, most, reported.get((key, name), ())))
    return tuple(found)


def read_boards(
    boards: Mapping[str, Leaderboard],
    checked: BoardsCheck,
    wanted: Sequence[str],
    rows: Sequence[Row],
    viewer: Viewer,
    *,
    released: Collection[str],
    headers: Mapping[str, tuple[Header, ...]],
    held_from: datetime | None = None,
) -> tuple[Board, ...]:
    """The boards `wanted` as `viewer` reads them, in that order: `rows`
    every row with every column the check orders worked out for this
    viewer, `released` the tasks released and not withdrawn, `headers` each
    board's displayed columns', and `held_from` when the hold holds other
    rows from. A board the viewer does not see is left out. The boards a
    wanted one reads are read first, so what each serves the viewer bounds
    the columns reading it (rule 5)."""
    tasks, others = _what_columns_read(checked)
    served: dict[str, frozenset[WorkspaceOwner]] = {}
    found: dict[str, Board] = {}
    for key in _board_order(checked):
        board = boards[key]
        if not sees(board, viewer):
            served[key] = frozenset()
            continue
        read = _Read(key, board, viewer, tasks, others, served, released)
        found[key] = read.read(rows, headers.get(key, ()), held_from)
        served[key] = frozenset(row.owner for row in found[key].rows)
    return tuple(found[key] for key in wanted if key in found)


class _Read:
    """One board read by one viewer."""

    def __init__(
        self,
        key: str,
        board: Leaderboard,
        viewer: Viewer,
        tasks: Mapping[BoardColumn, frozenset[str]],
        others: Mapping[BoardColumn, frozenset[str]],
        served: Mapping[str, frozenset[WorkspaceOwner]],
        released: Collection[str],
    ) -> None:
        self.key = key
        self.board = board
        self.viewer = viewer
        self.others = others
        self.served = served
        self.released = released
        self.listed = [
            name
            for name in board.shown
            if viewer.staff
            or not tasks.get((key, name))
            or any(task in released for task in tasks[(key, name)])
        ]
        self.tasks = frozenset().union(*(tasks.get((key, name), ()) for name in board.columns))

    def hidden(self, row: Row, name: str) -> bool:
        """Whether the column `name` is hidden on `row` for the viewer."""
        if self.viewer.staff:
            return False
        if row.cells[(self.key, name)].hidden:
            return True
        return any(
            row.owner not in self.served.get(other, frozenset())
            for other in self.others.get((self.key, name), ())
        )

    def read(
        self, rows: Sequence[Row], headers: tuple[Header, ...], held_from: datetime | None
    ) -> Board:
        board, viewer = self.board, self.viewer
        own = board.audience.rows == "own" and not viewer.staff
        candidates = [row for row in rows if not own or row.owner == viewer.own]
        keys = tuple(
            (name, way)
            for name, way in board.sort
            if name in self.listed and not any(self.hidden(row, name) for row in candidates)
        )
        dropped = tuple(name for name, way in board.sort if (name, way) not in keys)
        placed = bool(board.sort) and not dropped and not own

        def order(row: Row) -> tuple[object, ...]:
            found: list[object] = []
            for name, way in keys:
                number = row.cells[(self.key, name)].number
                if number is None:
                    found.append((1, Fraction(0)))
                else:
                    found.append((0, -number if way == "desc" else number))
            return tuple(found)

        listed = sorted(candidates, key=lambda row: (order(row), row.name, str(row.owner)))
        kept = listed if viewer.staff else self._cut(listed, order)
        places: dict[WorkspaceOwner, int] = {}
        last: tuple[object, ...] | None = None
        place = 0
        for index, row in enumerate(listed, start=1):
            current = order(row)
            if current != last:
                place, last = index, current
            places[row.owner] = place
        shown = tuple(self._row(row, places[row.owner] if placed else None) for row in kept)
        return Board(
            self.key,
            board.title_or(self.key),
            board.audience,
            tuple(self._header(header) for header in headers if header.name in self.listed),
            keys,
            shown,
            dropped,
            held_from if not viewer.staff else None,
        )

    def _header(self, header: Header) -> Header:
        """A listed column's header as the viewer reads it: for anyone but
        staff, naming no self-reported value of a task not released (rule
        8)."""
        if self.viewer.staff:
            return header
        reported = tuple(each for each in header.self_reported if each[0] in self.released)
        return replace(header, self_reported=reported)

    def _cut(self, listed: Sequence[Row], order: Callable[[Row], tuple[object, ...]]) -> list[Row]:
        """The rows the audience serves the viewer, in order: with a number,
        that many from the top and those tied with the last on the keys
        left, none tied when no key is left, and the viewer's own."""
        rows = self.board.audience.rows
        if not isinstance(rows, int):
            return list(listed)
        kept = list(listed[:rows])
        if kept and order(kept[-1]):
            last = order(kept[-1])
            kept += [row for row in listed[rows:] if order(row) == last]
        mine = next((row for row in listed if row.owner == self.viewer.own), None)
        if mine is not None and mine not in kept:
            kept.append(mine)
        return kept

    def _row(self, row: Row, place: int | None) -> BoardRow:
        told = self.viewer.staff or row.owner == self.viewer.own
        cells: list[BoardCell] = []
        for name in self.listed:
            cell = row.cells[(self.key, name)]
            if self.hidden(row, name):
                # Why a cell has no number is told only where its counts
                # are, so on another row hidden, waiting and missing read
                # alike (item 127).
                cells.append(BoardCell(name, None, hidden=told))
                continue
            cells.append(
                BoardCell(
                    name,
                    cell.number,
                    _counted(cell.counted) if told else (),
                    _counted(cell.waiting) if told else (),
                )
            )
        held = sum(count for task, count in row.held.items() if task in self.tasks)
        grading = sum(count for task, count in row.grading.items() if task in self.tasks)
        return BoardRow(
            row.owner, row.name, place, tuple(cells), held, told, grading if told else 0
        )


def _counted(found: Collection[tuple[str, int]]) -> tuple[Counted, ...]:
    return tuple(Counted(task, number) for task, number in sorted(found))


def _what_columns_read(
    checked: BoardsCheck,
) -> tuple[dict[BoardColumn, frozenset[str]], dict[BoardColumn, frozenset[str]]]:
    """For each column the check orders, every task whose results it reads
    and every other board whose column it reads, directly or through the
    columns it reads. The order has each column after those it reads, so
    one pass gathers both."""
    tasks: dict[BoardColumn, frozenset[str]] = {}
    others: dict[BoardColumn, frozenset[str]] = {}
    for board, name in checked.order:
        shape = checked.shapes[board][name]
        read_tasks = set(shape.tasks)
        read_boards: set[str] = set()
        for other in shape.columns:
            read_tasks |= tasks.get(other, frozenset())
            read_boards |= others.get(other, frozenset())
            read_boards.add(other[0])
        read_boards.discard(board)
        tasks[(board, name)] = frozenset(read_tasks)
        others[(board, name)] = frozenset(read_boards)
    return tasks, others


def boards_read(checked: BoardsCheck, wanted: Collection[str]) -> list[str]:
    """`wanted` and every board a column of theirs reads, directly or
    through other boards, in the file's order: each read whole, since what
    a board serves a viewer bounds a column reading it (rule 5)."""
    found = set(wanted)
    waiting = list(wanted)
    while waiting:
        board = waiting.pop()
        for shape in checked.shapes.get(board, {}).values():
            for other, _ in shape.columns:
                if other not in found:
                    found.add(other)
                    waiting.append(other)
    return [board for board in checked.shapes if board in found]


def _board_order(checked: BoardsCheck) -> list[str]:
    """The boards the check orders, each after the boards its columns read,
    those reading each other last in the file's order: a column reading a
    board not read yet is hidden from anyone but staff, which errs towards
    hiding."""
    boards = list(dict.fromkeys(board for board, _ in checked.order))
    reads = {
        board: {
            other
            for name in checked.shapes[board]
            for other, _ in checked.shapes[board][name].columns
            if other != board
        }
        for board in boards
    }
    order: list[str] = []
    waiting = list(boards)
    while waiting:
        ready = [board for board in waiting if reads[board] <= set(order)]
        if not ready:
            return order + waiting
        order += ready
        waiting = [board for board in waiting if board not in order]
    return order


__all__ = [
    "MARKED",
    "STAFF",
    "Board",
    "BoardCell",
    "BoardRow",
    "Counted",
    "Header",
    "Loaded",
    "Row",
    "Viewer",
    "alone",
    "boards_read",
    "counted_in",
    "entries_open",
    "grading_seen",
    "headers_of",
    "held_back",
    "holding",
    "listed_now",
    "read_boards",
    "results_seen",
    "seen_bounds",
    "sees",
    "still_grading",
]
