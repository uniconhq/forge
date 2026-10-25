"""Working a task's values out (SIMPLER-CORE.md 9.2, 9.3 and 14, #511):
`evaluate` gives each value its `Outcome`, a number, missing or waiting,
with what it read, every number an exact fraction.

- A test is true when it isn't 0; the comparisons, `and`, `or` and `not`
  give 1 or 0; `if` reads only the branch it takes.
- Missing in, missing out, and dividing by zero is missing. `and` and `or`
  read every input, so a missing one makes them missing too.
- A fold reads a group's runs, its `of` and `where` once for each run
  through `{run: [<group>, <column>]}`. `weights` gives a run a weight by
  one repeated input's value, 1 when it names none; `sum` adds each run's
  `of` times its weight, and `count` adds the weights.
- A report is missing when its run wasn't accepted, or it has no number
  for it, as when its step didn't run. In a fold it counts as the fold's
  `missing` says when it says: `skip` leaves the run out, and a number
  counts as that number. Otherwise it counts as the column's worst bound:
  `at_least` when higher is better, `at_most` when lower is; a column with
  no bound on its worse side makes the fold missing. `passed` and a key's
  number are never missing.
- A fold left with no runs is missing, but `count`, which is 0.
- **Waiting:** a run whose verdict is `system_error` waits for staff, and
  makes every fold over a group holding it waiting. Waiting spreads
  through everything that reads it, ahead of missing, but an `if` branch
  not taken.

- **A number past `MOST_BITS`,** in its numerator or its denominator, is
  missing, as dividing by zero is: values that square each other a few
  times would otherwise build numbers of millions of digits on every read.

What a value read is the ids of the runs its folds read, and the names of
the values and the facts, through the values it read too, and along the
branches it took only: not their contents.

The work is bounded as well as the trees (`check.MOST_NODES`). Each value
is worked out once; each block that reads no run of the fold around it,
a fold among them, since a fold reads only its own runs, is worked out
once per call and kept, so folds nested in folds don't multiply. What is
left is a block reading a run, worked out once for each run. A tree is
walked without recursion: each block is a generator, driven from one
stack, so a deep tree costs no Python frames. A node the catalogue doesn't
have, or a fact that is not a number, raises, since the save refuses the
one and the caller gives the other, so it is never skipped or guessed.
"""

import math
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from forge.domain.blocks.catalogue import CATALOGUE, LEAVES, SKIP, Form
from forge.domain.blocks.check import form_of, same_value
from forge.domain.blocks.numbers import Better, exact

ACCEPTED = "accepted"
SYSTEM_ERROR = "system_error"
"""The verdict of a run that waits for staff (9.3)."""
MOST_BITS = 4096
"""The most bits a number's numerator or denominator holds."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """One run as a fold reads it: its id, its verdict, its key's values and
    its number reports, the reports of steps done once among them.
    """

    id: str
    verdict: str
    key: Mapping[str, object] = field(default_factory=dict)
    reports: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Column:
    """A report column as the plan gives it: which way is better, `higher`
    or `lower`, and its bounds.
    """

    better: Better | None = None
    at_least: Fraction | None = None
    at_most: Fraction | None = None

    @property
    def worst(self) -> Fraction | None:
        """What a missing report counts as: `at_least` when higher is
        better, `at_most` when lower is, none when there is no such bound.
        """
        if self.better is Better.HIGHER:
            return self.at_least
        if self.better is Better.LOWER:
            return self.at_most
        return None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a value worked out to: its number, none when it is missing or
    waiting; whether it waits for staff; and the ids of the runs, the values
    and the facts it read.
    """

    number: Fraction | None
    waiting: bool = False
    runs: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    facts: frozenset[str] = frozenset()

    @property
    def missing(self) -> bool:
        return self.number is None and not self.waiting


def evaluate(
    values: Mapping[str, object],
    runs: Mapping[str, Sequence[RunResult]],
    facts: Mapping[str, int | Fraction],
    columns: Mapping[str, Column],
) -> dict[str, Outcome]:
    """Each of a task's values worked out, as checked at the save: `runs`
    each group's runs in order, `all` among them; `facts` the submission's
    facts, each a number; `columns` each report a run may give, by name. A
    run's other columns are `passed` and its key's.
    """
    walk = _Walk(runs, facts, columns, _reading_runs(values.values()))
    for name in _in_order(values):
        got = walk.tree(values[name])
        walk.done[name] = Outcome(got.number, got.waiting, got.runs, got.values, got.facts)
    return {name: walk.done[name] for name in values}


@dataclass(frozen=True, slots=True)
class _Got:
    """What a node gave: as an `Outcome`, and `skip` for a missing report a
    fold's `missing: skip` leaves its run out for."""

    number: Fraction | None
    waiting: bool = False
    skip: bool = False
    runs: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    facts: frozenset[str] = frozenset()

    def reading(self, *others: _Got) -> _Got:
        """This, having read `others` too."""
        return _Got(
            self.number,
            self.waiting,
            self.skip,
            self.runs.union(*(other.runs for other in others)),
            self.values.union(*(other.values for other in others)),
            self.facts.union(*(other.facts for other in others)),
        )


_NONE = _Got(None)


@dataclass(frozen=True, slots=True)
class _Scope:
    """The run the innermost fold is reading, and that fold's `missing`."""

    run: RunResult | None = None
    missing: object = None


_Asks = Generator[tuple[object, _Scope], _Got, _Got]


def _kept(number: Fraction | None) -> Fraction | None:
    """`number`, or none past `MOST_BITS`."""
    if number is None:
        return None
    if number.numerator.bit_length() > MOST_BITS or number.denominator.bit_length() > MOST_BITS:
        return None
    return number


class _Walk:
    def __init__(
        self,
        runs: Mapping[str, Sequence[RunResult]],
        facts: Mapping[str, int | Fraction],
        columns: Mapping[str, Column],
        per_run: set[int],
    ) -> None:
        self.runs = runs
        self.facts = facts
        self.columns = columns
        self.per_run = per_run
        self.done: dict[str, Outcome] = {}
        self.kept: dict[int, _Got] = {}

    def tree(self, tree: object) -> _Got:
        """What a value's tree gives, its blocks driven from one stack, each
        with the node to keep its answer under, when it reads no run.
        """
        stack: list[tuple[_Asks, int | None]] = []
        got = self.start(tree, _Scope(), stack)
        while stack:
            asking, keep = stack[-1]
            try:
                node, scope = asking.send(got)  # type: ignore[arg-type]
            except StopIteration as stop:
                stack.pop()
                got = stop.value
                if keep is not None:
                    self.kept[keep] = stop.value
                continue
            got = self.start(node, scope, stack)
        assert got is not None
        return got

    def start(
        self, node: object, scope: _Scope, stack: list[tuple[_Asks, int | None]]
    ) -> _Got | None:
        """A leaf's number, or a block's kept answer, or none with the
        block put on the stack.
        """
        if not isinstance(node, dict | list):
            found = exact(node)
            if found is not None:
                return _Got(_kept(found))
        if not isinstance(node, dict):
            raise ValueError(f"{node!r} is not a node of a value's tree")
        if "block" in node:
            if id(node) in self.per_run:
                stack.append((self.block(node, scope), None))
                return None
            if id(node) in self.kept:
                return self.kept[id(node)]
            stack.append((self.block(node, scope), id(node)))
            return None
        if len(node) != 1 or next(iter(node)) not in LEAVES:
            raise ValueError(f"{node!r} is not a node of a value's tree")
        [(kind, named)] = node.items()
        match kind:
            case "value":
                done = self.done[named]
                return _Got(
                    done.number,
                    done.waiting,
                    runs=done.runs,
                    values=done.values | {named},
                    facts=done.facts,
                )
            case "fact":
                given = self.facts.get(named)
                if isinstance(given, bool) or not isinstance(given, int | Fraction):
                    raise ValueError(f"the fact {named} is {given!r}, not a number")
                return _Got(_kept(Fraction(given)), facts=frozenset({named}))
            case "run":
                _, column = named
                return self.column(column, scope)
        raise ValueError(f"{node!r} gives no number")

    def column(self, column: str, scope: _Scope) -> _Got:
        """A column of the run the innermost fold reads."""
        run = scope.run
        if run is None:
            raise ValueError(f"the column {column} is read outside a fold")
        if column == "passed":
            return _Got(Fraction(1 if run.verdict == ACCEPTED else 0))
        if column in self.columns:
            number = exact(run.reports.get(column)) if run.verdict == ACCEPTED else None
            if number is not None:
                return _Got(_kept(number))
            if scope.missing == SKIP:
                return _Got(None, skip=True)
            if scope.missing is not None:
                return _Got(exact(scope.missing))
            return _Got(self.columns[column].worst)
        number = exact(run.key.get(column))
        if number is None:
            raise ValueError(f"the run {run.id} has no number for the column {column}")
        return _Got(_kept(number))

    def block(self, node: Mapping[str, object], scope: _Scope) -> _Asks:
        name = node["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        if block is None:
            raise ValueError(f"{name!r} is not a block Unicon has")
        form = form_of(block, node)
        if form is None:
            raise ValueError(f"{node!r} fits no form of {block.name}")
        if form.fold:
            return (yield from self.fold(block.name, form, node))
        if block.name == "if":
            test = yield (node["test"], scope)
            if test.waiting or test.skip or test.number is None:
                return test
            branch = yield (node["then"] if test.number != 0 else node["else"], scope)
            return branch.reading(test)
        given = node["of"]
        got: list[_Got] = []
        for each in given if isinstance(given, list) else [given]:
            got.append((yield (each, scope)))
        return _apply(block.name, got)

    def fold(self, name: str, form: Form, node: Mapping[str, object]) -> _Asks:
        over = node["over"]
        assert isinstance(over, dict)
        runs = self.runs[over["runs"]]
        read = _Got(None, runs=frozenset(run.id for run in runs))
        if any(run.verdict == SYSTEM_ERROR for run in runs):
            return _Got(None, waiting=True).reading(read)
        missing = node.get("missing")
        taken: list[tuple[Fraction, Fraction]] = []
        blocked: _Got | None = None
        for run in runs:
            scope = _Scope(run, missing)
            if "where" in node:
                test = yield (node["where"], scope)
                read = read.reading(test)
                if test.skip:
                    continue
                if test.waiting or test.number is None:
                    blocked = test if test.waiting or blocked is None else blocked
                    continue
                if test.number == 0:
                    continue
            number = Fraction(1)
            if "of" in form.inputs:
                got = yield (node["of"], scope)
                read = read.reading(got)
                if got.skip:
                    continue
                if got.waiting or got.number is None:
                    blocked = got if got.waiting or blocked is None else blocked
                    continue
                number = got.number
            taken.append((self.weight(run, node.get("weights")), number))
        if blocked is not None:
            return _Got(None, blocked.waiting).reading(read)
        if name == "count":
            return _Got(_summed(weight for weight, _ in taken)).reading(read)
        if not taken:
            return read
        numbers = [number for _, number in taken]
        found: Fraction | None
        match name:
            case "sum":
                found = _summed(weight * number for weight, number in taken)
            case "min":
                found = min(numbers)
            case "max":
                found = max(numbers)
            case _:
                raise ValueError(f"{name} folds no runs")
        return _Got(_kept(found)).reading(read)

    def weight(self, run: RunResult, weights: object) -> Fraction:
        """A run's weight: the one `weights` gives its key's value, else 1."""
        if not isinstance(weights, dict):
            return Fraction(1)
        [(name, table)] = weights.items()
        for value, weight in table.items():
            if same_value(value, run.key.get(name)):
                found = exact(weight)
                assert found is not None
                return found
        return Fraction(1)


def _apply(name: str, got: Sequence[_Got]) -> _Got:
    """A block over numbers: waiting when an input waits, then missing when
    one is missing, else its number.
    """
    read = _NONE.reading(*got)
    for each in got:
        if each.waiting:
            return _Got(None, waiting=True).reading(read)
    for each in got:
        if each.skip:
            return _Got(None, skip=True).reading(read)
    numbers = [each.number for each in got]
    if any(number is None for number in numbers):
        return read
    given = [number for number in numbers if number is not None]
    first = given[0]
    found: Fraction | None
    match name:
        case "add":
            found = _summed(given)
        case "sub":
            found = first - given[1]
        case "mul":
            found = Fraction(1)
            for number in given:
                found = _kept(found * number)
                if found is None:
                    break
        case "div":
            found = None if given[1] == 0 else first / given[1]
        case "lt":
            found = _truth(first < given[1])
        case "le":
            found = _truth(first <= given[1])
        case "gt":
            found = _truth(first > given[1])
        case "ge":
            found = _truth(first >= given[1])
        case "eq":
            found = _truth(first == given[1])
        case "ne":
            found = _truth(first != given[1])
        case "and":
            found = _truth(all(number != 0 for number in given))
        case "or":
            found = _truth(any(number != 0 for number in given))
        case "not":
            found = _truth(first == 0)
        case "min":
            found = min(given)
        case "max":
            found = max(given)
        case "floor":
            found = Fraction(math.floor(first))
        case "ceil":
            found = Fraction(math.ceil(first))
        case _:
            raise ValueError(f"{name} is not a block over numbers")
    return _Got(_kept(found)).reading(read)


def _summed(numbers: Iterable[Fraction]) -> Fraction | None:
    """The sum of `numbers`, or none once it passes `MOST_BITS`."""
    total = Fraction(0)
    for number in numbers:
        kept = _kept(total + number)
        if kept is None:
            return None
        total = kept
    return total


def _reading_runs(trees: Iterable[object]) -> set[int]:
    """The nodes of `trees` that read a run of the fold around them: a
    `{run}` leaf, and a block given one, or given a node that reads one,
    but a fold, which reads its own runs only. Walked without recursion.
    """
    found: set[int] = set()
    waiting: list[tuple[object, bool]] = [(tree, False) for tree in trees]
    while waiting:
        node, seen = waiting.pop()
        if isinstance(node, list):
            waiting.extend((each, False) for each in node)
            continue
        if not isinstance(node, dict):
            continue
        if "block" not in node:
            if "run" in node:
                found.add(id(node))
            continue
        given = _given(node)
        if not seen:
            waiting.append((node, True))
            waiting.extend((each, False) for each in given)
        elif "over" not in node and any(id(each) in found for each in given):
            found.add(id(node))
    return found


def _given(node: Mapping[str, object]) -> list[object]:
    """The nodes a block is given, each list's items one by one."""
    given: list[object] = []
    for key, each in node.items():
        if key != "block":
            given.extend(each if isinstance(each, list) else [each])
    return given


def _truth(test: bool) -> Fraction:
    return Fraction(1 if test else 0)


def _in_order(values: Mapping[str, object]) -> list[str]:
    """The values, each after every value it reads, on any branch."""
    reads = {name: _value_leaves(tree) for name, tree in values.items()}
    for name, read in reads.items():
        for unknown in sorted(read - set(values)):
            raise ValueError(f"the value {name} reads {unknown}, which is no value")
    order: list[str] = []
    placed: set[str] = set()
    waiting = sorted(values)
    while waiting:
        ready = [name for name in waiting if reads[name] <= placed]
        if not ready:
            raise ValueError(f"the values {', '.join(waiting)} read each other in a loop")
        order.extend(ready)
        placed.update(ready)
        waiting = [name for name in waiting if name not in placed]
    return order


def _value_leaves(tree: object) -> set[str]:
    """The values a tree reads directly, through `{value: <name>}`."""
    found: set[str] = set()
    waiting = [tree]
    while waiting:
        node = waiting.pop()
        if isinstance(node, list):
            waiting.extend(node)
        elif isinstance(node, dict):
            if "block" in node:
                waiting.extend(given for key, given in node.items() if key != "block")
            elif set(node) == {"value"} and isinstance(node["value"], str):
                found.add(node["value"])
    return found


__all__ = ["ACCEPTED", "SYSTEM_ERROR", "Column", "Outcome", "RunResult", "evaluate"]
