"""The most and the least a task's value or a board's column can give
(stage 5 section 6, item 5; the leaderboards note's 9.2), the same for
every row, and which self-reported values a column reads (item 7).

Bounds are carried where every part has one and are none otherwise:

- a number is its own; a run's report column its plan's `at_least` and
  `at_most`, a fold's `missing` number widening them; `passed` 0 to 1; a
  key's column the least and most its runs take; an `entered` block what
  it declares; `compiled` and `marked` 0 to 1; `attempt` 1 to the task's
  `submissions.max`;
- through arithmetic, the comparisons, `min`, `max`, `floor` and `ceil`;
  a division whose divisor's bounds hold 0 has none;
- a fold over a group by its number of runs and their weights, and a fold
  over a task's results by the task's `submissions.max`, none without
  one, a `missing` number widening them; a `where`, or a `missing: skip`,
  may leave out all but one;
- a pick's value is the value's bounds, `best_each` its value's, and `if`
  and `otherwise` the widest of their branches; `sum_best` the sum of the
  `k` greatest least and most bounds.

None for `seconds`, `seconds_late`, an `exec` block, whose program's
declared bounds the plan does not keep, and a group with no runs. A bound
past `MOST_BITS` is none, as such a number is.

The trees are walked by recursion: the save keeps each at most
`check.MOST_DEPTH` deep.
"""

import math
from collections.abc import Mapping, Sequence
from fractions import Fraction

from forge.domain.blocks.board_check import BoardColumn, BoardsCheck, column_at
from forge.domain.blocks.catalogue import CATALOGUE, ENTERED, SKIP, Kind
from forge.domain.blocks.check import form_of, leaf_of, same_value
from forge.domain.blocks.evaluate import MOST_BITS, Column, RunResult
from forge.domain.blocks.numbers import exact
from forge.domain.definitions import Leaderboard

Bounds = tuple[Fraction, Fraction] | None
"""The least and the most a number can be, or none when either is unknown."""

ZERO_ONE: Bounds = (Fraction(0), Fraction(1))
_TRUTHS = ("lt", "le", "gt", "ge", "eq", "ne", "and", "or", "not")


def _kept(bounds: Bounds) -> Bounds:
    if bounds is None:
        return None
    for number in bounds:
        if number.numerator.bit_length() > MOST_BITS or number.denominator.bit_length() > MOST_BITS:
            return None
    return bounds


def widest(*given: Bounds) -> Bounds:
    """The bounds holding each of `given`, none when any is none."""
    if not given or any(each is None for each in given):
        return None
    known = [each for each in given if each is not None]
    return (min(low for low, _ in known), max(high for _, high in known))


def _over_numbers(name: str, given: Sequence[Bounds]) -> Bounds:
    """A block over numbers, its inputs' bounds `given`."""
    if name in _TRUTHS:
        return ZERO_ONE
    if any(each is None for each in given):
        return None
    known = [each for each in given if each is not None]
    first = known[0]
    match name:
        case "add":
            zero = Fraction(0)
            return _kept(
                (sum((low for low, _ in known), zero), sum((high for _, high in known), zero))
            )
        case "sub":
            return _kept((first[0] - known[1][1], first[1] - known[1][0]))
        case "mul":
            found = first
            for each in known[1:]:
                corners = [a * b for a in found for b in each]
                kept = _kept((min(corners), max(corners)))
                if kept is None:
                    return None
                found = kept
            return found
        case "div":
            low, high = known[1]
            if low <= 0 <= high:
                return None
            corners = [a / b for a in first for b in known[1]]
            return _kept((min(corners), max(corners)))
        case "min":
            return (min(low for low, _ in known), min(high for _, high in known))
        case "max":
            return (max(low for low, _ in known), max(high for _, high in known))
        case "floor":
            return (Fraction(math.floor(first[0])), Fraction(math.floor(first[1])))
        case "ceil":
            return (Fraction(math.ceil(first[0])), Fraction(math.ceil(first[1])))
    return None


def _summed(each: Bounds, most: Fraction, least: Fraction, *, some: bool) -> Bounds:
    """A sum of terms each within `each`, weighing `most` in all, or, when
    `some`, any of them, weighing at least `least`."""
    if each is None:
        return None
    low, high = each
    if not some:
        return _kept((low * most, high * most))
    return _kept((min(low * least, low * most), max(high * least, high * most)))


def value_bounds(
    values: Mapping[str, object],
    groups: Mapping[str, Sequence[RunResult]],
    columns: Mapping[str, Column],
) -> dict[str, Bounds]:
    """Each of a task's values' bounds: `groups` each group's runs, `all`
    among them, as a fold reads them, and `columns` each report's column as
    the plan gives it."""
    found: dict[str, Bounds] = {}
    walking: set[str] = set()

    def value(name: str) -> Bounds:
        if name in found:
            return found[name]
        tree = values.get(name)
        if tree is None or name in walking:
            return None
        walking.add(name)
        found[name] = node(tree, None, None)
        walking.discard(name)
        return found[name]

    def node(tree: object, runs: Sequence[RunResult] | None, missing: object) -> Bounds:
        number = exact(tree) if not isinstance(tree, dict | list) else None
        if number is not None:
            return (number, number)
        if not isinstance(tree, dict):
            return None
        if "block" not in tree:
            leaf = leaf_of(tree)
            if leaf is None:
                return None
            kind, named = leaf
            if kind == "value" and isinstance(named, str):
                return value(named)
            if kind == "fact":
                return ZERO_ONE if named == "compiled" else None
            if kind == "run" and isinstance(named, list) and runs is not None:
                return _run_column(str(named[1]), runs, columns, missing)
            return None
        name = tree["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        form = form_of(block, tree) if block is not None else None
        if block is None or form is None:
            return None
        if block.name == ENTERED:
            least, most = exact(tree.get("at_least")), exact(tree.get("at_most"))
            return (least, most) if least is not None and most is not None else None
        if form.fold:
            over = tree.get("over")
            leaf = leaf_of(over) if isinstance(over, dict) else None
            group = groups.get(str(leaf[1])) if leaf is not None else None
            if not group:
                return None
            gone = tree.get("missing")
            weights = [_weight(run, tree.get("weights")) for run in group]
            some = "where" in tree or gone == SKIP
            total, least = sum(weights, Fraction(0)), min(weights)
            if block.name == "count":
                return (Fraction(0) if "where" in tree else total, total)
            each = node(tree.get("of"), group, gone)
            if block.name == "sum":
                return _summed(each, total, least, some=some)
            return each
        if "over" in form.inputs:
            # `exec`: the plan keeps no bounds its program declares.
            return None
        if block.name == "if":
            return widest(
                node(tree.get("then"), runs, missing), node(tree.get("else"), runs, missing)
            )
        given = tree.get("of")
        listed = given if isinstance(given, list) else [given]
        return _over_numbers(block.name, [node(each, runs, missing) for each in listed])

    return {name: value(name) for name in values}


def _weight(run: RunResult, weights: object) -> Fraction:
    if not isinstance(weights, dict) or len(weights) != 1:
        return Fraction(1)
    [(name, table)] = weights.items()
    if isinstance(table, dict):
        for value, weight in table.items():
            if same_value(value, run.key.get(name)):
                return exact(weight) or Fraction(1)
    return Fraction(1)


def _run_column(
    column: str, runs: Sequence[RunResult], columns: Mapping[str, Column], missing: object
) -> Bounds:
    if column == "passed":
        return ZERO_ONE
    if column in columns:
        found = columns[column]
        if found.at_least is None or found.at_most is None:
            return None
        bounds: Bounds = (found.at_least, found.at_most)
        instead = exact(missing)
        return widest(bounds, (instead, instead)) if instead is not None else bounds
    numbers = [exact(run.key.get(column)) for run in runs]
    if not numbers or any(number is None for number in numbers):
        return None
    known = [number for number in numbers if number is not None]
    return (min(known), max(known))


def fact_bounds(name: str, most: int | None) -> Bounds:
    """A submission's fact's bounds, `most` its task's `submissions.max`."""
    if name in ("marked", "compiled"):
        return ZERO_ONE
    if name == "attempt" and most is not None:
        return (Fraction(1), Fraction(most))
    return None


def column_bounds(
    boards: Mapping[str, Leaderboard],
    checked: BoardsCheck,
    tasks: Mapping[str, Mapping[str, Bounds]],
    most: Mapping[str, int | None],
) -> dict[BoardColumn, Bounds]:
    """Each number column's bounds, of the boards worked out on read:
    `tasks` each task's values' bounds, a task the board reads with none
    there giving none, and `most` each task's `submissions.max`."""
    found: dict[BoardColumn, Bounds] = {}
    for board, name in checked.order:
        shape = checked.shapes[board][name]
        if shape.kind is not Kind.NUMBER:
            continue
        reader = _ColumnBounds(board, boards[board], checked, tasks, most, found)
        found[(board, name)] = reader.node(boards[board].columns[name], ())
    return found


class _ColumnBounds:
    def __init__(
        self,
        board: str,
        leaderboard: Leaderboard,
        checked: BoardsCheck,
        tasks: Mapping[str, Mapping[str, Bounds]],
        most: Mapping[str, int | None],
        done: Mapping[BoardColumn, Bounds],
    ) -> None:
        self.board = board
        self.leaderboard = leaderboard
        self.checked = checked
        self.tasks = tasks
        self.most = most
        self.done = done

    def read(self, task: str, name: str) -> Bounds:
        fact = fact_bounds(name, self.most.get(task))
        if fact is not None or name in ("attempt", "seconds", "seconds_late"):
            return fact
        return self.tasks.get(task, {}).get(name)

    def submission_tasks(self, node: object) -> frozenset[str]:
        if isinstance(node, str):
            shape = self.checked.shapes[self.board].get(node)
            return shape.of_tasks if shape is not None else frozenset()
        if isinstance(node, dict) and "block" in node:
            if node["block"] == "otherwise" and isinstance(node.get("of"), list):
                return frozenset().union(*(self.submission_tasks(each) for each in node["of"]))
            source = node.get("from")
            leaf = leaf_of(source) if isinstance(source, dict) else None
            return frozenset({str(leaf[1])}) if leaf is not None else frozenset()
        leaf = leaf_of(node) if isinstance(node, dict) else None
        if leaf is not None and leaf[0] == "column":
            board, name = column_at(self.board, leaf[1])
            shape = self.checked.shapes.get(board, {}).get(name)
            return shape.of_tasks if shape is not None else frozenset()
        return frozenset()

    def node(self, tree: object, scope: tuple[str, ...]) -> Bounds:
        number = exact(tree) if not isinstance(tree, dict | list) else None
        if number is not None:
            return (number, number)
        if not isinstance(tree, dict):
            return None
        if "block" not in tree:
            leaf = leaf_of(tree)
            if leaf is None:
                return None
            kind, named = leaf
            if kind == "column":
                return self.done.get(column_at(self.board, named))
            if kind == "value" and isinstance(named, list):
                of, name = named
                if isinstance(of, str) and of in scope:
                    return self.read(of, str(name))
                tasks = self.submission_tasks(of)
                if not tasks:
                    return None
                return widest(*(self.read(task, str(name)) for task in sorted(tasks)))
            return None
        name = tree["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        form = form_of(block, tree) if block is not None else None
        if block is None or form is None:
            return None
        over = form.goes_over
        if over is not None:
            leaf = leaf_of(tree[over]) if isinstance(tree.get(over), dict) else None
            task = str(leaf[1]) if leaf is not None else ""
            if block.name == "best_each":
                value = tree.get("value")
                named = leaf_of(value) if isinstance(value, dict) else None
                return self.read(task, str(named[1])) if named is not None else None
            inner = (*scope, task)
            most = self.most.get(task)
            if block.name == "count":
                return (Fraction(0), Fraction(most)) if most is not None else None
            each = self.node(tree.get("of"), inner)
            instead = exact(tree.get("missing"))
            if instead is not None:
                each = widest(each, (instead, instead))
            if block.name == "sum":
                if most is None:
                    return None
                return _summed(each, Fraction(most), Fraction(1), some=True)
            return each
        if block.name in ("if",):
            return widest(self.node(tree.get("then"), scope), self.node(tree.get("else"), scope))
        given = tree.get("of")
        listed = given if isinstance(given, list) else [given]
        got = [self.node(each, scope) for each in listed]
        if block.name == "otherwise":
            return widest(*got)
        if block.name == "sum_best":
            k = tree.get("k")
            if not isinstance(k, int) or any(each is None for each in got):
                return None
            known = [each for each in got if each is not None]
            take = min(k, len(known))
            lows = sorted((low for low, _ in known), reverse=True)[:take]
            highs = sorted((high for _, high in known), reverse=True)[:take]
            return _kept((sum(lows, Fraction(0)), sum(highs, Fraction(0))))
        return _over_numbers(block.name, got)


def self_reported_parts(
    checked: BoardsCheck, reported: Mapping[str, frozenset[str]]
) -> dict[BoardColumn, tuple[tuple[str, str], ...]]:
    """Each column's self-reported parts: each task and value it reads,
    directly or through the columns it reads, that reads a column the
    entrant could set, `reported` each task's such values (#504)."""
    found: dict[BoardColumn, tuple[tuple[str, str], ...]] = {}
    for board, name in checked.order:
        shape = checked.shapes[board][name]
        parts = {
            (task, value) for task, value in shape.reads if value in reported.get(task, frozenset())
        }
        for other in shape.columns:
            parts.update(found.get(other, ()))
        found[(board, name)] = tuple(sorted(parts))
    return found


__all__ = [
    "ZERO_ONE",
    "Bounds",
    "column_bounds",
    "fact_bounds",
    "self_reported_parts",
    "value_bounds",
    "widest",
]
