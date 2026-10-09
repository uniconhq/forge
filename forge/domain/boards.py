"""A board, one ranking over a fixed scope, computed on read from the scored
gradings (TASK-FORMAT.md sections 1.1, 1.7 and 3.6), never stored, and the
same for every viewer: what differs between viewers is only which rows they
are given, and between moments only what is shown.

- **Scope**, from each group's declared `show`: `all`, every group; `live`,
  `always` and `verdict`; `after_close`, `after_close`. A cell counts, for
  points, the scope's groups whose verdict is shown, and for values the
  tests of the scope's groups whose tests are shown.
- **Which submission counts** (`select`): `best`, the candidate ranking the
  row highest under `order`, key by key, a missing value worse than any,
  the earliest of equals; `best_per_group`, each group's most points over
  the candidates, the earliest reaching it counted for that group;
  `marked`, the best of the marked candidates, or, with none, the one
  `best` picks over what the `live` scope showed before the reveal.
- **Keys** (`order`): `points`, higher is better; a value, its own
  direction; `penalty`, whole minutes from the contest's start to the
  counted submission (the latest of them under `best_per_group`), plus
  `per_attempt` for each earlier attempt that is not itself counted.
- **Rows**: a cell counts when its counted submission earned something on
  the first key, points above 0 or a value present; a row's number on a
  key is the sum over its counting cells, without a value when a counting
  cell lacks one, and with 0 points and nothing else when no cell counts.
  Rows are ordered key by key, a row without a value after every row with
  one; rows equal on every key share a rank (1, 2, 2, 4) and are listed by
  name. A cell's `attempts` is what `penalty` charges for, or every attempt
  when it does not count.

A task not yet released is not on the board's view; organisers' `final`
counts every task as revealed. A board whose scope shows nothing yet is
given as the time it is shown from. `rows` and the viewer's own row only
cut what is handed over (`for_viewer`).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum, StrEnum
from fractions import Fraction

from forge.domain.definitions import Leaderboard, Over, Penalty, Select, Show, Who
from forge.domain.names import WorkspaceOwner
from forge.domain.scoring import BEFORE_REVEAL, EVERYTHING, ZERO, Better, Scored, Seen

POINTS = "points"
PENALTY = "penalty"


class State(StrEnum):
    """Where one submission stands on a board: still `grading` to its row
    (a run in `system_error` among them); `void`, no attempt, cancelled or
    stopped by a step that is not sealed; an `attempt` a sealed step
    stopped; or a `candidate`, an attempt whose run was not stopped.
    """

    GRADING = "grading"
    VOID = "void"
    ATTEMPT = "attempt"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class Entry:
    """One of a row's submissions to a task: its number, when it was taken,
    where it stands, its scored grading when it is a candidate, and whether
    the row marked it.
    """

    number: int
    at: datetime
    state: State
    scored: Scored | None = None
    marked: bool = False


@dataclass(frozen=True, slots=True)
class Column:
    """A covered task: its name, label, worth, every group of its latest
    publication with its declared `show`, when it is released and reveals,
    and whether those have passed.
    """

    name: str
    label: str
    worth: Fraction | None
    shows: Mapping[str, Show]
    release_at: datetime
    released: bool
    reveal_at: datetime
    revealed: bool


@dataclass(frozen=True, slots=True)
class Row:
    """A contestant or a team, by whose workspace it is, its name, and its
    submissions to each covered task, by task name.
    """

    owner: WorkspaceOwner
    name: str
    entries: Mapping[str, tuple[Entry, ...]]


@dataclass(frozen=True, slots=True)
class Key:
    """One key rows are compared on: what it ranks by, which way is better,
    none for a value no covered task gives a direction, and the minutes a
    `penalty` charges per attempt.
    """

    by: str
    better: Better | None
    per_attempt: int | None = None


@dataclass(frozen=True, slots=True)
class Cell:
    """A row's cell on a task: whether it counts, its number on each key it
    has one for, while it counts, its attempts, and, given only in the
    viewer's own row, how many of its submissions are still grading and
    which it counts.
    """

    counting: bool
    numbers: Mapping[str, Fraction]
    attempts: int
    grading: int | None
    submissions: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class Ranked:
    """A row on the board: its rank, whose it is, its name, its number on
    each key, none where it has none, and its cell on each task.
    """

    rank: int
    owner: WorkspaceOwner
    name: str
    keys: tuple[Fraction | None, ...]
    cells: Mapping[str, Cell]


@dataclass(frozen=True, slots=True)
class NotInView:
    """What of a task is in the board's scope and not yet in its numbers:
    the groups whose verdict is not shown, and, on a board ranking a value,
    the `verdict` groups whose tests are not shown, with when they join.
    """

    task: str
    groups: tuple[str, ...]
    tests_of: tuple[str, ...]
    shown_at: datetime


@dataclass(frozen=True, slots=True)
class Standings:
    """A board at a moment: its keys, the tasks on its view, what is not in
    view yet, its rows in rank order, and, when its scope shows nothing yet,
    the time it is shown from, with no rows.
    """

    board: Leaderboard
    keys: tuple[Key, ...]
    tasks: tuple[Column, ...]
    not_in_view: tuple[NotInView, ...]
    rows: tuple[Ranked, ...]
    shown_at: datetime | None = None
    nothing_shown: bool = False


class Audience(IntEnum):
    """Who is reading a board, among those who may see the contest."""

    EVERYONE = 0
    CONTESTANTS = 1
    ORGANISERS = 2


LEAST = {Who.EVERYONE: Audience.EVERYONE, Who.CONTESTANTS: Audience.CONTESTANTS}


def sees(board: Leaderboard, audience: Audience) -> bool:
    """Whether `audience` sees the board: a higher one sees every board a
    lower one sees.
    """
    return audience >= LEAST.get(board.who, Audience.ORGANISERS)


def in_scope(over: Over) -> Callable[[Show], bool]:
    """Which declared `show` a board over `over` reads."""
    if over is Over.LIVE:
        return lambda show: show is not Show.AFTER_CLOSE
    if over is Over.AFTER_CLOSE:
        return lambda show: show is Show.AFTER_CLOSE
    return lambda show: True


def keys_of(board: Leaderboard, directions: Mapping[str, Better | None]) -> tuple[Key, ...]:
    """The board's keys, each value's direction as its covered tasks give it."""
    found: list[Key] = []
    for key in board.order:
        if isinstance(key, Penalty):
            found.append(Key(PENALTY, Better.LOWER, key.per_attempt))
        elif key == PENALTY:
            found.append(Key(PENALTY, Better.LOWER, 0))
        elif key == POINTS:
            found.append(Key(POINTS, Better.HIGHER))
        else:
            found.append(Key(key, directions.get(key)))
    return tuple(found)


def ranks_a_value(keys: Sequence[Key]) -> bool:
    return any(key.by not in (POINTS, PENALTY) for key in keys)


def rank(
    board: Leaderboard,
    keys: tuple[Key, ...],
    start: datetime,
    columns: Sequence[Column],
    rows: Sequence[Row],
    *,
    final: bool,
) -> Standings:
    """The board as its audience sees it now, or, `final`, as it will read
    once every task has revealed, with every row.
    """
    view = [column for column in columns if final or column.released]
    seen = {column.name: EVERYTHING if final else Seen(column.revealed) for column in view}
    scope = in_scope(board.over)
    first = keys[0]
    if not final and not any(
        _shows_some(column, scope, seen[column.name], first) for column in view
    ):
        return Standings(
            board,
            keys,
            tuple(view),
            (),
            (),
            shown_at=_first_shown(view, scope, first),
            nothing_shown=True,
        )
    not_in_view = () if final else _not_in_view(view, scope, seen, keys)
    scored = [
        (
            row,
            {
                column.name: _cell(
                    board, keys, start, scope, seen[column.name], row.entries.get(column.name, ())
                )
                for column in view
            },
        )
        for row in rows
    ]
    totals = [(row, cells, _totals(keys, cells.values())) for row, cells in scored]
    totals.sort(key=lambda found: (_order(keys, found[2]), found[0].name.casefold(), found[0].name))
    ranked: list[Ranked] = []
    for index, (row, cells, numbers) in enumerate(totals):
        tied = ranked and ranked[-1].keys == numbers
        ranked.append(
            Ranked(ranked[-1].rank if tied else index + 1, row.owner, row.name, numbers, cells)
        )
    return Standings(board, keys, tuple(view), not_in_view, tuple(ranked))


def for_viewer(
    standings: Standings, rows: str | int, own: WorkspaceOwner | None, *, every_row: bool = False
) -> Standings:
    """What one viewer is given of the board: the rows `rows` names, and the
    viewer's own row below them when it is not among them; which submission
    counts and how many are still grading only in their own row, or, for
    organisers (`every_row`), in every row and every row given.
    """
    if every_row:
        return standings
    listed = list(standings.rows)
    if rows == "own":
        kept = [row for row in listed if row.owner == own]
    elif isinstance(rows, int):
        cutoff = listed[rows - 1].rank if len(listed) >= rows else None
        kept = [row for row in listed if cutoff is None or row.rank <= cutoff]
        mine = next((row for row in listed if row.owner == own), None)
        if mine is not None and mine not in kept:
            kept.append(mine)
    else:
        kept = listed
    return replace(standings, rows=tuple(row if row.owner == own else _bare(row) for row in kept))


def _bare(row: Ranked) -> Ranked:
    return replace(
        row,
        cells={
            task: replace(cell, grading=None, submissions=None) for task, cell in row.cells.items()
        },
    )


def _shows_some(column: Column, scope: Callable[[Show], bool], seen: Seen, first: Key) -> bool:
    """Whether some group of the task in scope shows what the first key
    reads: its verdict for points, its tests for a value.
    """
    reads = seen.verdict if first.by == POINTS else seen.tests
    return any(scope(show) and reads(show) for show in column.shows.values())


def _first_shown(
    columns: Sequence[Column], scope: Callable[[Show], bool], first: Key
) -> datetime | None:
    """When the board's scope first shows something on the tasks of its
    view: a task's release, for a group shown before its reveal, otherwise
    its reveal; none while no covered task is released, since a task not
    yet released is not on the board, nor its times.
    """
    reads = BEFORE_REVEAL.verdict if first.by == POINTS else BEFORE_REVEAL.tests
    times = [
        column.release_at if reads(show) else column.reveal_at
        for column in columns
        for show in column.shows.values()
        if scope(show)
    ]
    return min(times) if times else None


def _not_in_view(
    view: Sequence[Column],
    scope: Callable[[Show], bool],
    seen: Mapping[str, Seen],
    keys: Sequence[Key],
) -> tuple[NotInView, ...]:
    found: list[NotInView] = []
    values = ranks_a_value(keys)
    for column in view:
        shown = seen[column.name]
        groups = tuple(
            name for name, show in column.shows.items() if scope(show) and not shown.verdict(show)
        )
        tests_of = tuple(
            name
            for name, show in column.shows.items()
            if values and scope(show) and shown.verdict(show) and not shown.tests(show)
        )
        if groups or tests_of:
            found.append(NotInView(column.name, groups, tests_of, column.reveal_at))
    return tuple(found)


def _minutes(start: datetime, at: datetime) -> Fraction:
    return Fraction(max(0, int((at - start).total_seconds() // 60)))


def _when(entry: Entry) -> tuple[datetime, int]:
    return entry.at, entry.number


def _order(
    keys: Sequence[Key], numbers: Sequence[Fraction | None]
) -> tuple[tuple[int, Fraction], ...]:
    """A sort key putting the better first on each key in turn, a missing
    number after every present one.
    """
    found: list[tuple[int, Fraction]] = []
    for key, number in zip(keys, numbers, strict=True):
        if number is None or key.better is None:
            found.append((1, ZERO))
        else:
            found.append((0, -number if key.better is Better.HIGHER else number))
    return tuple(found)


def _counts(first: Key, number: Fraction | None) -> bool:
    """Whether a cell earned something on the first key."""
    if number is None:
        return False
    return number > 0 if first.by == POINTS else True


def _cell(
    board: Leaderboard,
    keys: Sequence[Key],
    start: datetime,
    scope: Callable[[Show], bool],
    seen: Seen,
    entries: Sequence[Entry],
) -> Cell:
    ordered = sorted(entries, key=_when)
    attempts = [entry for entry in ordered if entry.state in (State.ATTEMPT, State.CANDIDATE)]
    candidates = [
        entry for entry in ordered if entry.state is State.CANDIDATE and entry.scored is not None
    ]
    grading = sum(1 for entry in ordered if entry.state is State.GRADING)
    if board.select is Select.BEST_PER_GROUP:
        return _per_group(keys, start, scope, seen, candidates, attempts, grading)

    def numbers(
        entry: Entry, reading: Callable[[Show], bool], at: Seen
    ) -> tuple[Fraction | None, ...]:
        assert entry.scored is not None
        earlier = sum(1 for attempt in attempts if _when(attempt) < _when(entry))
        found: list[Fraction | None] = []
        for key in keys:
            if key.by == POINTS:
                found.append(entry.scored.points(reading, at))
            elif key.by == PENALTY:
                found.append(_minutes(start, entry.at) + (key.per_attempt or 0) * earlier)
            else:
                found.append(entry.scored.value(key.by, reading, at))
        return tuple(found)

    def best(pool: Sequence[Entry], reading: Callable[[Show], bool], at: Seen) -> Entry | None:
        if not pool:
            return None
        return min(
            pool, key=lambda entry: (_order(keys, numbers(entry, reading, at)), _when(entry))
        )

    if board.select is Select.MARKED:
        marked = [entry for entry in candidates if entry.marked]
        picked = (
            best(marked, scope, seen)
            if marked
            else best(candidates, in_scope(Over.LIVE), BEFORE_REVEAL)
        )
    else:
        picked = best(candidates, scope, seen)
    if picked is None:
        return Cell(False, {}, len(attempts), grading, ())
    got = numbers(picked, scope, seen)
    if not _counts(keys[0], got[0]):
        return Cell(False, {}, len(attempts), grading, ())
    before = sum(1 for attempt in attempts if _when(attempt) < _when(picked))
    return Cell(
        True,
        {key.by: number for key, number in zip(keys, got, strict=True) if number is not None},
        before,
        grading,
        (picked.number,),
    )


def _per_group(
    keys: Sequence[Key],
    start: datetime,
    scope: Callable[[Show], bool],
    seen: Seen,
    candidates: Sequence[Entry],
    attempts: Sequence[Entry],
    grading: int,
) -> Cell:
    """`best_per_group`: each group's most points over the candidates, the
    earliest reaching it counted for that group.
    """
    best: dict[str, Fraction] = {}
    counted: dict[str, Entry] = {}
    gives = False
    for entry in candidates:
        assert entry.scored is not None
        gives = gives or entry.scored.gives_points
        for group in entry.scored.groups:
            if not (scope(group.show) and seen.verdict(group.show)) or group.points is None:
                continue
            if group.points > best.get(group.group, ZERO):
                best[group.group] = group.points
                counted[group.group] = entry
    points = sum(best.values(), ZERO) if gives else None
    if not _counts(keys[0], points):
        return Cell(False, {}, len(attempts), grading, ())
    assert points is not None
    chosen = sorted({entry.number: entry for entry in counted.values()}.values(), key=_when)
    latest = chosen[-1]
    numbers = {entry.number for entry in chosen}
    before = sum(
        1
        for attempt in attempts
        if _when(attempt) < _when(latest) and attempt.number not in numbers
    )
    found: dict[str, Fraction] = {}
    for key in keys:
        if key.by == POINTS:
            found[POINTS] = points
        elif key.by == PENALTY:
            found[PENALTY] = _minutes(start, latest.at) + (key.per_attempt or 0) * before
    return Cell(True, found, before, grading, tuple(entry.number for entry in chosen))


def _totals(keys: Sequence[Key], cells: Iterable[Cell]) -> tuple[Fraction | None, ...]:
    """A row's number on each key: the sum over its counting cells, none
    where a counting cell has none; 0 points and nothing else when no cell
    counts.
    """
    counting = [cell for cell in cells if cell.counting]
    found: list[Fraction | None] = []
    for key in keys:
        if not counting:
            found.append(ZERO if key.by == POINTS else None)
            continue
        present = [cell.numbers[key.by] for cell in counting if key.by in cell.numbers]
        found.append(sum(present, ZERO) if len(present) == len(counting) else None)
    return tuple(found)
