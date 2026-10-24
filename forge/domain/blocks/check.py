"""The structural check of a task's values against the catalogue (the
leaderboards note's 6.1, SIMPLER-CORE.md 9.5): every node a block the
catalogue has or a leaf, each input of the kind its block takes, every name
a leaf reads existing, a run's column read only inside a fold over its
group, and no value reading itself through others. Group and value names
are one set, none named like a fact, and `all` is every run's. A block with
more than one form is checked against the one its node fits, by the inputs
it is given and whether each is a group's runs; a node fitting none is
refused naming the forms. A form or a leaf only a leaderboard's column may use
(`Scope.LEADERBOARD`, `LEADERBOARD_LEAVES`) is refused in a task's value, naming
where it belongs. A tree is at most `MOST_NODES` nodes and
`MOST_DEPTH` deep, and a task has at most `MOST_VALUES` values and
`MOST_GROUPS` groups, each measured before anything is walked, since every
read of a leaderboard works every value of every submission out.

A value holds one `entered` block at most, its bounds numbers written
out, the lower first, and one `exec` block at most, its program a folder,
its language one `unicon/compile@v3` takes and its limits numbers more
than 0; the save checks the folder and its `program.yaml`
(`run_task`).

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
    ENTERED,
    EXEC,
    FACTS,
    LANGUAGES,
    LEAVES,
    NUMBERS,
    PICKS,
    SKIP,
    TASK_FACTS,
    TASK_LEAVES,
    Block,
    BlockInput,
    Form,
    Kind,
    Scope,
)
from forge.domain.blocks.formula_words import unreadable_problem
from forge.domain.blocks.tree_reads import reached
from forge.domain.definitions import VALUE_NAME
from forge.domain.yaml_models import Path, is_number

Found = list[tuple[Path, str]]

MOST_NODES = 1000
"""The most nodes a value's tree holds, every number, leaf and block."""
MOST_DEPTH = 64
"""The most nodes deep a value's tree is, the top one at depth 1."""
MOST_VALUES = 200
"""The most values a task has, so the trees a submission is worked out
from hold at most `MOST_VALUES` times `MOST_NODES` nodes."""
MOST_GROUPS = 200
"""The most groups a task has, besides `all`."""

_LEAF_WORDS = (
    "Give a number, a block such as {block: add, of: [...]}, or what it reads: "
    "{runs: <group>}, {run: [<group>, <column>]}, {value: <name>} or {fact: <name>}."
)


@dataclass(frozen=True, slots=True)
class Reads:
    """What one value's tree reads directly: groups, through a fold's
    `over` or a run's column; other values; facts; each run column, by its
    group; and the blocks it uses that read beyond its runs (`READING`).
    """

    groups: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    facts: frozenset[str] = frozenset()
    columns: frozenset[tuple[str, str]] = frozenset()
    blocks: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ValuesCheck:
    """Every problem with a task's values, each at its path below
    `scoring`, and what each value that is there reads directly.
    """

    problems: Found
    reads: Mapping[str, Reads] = field(default_factory=dict)
    kinds: Mapping[str, Kind] = field(default_factory=dict)

    def through(self, name: str) -> Reads:
        """What the value `name` reads, directly or through the values it
        reads, itself among them.
        """
        groups: set[str] = set()
        facts: set[str] = set()
        columns: set[tuple[str, str]] = set()
        blocks: set[str] = set()
        values = reached([name], {key: read.values for key, read in self.reads.items()})
        for current in values:
            read = self.reads.get(current, Reads())
            groups |= read.groups
            facts |= read.facts
            columns |= read.columns
            blocks |= read.blocks
        return Reads(
            frozenset(groups),
            frozenset(values),
            frozenset(facts),
            frozenset(columns),
            frozenset(blocks),
        )


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
    `all`, none named like a fact, none a word a formula reads as an
    operator or a constant, and none both a group and a value.
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
            elif (problem := unreadable_problem(name)) is not None:
                found.append((at, problem))
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
    texts: Collection[str] = (),
) -> ValuesCheck:
    """Check each of a task's values, `groups` the task's groups besides
    `all`, `columns` each column of a run, with none for a number a fold may
    read and the reason for any other, `texts` the columns that are text a
    formula may read (`verdict` and a repeated input's text; a step's text
    output is not among them), and `keys` each repeated input the organiser
    fills with the values its runs take, which `weights` may name.
    """
    too_many: Found = [
        (
            (kind,),
            f"This task has {len(names)} {kind}. A task has at most {most}, so working "
            "its values out stays quick.",
        )
        for kind, names, most in (
            ("groups", groups, MOST_GROUPS),
            ("values", values, MOST_VALUES),
        )
        if len(names) > most
    ]
    if too_many:
        return ValuesCheck(too_many)
    checker = _Checker(values, set(groups) | {ALL}, columns, keys, set(texts))
    reads: dict[str, Reads] = {}
    for name, tree in values.items():
        checker.reading = _Reading()
        nodes, depth = tree_size(tree)
        if nodes > MOST_NODES or depth > MOST_DEPTH:
            checker.refuse(
                ("values", name),
                f"This value has {nodes} parts and is {depth} deep. A value has at most "
                f"{MOST_NODES} parts and is at most {MOST_DEPTH} deep, so working it out "
                "stays quick.",
            )
        else:
            checker.node(("values", name), tree, BlockInput(Kind.NUMBER_OR_TEXT), None)
        found = checker.reading
        reads[name] = Reads(
            frozenset(found.groups),
            frozenset(found.values),
            frozenset(found.facts),
            frozenset(found.columns),
            frozenset(found.blocks),
        )
        for block in READING:
            if found.blocks.get(block, 0) > 1:
                checker.refuse(
                    ("values", name),
                    f"This value uses {block} {found.blocks[block]} times. A value uses it "
                    f"once at most, so {_ONE_WHY[block]}.",
                )
        if EXEC in found.blocks and len(name) > MOST_EXEC_NAME:
            checker.refuse(
                ("values", name),
                f"A value using exec has a name of at most {MOST_EXEC_NAME} characters, since "
                "its program's steps are named after it.",
            )
    checker.problems.extend(_loops(reads))
    kinds = {name: checker._value_kind(name, set()) for name in values}
    return ValuesCheck(checker.problems, reads, kinds)


@dataclass
class _Reading:
    groups: set[str] = field(default_factory=set)
    values: set[str] = field(default_factory=set)
    facts: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)
    blocks: dict[str, int] = field(default_factory=dict)


READING = (ENTERED, EXEC)
"""The blocks that read beyond a submission's runs and facts, each once in
a value at most, which `Reads.blocks` records."""
_ONE_WHY = {
    ENTERED: "a mark entered for it is the value's own",
    EXEC: "its program's steps are named after it",
}
MOST_EXEC_NAME = 34
"""The longest name of a value using `exec`, whose program's steps are
named `build-<value>` and `exec-<value>`, at most 40 characters."""


class _Checker:
    def __init__(
        self,
        values: Mapping[str, object],
        groups: set[str],
        columns: Mapping[str, str | None],
        keys: Mapping[str, Sequence[object]],
        texts: set[str],
    ) -> None:
        self.values = values
        self.groups = groups
        self.columns = columns
        self.keys = keys
        self.texts = texts
        self.value_kinds: dict[str, Kind] = {}
        self.problems: Found = []
        self.reading = _Reading()

    def refuse(self, at: Path, message: str) -> None:
        self.problems.append((at, message))

    def node(self, at: Path, node: object, wanted: BlockInput, over: str | None) -> None:
        """Check one node given to an input that takes `wanted`, `over` the
        group whose runs the innermost fold around it reads them once for
        each of, when the input is read per run.
        """
        if wanted.plain:
            if isinstance(node, bool) or not is_number(node):
                self.refuse(at, "Give a number written out here, such as 0 or 10.")
            return
        if isinstance(node, dict) and wanted.kind in (
            Kind.NUMBER,
            Kind.TEXT,
            Kind.NUMBER_OR_TEXT,
            Kind.EITHER,
            Kind.RUN,
        ):
            got = self._tree_gives(node, set())
            if not _fits_kind(wanted.kind, got):
                self.refuse(at, _kind_message(node, got))
                return
        match wanted.kind:
            case Kind.RUNS:
                self._runs(at, node)
            case Kind.RESULTS | Kind.NAME | Kind.DIRECTIONS:
                self.refuse(at, ONLY_LEADERBOARDS)
            case Kind.WEIGHTS:
                self._weights(at, node)
            case Kind.MISSING:
                if node != SKIP and not is_number(node):
                    self.refuse(
                        at,
                        f"Give what a missing report counts as: {SKIP}, to leave the run out, "
                        "or a number.",
                    )
            case Kind.RUN:
                if not isinstance(node, dict) or "block" not in node:
                    self.refuse(at, "Pick a run with first, last, highest or lowest.")
                    return
                self._block(at, node, over)
            case Kind.NUMBER | Kind.EITHER | Kind.NUMBER_OR_TEXT | Kind.TEXT:
                self._number(
                    at,
                    node,
                    over,
                    text=wanted.kind in (Kind.NUMBER_OR_TEXT, Kind.TEXT),
                )
            case Kind.PATH:
                if not _folder(node):
                    self.refuse(
                        at,
                        "Give the folder of the task that holds the program and its "
                        "program.yaml, such as programs/mean/.",
                    )
            case Kind.LANGUAGE:
                if node not in LANGUAGES:
                    self.refuse(at, f"Give the program's language, one of {', '.join(LANGUAGES)}.")

    def _number(self, at: Path, node: object, over: str | None, *, text: bool = False) -> None:
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
        leaf = leaf_of(node)
        if leaf is None:
            self.refuse(at, _LEAF_WORDS)
            return
        kind, named = leaf
        if kind not in TASK_LEAVES or (kind == "value" and isinstance(named, list)):
            self.refuse(at, ONLY_LEADERBOARDS)
            return
        if kind == "text":
            if text and isinstance(named, str):
                return
            self.refuse(
                at,
                'Give text in quotes, such as "none".'
                if text
                else "This is text, and this reads a number.",
            )
            return
        match kind:
            case "runs":
                self.refuse(
                    at,
                    "A group's runs give no number by themselves. Fold them, such as "
                    f"{{block: count, over: {{runs: {named}}}}}.",
                )
            case "run":
                self._column(at, named, over, text=text)
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
        form = form_of(block, node, Scope.LEADERBOARD)
        if form is None:
            self.refuse(at, forms_words(block, Scope.LEADERBOARD))
            return
        if form.scope is Scope.LEADERBOARD:
            self.refuse(
                at,
                f"{block.name} given {form.given} is for a leaderboard's column. "
                f"{ONLY_LEADERBOARDS}",
            )
            return
        for key in node:
            if key != "block" and key not in form.inputs:
                self.refuse(
                    (*at, str(key)),
                    f"{block.name} has no input called {key}. It takes {', '.join(form.inputs)}.",
                )
        if block.name in READING:
            self.reading.blocks[block.name] = self.reading.blocks.get(block.name, 0) + 1
        if block.name == ENTERED:
            self._bounds(at, node)
        if block.name == EXEC:
            for key in ("time_limit", "memory_limit"):
                given = node.get(key)
                if is_number(given) and not isinstance(given, bool) and Decimal(str(given)) <= 0:
                    self.refuse((*at, key), f"{key} is a number more than 0.")
        inner = over
        if form.fold:
            inner = self._fold_group(node.get("over"))
        elif form.gives is Kind.RUN:
            inner = self._fold_group(node.get("from"))
        self._mixed(at, form, node)
        for key, wanted in form.inputs.items():
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

    def _bounds(self, at: Path, node: Mapping[str, object]) -> None:
        least, most = node.get("at_least"), node.get("at_most")
        if (
            is_number(least)
            and is_number(most)
            and not isinstance(least, bool)
            and not isinstance(most, bool)
            and Decimal(str(least)) > Decimal(str(most))
        ):
            self.refuse(
                (*at, "at_most"),
                f"at_most is {most}, below at_least, {least}. Give bounds a mark fits between.",
            )

    def _fold_group(self, given: object) -> str | None:
        leaf = leaf_of(given) if isinstance(given, dict) else None
        if leaf is None or leaf[0] != "runs" or not isinstance(leaf[1], str):
            return None
        return leaf[1]

    def _runs(self, at: Path, node: object) -> None:
        leaf = leaf_of(node) if isinstance(node, dict) else None
        if leaf is None or leaf[0] != "runs":
            self.refuse(at, "Give the group whose runs this folds, as {runs: <group>}.")
            return
        named = leaf[1]
        if not isinstance(named, str) or named not in self.groups:
            self.refuse(at, f"There is no group called {named}.")
            return
        self.reading.groups.add(named)

    def _mixed(self, at: Path, form: Form, node: Mapping[str, object]) -> None:
        """Refuse `==`, `!=`, `if` or `otherwise` given a number and a text."""
        kinds: list[Kind] = []
        for key, wanted in form.inputs.items():
            if wanted.kind is not Kind.NUMBER_OR_TEXT or key not in node:
                continue
            given = node[key]
            for each in given if isinstance(given, list) else [given]:
                kinds.append(self._tree_gives(each, set()))
        if Kind.TEXT in kinds and Kind.NUMBER in kinds:
            self.refuse(at, "Give two numbers or two texts, not one of each.")

    def _column(self, at: Path, named: object, over: str | None, *, text: bool = False) -> None:
        if (
            isinstance(named, list)
            and len(named) == 2
            and isinstance(named[0], dict)
            and isinstance(named[1], str)
        ):
            self._picked(at, named[0], named[1], text=text)
            return
        if (
            not isinstance(named, list)
            or len(named) != 2
            or not all(isinstance(part, str) for part in named)
        ):
            self.refuse(
                at,
                "Give a run's column as {run: [<group>, <column>]}, or a picked run's as "
                "{run: [<pick>, <column>]}.",
            )
            return
        group, column = named
        if over is None:
            self.refuse(
                at,
                "A run's column is read only inside a fold over its group's runs, in its of "
                "or its where, such as {block: max, over: {runs: "
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
        if not self._readable(at, column, text=text):
            return
        self.reading.groups.add(group)
        self.reading.columns.add((group, column))

    def _picked(self, at: Path, pick: Mapping[str, object], column: str, *, text: bool) -> None:
        if "block" not in pick:
            self.refuse(at, "Give a picked run's column as {run: [<pick>, <column>]}.")
            return
        self.node((*at, "run", 0), pick, BlockInput(Kind.RUN), None)
        group = self._fold_group(pick.get("from"))
        if not self._readable(at, column, text=text):
            return
        if group is not None:
            self.reading.groups.add(group)
            self.reading.columns.add((group, column))

    def _readable(self, at: Path, column: str, *, text: bool) -> bool:
        if column not in self.columns:
            self.refuse(
                at,
                f"A run has no column called {column}. Its columns are {', '.join(self.columns)}.",
            )
            return False
        reason = self.columns[column]
        if reason is not None and not (text and column in self.texts):
            self.refuse(at, reason)
            return False
        return True

    def _tree_gives(self, node: object, seen: set[str]) -> Kind:
        """What `node` gives, from the form and its children: a number when
        it is not a tree that gives text or a run."""
        if isinstance(node, bool) or not isinstance(node, dict):
            return Kind.NUMBER
        if set(node) == {"text"} and isinstance(node.get("text"), str):
            return Kind.TEXT
        if "block" in node and isinstance(node["block"], str):
            block = CATALOGUE.get(node["block"])
            form = form_of(block, node, Scope.LEADERBOARD) if block is not None else None
            if form is None:
                return Kind.NUMBER
            if form.gives is Kind.RUN:
                return Kind.RUN
            if form.gives is Kind.NUMBER:
                return Kind.NUMBER
            if form.gives in (Kind.NUMBER_OR_TEXT, Kind.EITHER):
                kinds = [
                    self._tree_gives(each, seen)
                    for key, wanted in form.inputs.items()
                    if wanted.kind is Kind.NUMBER_OR_TEXT and key in node
                    for each in (node[key] if isinstance(node[key], list) else [node[key]])
                ]
                if Kind.TEXT in kinds and Kind.NUMBER not in kinds and Kind.RUN not in kinds:
                    return Kind.TEXT
                if Kind.RUN in kinds:
                    return Kind.RUN
            return Kind.NUMBER
        leaf = leaf_of(node)
        if leaf is None:
            return Kind.NUMBER
        kind, named = leaf
        if kind == "value" and isinstance(named, str):
            return self._value_kind(named, seen)
        if kind == "run":
            return self._column_kind(named)
        return Kind.NUMBER

    def _value_kind(self, name: str, seen: set[str]) -> Kind:
        cached = self.value_kinds.get(name)
        if cached is not None:
            return cached
        if name not in self.values or name in seen:
            return Kind.NUMBER
        seen.add(name)
        kind = self._tree_gives(self.values[name], seen)
        seen.remove(name)
        self.value_kinds[name] = kind
        return kind

    def _column_kind(self, named: object) -> Kind:
        column = named[1] if isinstance(named, list) and len(named) == 2 else None
        if isinstance(column, str) and column in self.texts:
            return Kind.TEXT
        return Kind.NUMBER

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


ONLY_LEADERBOARDS = (
    "A task's value reads one submission's runs and facts; a task's results, a "
    "submission's results and a leaderboard's columns are read on a leaderboard only."
)
_FACT_WHY = {
    "attempt": "depends on the entrant's other submissions",
    "marked": "changes until the task closes",
}


def form_of(
    block: Block, node: Mapping[str, object], leaving_out: Scope | None = None
) -> Form | None:
    """The form of `block` a node means: the one its inputs fit, by name and
    by whether each is a group's runs or a task's results; else the only
    form usable where the block is, those whose scope is not `leaving_out`,
    so its inputs are checked one by one; none when they fit no form of
    several.
    """
    for form in block.forms:
        if _fits(form, node):
            return form
    usable = [form for form in block.forms if form.scope is not leaving_out]
    return usable[0] if len(usable) == 1 else None


def _fits(form: Form, node: Mapping[str, object]) -> bool:
    given = [key for key in node if key != "block"]
    if any(key not in form.inputs for key in given):
        return False
    if any(key not in node for key, wanted in form.inputs.items() if not wanted.optional):
        return False
    for key in given:
        wanted = form.inputs[key]
        value = node[key]
        if isinstance(value, list) != (wanted.least is not None):
            return False
        for each in value if isinstance(value, list) else [value]:
            leaf = leaf_of(each) if isinstance(each, dict) else None
            gathered = leaf[0] if leaf is not None and leaf[0] in _GATHERED else None
            if gathered != _GATHERED_BY.get(wanted.kind):
                return False
            hint = _hint(each)
            if wanted.kind is Kind.NUMBER_OR_TEXT and hint == "submission":
                return False
            if wanted.kind is Kind.EITHER and hint == "text":
                return False
    return True


_GATHERED = ("runs", "results")
"""The leaves that give many, which tell a fold's forms apart."""
_GATHERED_BY = {Kind.RUNS: "runs", Kind.RESULTS: "results"}
_SUBMISSION_BLOCKS = frozenset((*PICKS, "best_each"))


def _hint(node: object) -> str | None:
    """What a child is, when that chooses `otherwise`'s form: text written
    out, or a block that gives a submission's results."""
    if not isinstance(node, dict):
        return None
    if set(node) == {"text"}:
        return "text"
    if node.get("block") in _SUBMISSION_BLOCKS:
        return "submission"
    return None


def _kind_message(node: object, got: Kind) -> str:
    """Why `node`, which gives `got`, is refused where a different kind is read."""
    if got is Kind.TEXT:
        named = node.get("value") if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get("run"), list):
            column = node["run"][1]
            if isinstance(column, str):
                named = column
        what = named if isinstance(named, str) else "This"
        return f"{what} is text, and this reads a number."
    if got is Kind.RUN:
        return "This gives a run. Read one of its columns, as {run: [<pick>, <column>]}."
    return "This gives a number, and this reads a run."


def _fits_kind(wanted: Kind, got: Kind) -> bool:
    """Whether an input that takes `wanted` may be given something that
    gives `got`."""
    if wanted is Kind.NUMBER:
        return got is Kind.NUMBER
    if wanted is Kind.TEXT:
        return got is Kind.TEXT
    if wanted is Kind.NUMBER_OR_TEXT:
        return got in (Kind.NUMBER, Kind.TEXT)
    if wanted is Kind.EITHER:
        return got in (Kind.NUMBER, Kind.SUBMISSION, Kind.EITHER)
    if wanted is Kind.RUN:
        return got is Kind.RUN
    return True


def forms_words(block: Block, leaving_out: Scope) -> str:
    """What `block` takes, naming only the forms usable where the block is:
    those whose scope is not `leaving_out`."""
    forms = [form for form in block.forms if form.scope is not leaving_out]
    if not forms:
        where = "a leaderboard's column" if leaving_out is Scope.TASK else "a task's value"
        return f"{block.name} is for {where} only."
    takes = "; ".join(
        f"given {form.given}, "
        + ", ".join(
            f"{key}{' (optional)' if wanted.optional else ''}"
            for key, wanted in form.inputs.items()
        )
        for form in forms
    )
    return (
        f"{block.name} is given {' or '.join(form.given for form in forms)}, and these "
        f"inputs fit no form of it. It takes, {takes}."
    )


def tree_size(tree: object) -> tuple[int, int]:
    """How many nodes a value's tree holds and how deep it is, walked
    without recursion: every number, leaf and block a block's inputs give,
    and nothing inside a leaf or a `weights` mapping.
    """
    nodes = deepest = 0
    waiting: list[tuple[object, int]] = [(tree, 1)]
    while waiting:
        node, depth = waiting.pop()
        if isinstance(node, list):
            waiting.extend((each, depth) for each in node)
            continue
        nodes += 1
        deepest = max(deepest, depth)
        if isinstance(node, dict) and "block" in node:
            waiting.extend((given, depth + 1) for key, given in node.items() if key != "block")
    return nodes, deepest


def _count(wanted: BlockInput) -> str:
    if wanted.most == wanted.least:
        return f"exactly {wanted.least} inputs"
    return f"at least {wanted.least} inputs"


def _folder(node: object) -> bool:
    """Whether `node` names a folder of the task: a relative path ending in
    `/`, no segment empty, `.` or `..`."""
    if not isinstance(node, str) or not node.endswith("/") or node.startswith("/"):
        return False
    return all(segment not in ("", ".", "..") for segment in node[:-1].split("/"))


def leaf_of(node: Mapping[object, object]) -> tuple[str, object] | None:
    """A leaf's kind and what it names, when `node` is one."""
    if len(node) != 1:
        return None
    [(key, named)] = node.items()
    if key not in LEAVES:
        return None
    return str(key), named


def direct_reads(tree: object) -> tuple[set[str], str | None]:
    """The values a task's value's tree reads directly, and the first thing
    it reads directly that is not another value or a number, in words, if
    any: a fact, a group's runs, a program, a mark or a fallback. A leaderboard's
    `best_each` works out again only a tree with none (the leaderboards
    note's 5.4), which the save checks and a read follows alike."""
    found: set[str] = set()
    waiting = [tree]
    while waiting:
        node = waiting.pop()
        if isinstance(node, list):
            waiting.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if "block" not in node:
            leaf = leaf_of(node)
            if leaf is not None and leaf[0] == "value":
                found.add(str(leaf[1]))
            elif leaf is not None and leaf[0] == "fact":
                return found, f"the fact {leaf[1]}"
            else:
                return found, "a group's runs"
            continue
        name = str(node["block"])
        block = CATALOGUE.get(name)
        form = form_of(block, node) if block is not None else None
        if name == EXEC:
            return found, "a program's number, through exec"
        if name == ENTERED:
            return found, "a mark staff enter, through entered"
        if name == "otherwise":
            return found, "a fallback for a missing number, through otherwise"
        if form is None or form.given != NUMBERS:
            return found, f"a group's runs, through {name}"
        waiting.extend(given for key, given in node.items() if key != "block")
    return found, None


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
    "MOST_DEPTH",
    "MOST_EXEC_NAME",
    "MOST_GROUPS",
    "MOST_NODES",
    "MOST_VALUES",
    "READING",
    "Reads",
    "ValuesCheck",
    "check_names",
    "check_values",
    "direct_reads",
    "form_of",
    "forms_words",
    "leaf_of",
    "same_value",
    "tree_size",
]
