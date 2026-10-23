"""A leaderboard as it is read (SIMPLER-CORE.md 10, the leaderboards note's
5.5 and 5.7): its columns' headers and its rows, each row's cells worked
out from its own results (`forge.domain.blocks.board_evaluate`), ranked by
the board's `sort`, never stored.

- **A header** says what a displayed column is, the same for every row:
  its name, the decimal places it is shown to, the least and most it can
  give where both are known (stage 5 section 6, item 5), and the
  self-reported values it reads, each by task (item 7).
- **A cell** is the column's number, missing when it has none, with the
  submissions it counted and those it left out as still waiting, each by
  task and number.
- **Ranking.** Rows are ordered key by key, `desc` the highest first and
  `asc` the lowest, a missing key after every row with one; rows equal on
  every key share a place, 1, 1, 3 (guarantee 7), and are listed by name.
  A board with no `sort` gives no places.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from forge.domain.blocks.board_evaluate import Cell
from forge.domain.blocks.bounds import Bounds
from forge.domain.definitions import BoardAudience, Leaderboard
from forge.domain.names import WorkspaceOwner


@dataclass(frozen=True, slots=True)
class Header:
    """A displayed column: its name, its decimal places, none when the
    board gives none, its least and most, both none unless both are known,
    and each self-reported value it reads, by task."""

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
    """One displayed column of a row: its number, none when missing; the
    submissions it counted; and those it left out as still waiting."""

    column: str
    number: Fraction | None
    counted: tuple[Counted, ...] = ()
    waiting: tuple[Counted, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One entrant's row: who, its name as shown, its place, none on a
    board with no `sort`, and its displayed cells in order."""

    owner: WorkspaceOwner
    name: str
    place: int | None
    cells: tuple[BoardCell, ...]

    def cell(self, column: str) -> BoardCell | None:
        return next((each for each in self.cells if each.column == column), None)


@dataclass(frozen=True, slots=True)
class Board:
    """A leaderboard as read: its key, its title, who it is for, its
    displayed columns' headers, the keys it sorts by, and its rows in
    order."""

    key: str
    title: str
    audience: BoardAudience
    headers: tuple[Header, ...]
    sort: tuple[tuple[str, str], ...]
    rows: tuple[BoardRow, ...]


@dataclass(frozen=True, slots=True)
class Unranked:
    """One row before ranking: who, its name, and each of the board's
    columns as worked out for it."""

    owner: WorkspaceOwner
    name: str
    cells: Mapping[str, Cell]


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


def ranked(
    key: str, board: Leaderboard, headers: tuple[Header, ...], rows: Sequence[Unranked]
) -> Board:
    """`board` with `rows` in order, each with its place and its displayed
    cells."""
    keys = board.sort

    def order(row: Unranked) -> tuple[object, ...]:
        found: list[object] = []
        for name, way in keys:
            number = row.cells[name].number
            if number is None:
                found.append((1, Fraction(0)))
            else:
                found.append((0, -number if way == "desc" else number))
        return tuple(found)

    listed = sorted(rows, key=lambda row: (order(row), row.name, str(row.owner)))
    shown: list[BoardRow] = []
    place = 0
    last: tuple[object, ...] | None = None
    for index, row in enumerate(listed, start=1):
        current = order(row)
        if current != last:
            place, last = index, current
        cells = tuple(_cell(name, row.cells[name]) for name, _ in board.display)
        shown.append(BoardRow(row.owner, row.name, place if keys else None, cells))
    return Board(key, board.title_or(key), board.audience, headers, keys, tuple(shown))


def _cell(name: str, cell: Cell) -> BoardCell:
    return BoardCell(
        name,
        cell.number,
        tuple(Counted(task, number) for task, number in sorted(cell.counted)),
        tuple(Counted(task, number) for task, number in sorted(cell.waiting)),
    )


__all__ = [
    "Board",
    "BoardCell",
    "BoardRow",
    "Counted",
    "Header",
    "Unranked",
    "headers_of",
    "ranked",
]
