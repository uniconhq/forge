"""The runs a draft's `repeat` makes, as the task's cards show them while the
organiser edits: every combination of the axes' tuples, the first axis
outermost, as the save makes them (`binder.make_runs`), each with the titles
of its tuples and the values the task gives. A tuple still being written is
read as far as it goes, and a value the entrant gives is left out.
"""

import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from forge.domain.definitions import ENTRANT
from forge.domain.run_task.records import MOST_RUNS


@dataclass(frozen=True, slots=True)
class DraftRun:
    titles: tuple[str, ...] = ()
    cells: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DraftRuns:
    """The runs, none when there would be more than a task may have, and
    their `count`; and each axis's own tuples as runs, an axis with no
    inputs having none."""

    runs: tuple[DraftRun, ...]
    count: int
    axes: tuple[tuple[DraftRun, ...], ...]


def draft_runs(repeat: object) -> DraftRuns:
    axes = [_axis(axis) for axis in repeat] if isinstance(repeat, list) else []
    used = [rows for rows, inputs, _ in axes if inputs]
    count = math.prod(size for _, inputs, size in axes if inputs)
    runs: list[DraftRun] = []
    if count <= MOST_RUNS:
        for combination in itertools.product(*used):
            cells: dict[str, object] = {}
            titles: list[str] = []
            for part in combination:
                cells.update(part.cells)
                titles += part.titles
            runs.append(DraftRun(tuple(titles), cells))
    return DraftRuns(tuple(runs), count, tuple(tuple(rows) for rows, _, _ in axes))


def _axis(axis: object) -> tuple[list[DraftRun], list[str], int]:
    """An axis's tuples as runs, at most as many as a task may have, its
    inputs and how many tuples it has."""
    if not isinstance(axis, Mapping):
        return [], [], 0
    inputs = [name for name in axis.get("inputs") or [] if isinstance(name, str)]
    tuples = axis.get("tuples")
    if not inputs or not isinstance(tuples, list):
        return [], inputs, 0
    return [_row(row, inputs) for row in tuples[: MOST_RUNS + 1]], inputs, len(tuples)


def _row(row: object, inputs: list[str]) -> DraftRun:
    cells: dict[str, object] = {}
    for column, name in enumerate(inputs):
        if isinstance(row, Mapping):
            value = row.get(name)
        elif isinstance(row, list):
            value = row[column] if column < len(row) else None
        else:
            value = row if len(inputs) == 1 else None
        if value is not None and value != ENTRANT:
            cells[name] = value
    title = row.get("title") if isinstance(row, Mapping) else None
    named = isinstance(title, str) and title.strip() != ""
    return DraftRun((title.strip(),) if named and isinstance(title, str) else (), cells)
