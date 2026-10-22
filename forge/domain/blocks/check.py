"""The structural check of a task's values against the catalogue (the
leaderboards note's 6.1, SIMPLER-CORE.md 9.5): every node a block the
catalogue has or a leaf, each input of the kind its block takes, every name
a leaf reads existing, a run's column read only inside a fold over its
group, and no value reading itself through others. Group and value names
are one set, none named like a fact, and `all` is every run's. Working a
value out is stage 5's.

What each value reads directly is recorded (`Reads`), so the save can tell
which runs a value shown during the contest reads, and whether it reads a
fact, through other values too (`ValuesCheck.through`).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from forge.domain.blocks.catalogue import (
    ALL,
    CATALOGUE,
    FACTS,
    LEAVES,
    SKIP,
    TASK_FACTS,
    Block,
    BlockInput,
    Kind,
)
from forge.domain.definitions import VALUE_NAME
from forge.domain.yaml_models import Path, is_number

Found = list[tuple[Path, str]]

_LEAF_WORDS = (
    "Give a number, a block such as {block: add, of: [...]}, or what it reads: "
    "{runs: <group>}, {run: [<group>, <column>]}, {value: <name>} or {fact: <name>}."
)


@dataclass(frozen=True, slots=True)
class Reads:
    """What one value's tree reads directly: groups, through a fold's
    `over` or a run's column; other values; facts; and each run column, by
    its group.
    """

    groups: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    facts: frozenset[str] = frozenset()
    columns: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class ValuesCheck:
    """Every problem with a task's values, each at its path below
    `scoring`, and what each value that is there reads directly.
    """

    problems: Found
    reads: Mapping[str, Reads] = field(default_factory=dict)

    def through(self, name: str) -> Reads:
        """What the value `name` reads, directly or through the values it
        reads, itself among them.
        """
        groups: set[str] = set()
        values: set[str] = set()
        facts: set[str] = set()
        columns: set[tuple[str, str]] = set()
        waiting = [name]
        while waiting:
            current = waiting.pop()
            if current in values:
                continue
            values.add(current)
            read = self.reads.get(current, Reads())
            groups |= read.groups
            facts |= read.facts
            columns |= read.columns
            waiting.extend(read.values)
        return Reads(frozenset(groups), frozenset(values), frozenset(facts), frozenset(columns))


def same_value(first: object, second: object) -> bool:
    """Whether two values of a task's file are the same value: true or false
    only ever equal to themselves, and a number equal to another of the same
    amount, never to text.
    """
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    if is_number(first) and is_number(second):
        return Decimal(str(first)) == Decimal(str(second))
    return type(first) is type(second) and first == second


def check_names(groups: Iterable[str], values: Iterable[str]) -> Found:
    """Group and value names: each written as a value's name is, none
    `all`, none named like a fact, and none both a group and a value.
    """
    found: Found = []
    taken = set(groups)
    for kind, names in (("groups", taken), ("values", values)):
        for name in names:
            at: Path = (kind, name)
            if not isinstance(name, str) or not VALUE_NAME.match(name):
                found.append(
                    (
                        at,
                        "A name may use only lower case letters, digits and underscores (_), "
                        "starting with a letter, since a formula reads it.",
                    )
                )
            elif name == ALL:
                found.append((at, "all is every run, which every task has. Choose another name."))
            elif name in FACTS:
                found.append(
                    (
                        at,
                        f"{name} is a fact of each submission, which a value reads as "
                        f"{{fact: {name}}}. Choose another name.",
                    )
                )
            elif kind == "values" and name in taken:
                found.append(
                    (
                        at,
                        f"{name} is already a group, and groups and values share one set of "
                        "names, so show can list either. Choose another name.",
                    )
                )
    return found


def check_values(
    values: Mapping[str, object],
    *,
    groups: Collection[str],
    columns: Mapping[str, str | None],
    keys: Mapping[str, Sequence[object]],
) -> ValuesCheck:
    """Check each of a task's values, `groups` the task's groups besides
    `all`, `columns` each column of a run, with none for a number a fold may
    read and the reason for any other, and `keys` each repeated input the
    organiser fills with the values its runs take, which `weights` may name.
    """
    checker = _Checker(values, set(groups) | {ALL}, columns, keys)
    reads: dict[str, Reads] = {}
    for name, tree in values.items():
        checker.reading = _Reading()
        checker.node(("values", name), tree, BlockInput(Kind.NUMBER), None)
        found = checker.reading
        reads[name] = Reads(
            frozenset(found.groups),
            frozenset(found.values),
            frozenset(found.facts),
            frozenset(found.columns),
        )
    checker.problems.extend(_loops(reads))
    return ValuesCheck(checker.problems, reads)


@dataclass
class _Reading:
    groups: set[str] = field(default_factory=set)
    values: set[str] = field(default_factory=set)
    facts: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)


class _Checker:
    def __init__(
        self,
        values: Mapping[str, object],
        groups: set[str],
        columns: Mapping[str, str | None],
        keys: Mapping[str, Sequence[object]],
    ) -> None:
        self.values = values
        self.groups = groups
        self.columns = columns
        self.keys = keys
        self.problems: Found = []
        self.reading = _Reading()

    def refuse(self, at: Path, message: str) -> None:
        self.problems.append((at, message))

    def node(self, at: Path, node: object, wanted: BlockInput, over: str | None) -> None:
        """Check one node given to an input that takes `wanted`, `over` the
        group whose runs the innermost fold around it reads them once for
        each of, when the input is read per run.
        """
        match wanted.kind:
            case Kind.RUNS:
                self._runs(at, node)
            case Kind.WEIGHTS:
                self._weights(at, node)
            case Kind.MISSING:
                if node != SKIP and not is_number(node):
                    self.refuse(
                        at,
                        f"Give what a missing report counts as: {SKIP}, to leave the run out, "
                        "or a number.",
                    )
            case Kind.NUMBER:
                self._number(at, node, over)

    def _number(self, at: Path, node: object, over: str | None) -> None:
        if isinstance(node, bool):
            self.refuse(at, "Give a number, 1 or 0, not true or false.")
            return
        if is_number(node):
            return
        if not isinstance(node, dict):
            self.refuse(at, _LEAF_WORDS)
            return
        if "block" in node:
            self._block(at, node, over)
            return
        leaf = _leaf(node)
        if leaf is None:
            self.refuse(at, _LEAF_WORDS)
            return
        kind, named = leaf
        match kind:
            case "runs":
                self.refuse(
                    at,
                    "A group's runs give no number by themselves. Fold them, such as "
                    f"{{block: count_runs, over: {{runs: {named}}}}}.",
                )
            case "run":
                self._column(at, named, over)
            case "value":
                if not isinstance(named, str) or named not in self.values:
                    self.refuse(at, f"There is no value called {named}.")
                else:
                    self.reading.values.add(named)
            case "fact":
                if named in TASK_FACTS:
                    assert isinstance(named, str)
                    self.reading.facts.add(named)
                elif named in FACTS:
                    self.refuse(
                        at,
                        f"A task's value reads one submission, so it cannot read {named}, "
                        f"which {_FACT_WHY.get(str(named), 'changes')}. It may read "
                        f"{', '.join(TASK_FACTS)}.",
                    )
                else:
                    self.refuse(
                        at,
                        f"There is no fact called {named}. A value may read "
                        f"{', '.join(TASK_FACTS)}.",
                    )

    def _block(self, at: Path, node: Mapping[str, object], over: str | None) -> None:
        name = node["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        if block is None:
            self.refuse(
                (*at, "block"),
                f"{name} is not a block Unicon has. The blocks are {', '.join(CATALOGUE)}.",
            )
            return
        for key in node:
            if key != "block" and key not in block.inputs:
                self.refuse(
                    (*at, str(key)),
                    f"{block.name} has no input called {key}. It takes {', '.join(block.inputs)}.",
                )
        inner = over
        if block.fold:
            inner = self._fold_group(node.get("over"))
        for key, wanted in block.inputs.items():
            if key not in node:
                if not wanted.optional:
                    self.refuse((*at, key), f"{block.name} needs its input {key}.")
                continue
            self._given(block, (*at, key), node[key], wanted, inner if wanted.per_run else over)

    def _given(
        self, block: Block, at: Path, given: object, wanted: BlockInput, over: str | None
    ) -> None:
        if wanted.least is None:
            if isinstance(given, list):
                self.refuse(at, f"This input of {block.name} takes one node, not a list.")
                return
            self.node(at, given, wanted, over)
            return
        if not isinstance(given, list):
            self.refuse(at, f"This input of {block.name} takes a list of {_count(wanted)}.")
            return
        if len(given) < wanted.least or (wanted.most is not None and len(given) > wanted.most):
            self.refuse(at, f"{block.name} takes {_count(wanted)} here, not {len(given)}.")
        for index, each in enumerate(given):
            self.node((*at, index), each, wanted, over)

    def _fold_group(self, given: object) -> str | None:
        leaf = _leaf(given) if isinstance(given, dict) else None
        if leaf is None or leaf[0] != "runs" or not isinstance(leaf[1], str):
            return None
        return leaf[1]

    def _runs(self, at: Path, node: object) -> None:
        leaf = _leaf(node) if isinstance(node, dict) else None
        if leaf is None or leaf[0] != "runs":
            self.refuse(at, "Give the group whose runs this folds, as {runs: <group>}.")
            return
        named = leaf[1]
        if not isinstance(named, str) or named not in self.groups:
            self.refuse(at, f"There is no group called {named}.")
            return
        self.reading.groups.add(named)

    def _column(self, at: Path, named: object, over: str | None) -> None:
        if (
            not isinstance(named, list)
            or len(named) != 2
            or not all(isinstance(part, str) for part in named)
        ):
            self.refuse(at, "Give a run's column as {run: [<group>, <column>]}.")
            return
        group, column = named
        if over is None:
            self.refuse(
                at,
                "A run's column is read only inside a fold over its group's runs, in its of "
                "or its where, such as {block: max_runs, over: {runs: "
                f"{group}}}, of: {{run: [{group}, {column}]}}}}.",
            )
            return
        if group != over:
            self.refuse(
                at,
                f"This fold reads the runs of {over}, so read their columns as "
                f"{{run: [{over}, {column}]}}.",
            )
            return
        if column not in self.columns:
            self.refuse(
                at,
                f"A run has no column called {column}. Its columns are {', '.join(self.columns)}.",
            )
            return
        reason = self.columns[column]
        if reason is not None:
            self.refuse(at, reason)
            return
        self.reading.groups.add(group)
        self.reading.columns.add((group, column))

    def _weights(self, at: Path, node: object) -> None:
        if not isinstance(node, dict) or len(node) != 1:
            self.refuse(
                at,
                "Give the weights by one repeated input's values, such as "
                "{input: {tests/7.in: 3}}; a run they do not name weighs 1.",
            )
            return
        [(name, weights)] = node.items()
        taken = self.keys.get(name) if isinstance(name, str) else None
        if taken is None:
            self.refuse(
                (*at, str(name)),
                f"{name} is not an input the task repeats and fills itself, so it cannot "
                "weigh a run.",
            )
            return
        if not isinstance(weights, dict) or not weights:
            self.refuse((*at, name), "Give each value's weight, such as {tests/7.in: 3}.")
            return
        for value, weight in weights.items():
            where = (*at, name, str(value))
            if not any(same_value(value, each) for each in taken):
                self.refuse(where, f"No run has {name} {value}.")
            elif not is_number(weight) or weight <= 0:
                self.refuse(where, "A weight is a number more than 0.")


_FACT_WHY = {
    "attempt": "depends on the entrant's other submissions",
    "marked": "changes until the task closes",
}


def _count(wanted: BlockInput) -> str:
    if wanted.most == wanted.least:
        return f"exactly {wanted.least} inputs"
    return f"at least {wanted.least} inputs"


def _leaf(node: Mapping[object, object]) -> tuple[str, object] | None:
    """A leaf's kind and what it names, when `node` is one."""
    if len(node) != 1:
        return None
    [(key, named)] = node.items()
    if key not in LEAVES:
        return None
    return str(key), named


def _loops(reads: Mapping[str, Reads]) -> Found:
    """A problem at each value that reads itself through others."""
    found: Found = []
    for start in reads:
        path = _cycle(start, reads)
        if path is not None:
            found.append(
                (
                    ("values", start),
                    f"{start} reads itself, through {' -> '.join(path)}. A value cannot read "
                    "one that reads it.",
                )
            )
    return found


def _cycle(start: str, reads: Mapping[str, Reads]) -> list[str] | None:
    """The values from `start` back to itself, when it reads itself."""
    waiting: list[list[str]] = [[start]]
    seen: set[str] = set()
    while waiting:
        path = waiting.pop()
        for name in sorted(reads.get(path[-1], Reads()).values):
            if name == start:
                return [*path, name]
            if name not in seen:
                seen.add(name)
                waiting.append([*path, name])
    return None


__all__ = [
    "Reads",
    "ValuesCheck",
    "check_names",
    "check_values",
    "same_value",
]
