"""Working a row of a contest's leaderboards out (SIMPLER-CORE.md 10.1, the
leaderboards note's 5.3 and 5.4): every column of the boards the check
reads (`BoardsCheck.order`), for one row, from that row's results, each
task's submissions oldest first, every number an exact fraction.

- **A pick** looks at the task's results oldest first: `first` takes the
  oldest that meets its `where`, `latest` the newest, `highest` and
  `lowest` the one with the highest or lowest `by`, the oldest on a tie. A
  pick over nothing left is none.
- **A fold over a task's results** folds its `of` over those that meet its
  `where`; over nothing left `sum`, `max` and `min` are missing and
  `count` is 0.
- **Picks, folds and `best_each` skip:** a submission whose `where` is
  missing does not meet it, and one whose `by` or folded `of` is missing
  is left out.
- **Missing and none spread** through arithmetic, the comparisons, `and`,
  `or`, `not`, `r.v` and `sum_best`; `if` reads only the branch its test
  takes, a submission's results as its test true when there is one and a
  missing number giving missing; `otherwise` gives its second input when
  its first is missing or none.
- **`best_each`** takes the best of each value the task's value reads
  directly over the results, each in its direction, the oldest on a tie,
  and works the value out again from them with the task's own tree; it is
  missing when that tree reads anything but those values and numbers, or
  a direction is not given, which the save refuses across files (phase
  5.7).
- **Waiting.** A value of a submission still being worked out, or waiting
  for staff, is left out of every block that reads it, as a missing one
  is, and the submission is recorded as left out waiting (guarantee 5).

Each cell records the submissions it counted (the leaderboards note's
5.3): a pick its one; `max` and `min` the oldest that reached the result;
`best_each` one for each value; `sum` and `count` every one they kept; a
block reading other cells theirs.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from forge.domain.blocks.board_check import BoardColumn, BoardsCheck, column_at
from forge.domain.blocks.catalogue import ASC, CATALOGUE, NUMBERS, PICKS
from forge.domain.blocks.check import form_of, leaf_of
from forge.domain.blocks.evaluate import over_numbers
from forge.domain.blocks.numbers import exact
from forge.domain.definitions import Leaderboard

SubmissionAt = tuple[str, int]
"""A submission by its task's id and its number."""


@dataclass(frozen=True, slots=True)
class Submitted:
    """One submission's results as a board reads them: its number, each
    value and fact that has a number, by name, and the names still
    waiting. A name in neither is missing."""

    number: int
    numbers: Mapping[str, Fraction] = field(default_factory=dict)
    waiting: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Cell:
    """What one column gave a row: its number, or for a column giving a
    submission's results, the submission with its task, none for none;
    whether it gives a submission's results; the submissions it counted;
    and those it left out as still waiting."""

    number: Fraction | None = None
    picked: tuple[str, Submitted] | None = None
    submission: bool = False
    counted: frozenset[SubmissionAt] = frozenset()
    waiting: frozenset[SubmissionAt] = frozenset()

    def having(self, *others: Cell) -> Cell:
        """This, with what `others` counted and left out added."""
        return Cell(
            self.number,
            self.picked,
            self.submission,
            self.counted.union(*(other.counted for other in others)),
            self.waiting.union(*(other.waiting for other in others)),
        )

    @property
    def nothing(self) -> bool:
        """Whether it is missing, or none."""
        return self.number is None and self.picked is None


_MISSING = Cell()


def work_out_row(
    boards: Mapping[str, Leaderboard],
    checked: BoardsCheck,
    results: Mapping[str, Sequence[Submitted]],
    trees: Mapping[str, Mapping[str, object]],
) -> dict[BoardColumn, Cell]:
    """Every column of the boards the check reads, for one row: `results`
    the row's submissions to each task, oldest first, a task with none left
    out, and `trees` each task's values' trees, which `best_each` reads."""
    row = _Row(results, trees)
    for board, name in checked.order:
        row.board = board
        row.done[(board, name)] = row.node(boards[board].columns[name], {})
    return row.done


Scope = Mapping[str, tuple[str, Submitted]]


class _Row:
    def __init__(
        self,
        results: Mapping[str, Sequence[Submitted]],
        trees: Mapping[str, Mapping[str, object]],
    ) -> None:
        self.results = results
        self.trees = trees
        self.done: dict[BoardColumn, Cell] = {}
        self.board = ""

    def node(self, tree: object, scope: Scope) -> Cell:
        if not isinstance(tree, dict | list):
            number = exact(tree)
            if number is None:
                raise ValueError(f"{tree!r} is not a node of a column's tree")
            return Cell(number)
        if not isinstance(tree, dict):
            raise ValueError(f"{tree!r} is not a node of a column's tree")
        if "block" in tree:
            return self.block(tree, scope)
        leaf = leaf_of(tree)
        if leaf is None:
            raise ValueError(f"{tree!r} is not a node of a column's tree")
        kind, named = leaf
        if kind == "column":
            return self.done[column_at(self.board, named)]
        if kind == "value" and isinstance(named, list):
            of, name = named
            if isinstance(of, str) and of in scope:
                return _read(scope[of], name)
            picked = self.done[(self.board, of)] if isinstance(of, str) else self.node(of, scope)
            if picked.picked is None:
                return Cell(counted=picked.counted, waiting=picked.waiting)
            return _read(picked.picked, name).having(picked)
        raise ValueError(f"{tree!r} gives no number on a board")

    def block(self, tree: Mapping[str, object], scope: Scope) -> Cell:
        name = tree["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        form = form_of(block, tree) if block is not None else None
        if block is None or form is None:
            raise ValueError(f"{tree!r} fits no block a board has")
        if form.goes_over is not None:
            source = tree[form.goes_over]
            assert isinstance(source, dict)
            task = str(source["results"])
            listed = self.results.get(task, ())
            if block.name in PICKS:
                return self.pick(block.name, tree, task, listed, scope)
            if block.name == "best_each":
                return self.best_each(tree, task, listed)
            return self.fold(block.name, tree, task, listed, scope)
        if block.name == "if":
            test = self.node(tree["test"], scope)
            if test.submission:
                truth = test.picked is not None
            elif test.number is None:
                return Cell(counted=test.counted, waiting=test.waiting)
            else:
                truth = test.number != 0
            return self.node(tree["then"] if truth else tree["else"], scope).having(test)
        given = tree["of"]
        inputs = given if isinstance(given, list) else [given]
        if block.name == "otherwise":
            first = self.node(inputs[0], scope)
            if not first.nothing:
                return first
            return self.node(inputs[1], scope).having(first)
        got = [self.node(each, scope) for each in inputs]
        read = _MISSING.having(*got)
        if any(each.number is None for each in got):
            return read
        numbers = [each.number for each in got if each.number is not None]
        if block.name == "sum_best":
            k = tree["k"]
            assert isinstance(k, int)
            best = sorted(numbers, reverse=True)[:k]
            return Cell(over_numbers("add", [Fraction(0), *best])).having(read)
        return Cell(over_numbers(block.name, numbers)).having(read)

    def _looked_at(
        self, tree: Mapping[str, object], key: str, task: str, each: Submitted, scope: Scope
    ) -> Cell | None:
        if key not in tree:
            return None
        inner = {**scope, task: (task, each)}
        return self.node(tree[key], inner)

    def pick(
        self,
        name: str,
        tree: Mapping[str, object],
        task: str,
        listed: Sequence[Submitted],
        scope: Scope,
    ) -> Cell:
        waiting: set[SubmissionAt] = set()
        kept: list[tuple[Submitted, Fraction]] = []
        for each in listed:
            where = self._looked_at(tree, "where", task, each, scope)
            if where is not None:
                waiting |= where.waiting
                if where.number is None or where.number == 0:
                    continue
            by = self._looked_at(tree, "by", task, each, scope)
            if by is not None:
                waiting |= by.waiting
                if by.number is None:
                    continue
            kept.append(
                (each, by.number if by is not None and by.number is not None else Fraction(0))
            )
        found: Submitted | None = None
        if kept:
            match name:
                case "first":
                    found = kept[0][0]
                case "latest":
                    found = kept[-1][0]
                case "highest":
                    best = max(number for _, number in kept)
                    found = next(each for each, number in kept if number == best)
                case "lowest":
                    best = min(number for _, number in kept)
                    found = next(each for each, number in kept if number == best)
        if found is None:
            return Cell(submission=True, waiting=frozenset(waiting))
        return Cell(
            picked=(task, found),
            submission=True,
            counted=frozenset({(task, found.number)}),
            waiting=frozenset(waiting),
        )

    def fold(
        self,
        name: str,
        tree: Mapping[str, object],
        task: str,
        listed: Sequence[Submitted],
        scope: Scope,
    ) -> Cell:
        waiting: set[SubmissionAt] = set()
        kept: list[tuple[Submitted, Fraction]] = []
        for each in listed:
            where = self._looked_at(tree, "where", task, each, scope)
            if where is not None:
                waiting |= where.waiting
                if where.number is None or where.number == 0:
                    continue
            of = self._looked_at(tree, "of", task, each, scope)
            if of is not None:
                waiting |= of.waiting
                if of.number is None:
                    continue
            kept.append(
                (each, of.number if of is not None and of.number is not None else Fraction(1))
            )
        left = frozenset(waiting)
        if name == "count":
            counted = frozenset((task, each.number) for each, _ in kept)
            return Cell(Fraction(len(kept)), counted=counted, waiting=left)
        if not kept:
            return Cell(waiting=left)
        if name == "sum":
            counted = frozenset((task, each.number) for each, _ in kept)
            total = over_numbers("add", [Fraction(0), *(number for _, number in kept)])
            return Cell(total, counted=counted, waiting=left)
        best = (max if name == "max" else min)(number for _, number in kept)
        first = next(each for each, number in kept if number == best)
        return Cell(best, counted=frozenset({(task, first.number)}), waiting=left)

    def best_each(self, tree: Mapping[str, object], task: str, listed: Sequence[Submitted]) -> Cell:
        named = tree["value"]
        directions = tree["directions"]
        assert isinstance(named, dict) and isinstance(directions, dict)
        value = str(named["name"])
        own = self.trees.get(task, {}).get(value)
        reads = _plain_reads(own)
        if own is None or reads is None or not reads <= set(directions):
            return _MISSING
        waiting: set[SubmissionAt] = set()
        counted: set[SubmissionAt] = set()
        bests: dict[str, Fraction | None] = {}
        for each in sorted(reads):
            best: tuple[Submitted, Fraction] | None = None
            for submitted in listed:
                if each in submitted.waiting:
                    waiting.add((task, submitted.number))
                    continue
                number = submitted.numbers.get(each)
                if number is None:
                    continue
                if (
                    best is None
                    or (directions[each] == ASC and number < best[1])
                    or (directions[each] != ASC and number > best[1])
                ):
                    best = (submitted, number)
            bests[each] = best[1] if best is not None else None
            if best is not None:
                counted.add((task, best[0].number))
        return Cell(_again(own, bests), counted=frozenset(counted), waiting=frozenset(waiting))


def _read(picked: tuple[str, Submitted], name: str) -> Cell:
    task, submitted = picked
    if name in submitted.waiting:
        return Cell(waiting=frozenset({(task, submitted.number)}))
    return Cell(submitted.numbers.get(name))


def _plain_reads(tree: object) -> set[str] | None:
    """The values a task's value's tree reads, when it reads nothing but
    values and numbers through blocks over numbers; none otherwise."""
    found: set[str] = set()
    waiting = [tree]
    while waiting:
        node = waiting.pop()
        if isinstance(node, list):
            waiting.extend(node)
            continue
        if not isinstance(node, dict):
            if exact(node) is None:
                return None
            continue
        if "block" not in node:
            leaf = leaf_of(node)
            if leaf is None or leaf[0] != "value" or not isinstance(leaf[1], str):
                return None
            found.add(leaf[1])
            continue
        block = CATALOGUE.get(str(node["block"]))
        form = form_of(block, node) if block is not None else None
        if form is None or form.given != NUMBERS or block is None or block.name == "entered":
            return None
        waiting.extend(given for key, given in node.items() if key != "block")
    return found


def _again(tree: object, bests: Mapping[str, Fraction | None]) -> Fraction | None:
    """A task's value's tree worked out again from `bests`, by value."""
    if not isinstance(tree, dict | list):
        return exact(tree)
    if not isinstance(tree, dict):
        return None
    if "block" not in tree:
        return bests.get(str(tree["value"]))
    name = str(tree["block"])
    if name == "if":
        test = _again(tree["test"], bests)
        if test is None:
            return None
        return _again(tree["then"] if test != 0 else tree["else"], bests)
    given = tree["of"]
    numbers = [_again(each, bests) for each in (given if isinstance(given, list) else [given])]
    if any(number is None for number in numbers):
        return None
    return over_numbers(name, [number for number in numbers if number is not None])


__all__ = ["Cell", "SubmissionAt", "Submitted", "work_out_row"]
