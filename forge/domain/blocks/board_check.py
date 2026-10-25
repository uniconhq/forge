"""The board half of the structural check (SIMPLER-CORE.md 10.1, the
leaderboards note's 5.3 to 5.5 and 6.2): a contest's leaderboards against
the catalogue, as far as `contest.yaml` alone says. What needs a task's own
file, such as whether `islands.solved` is one of its values, is checked
across files (`forge.domain.board_checks`).

- **Kinds.** Every node is a block the catalogue has in a form a board may
  use (`Scope`), or a leaf a board reads; each input is given what it
  takes: a number, a task's results (`{results: <task>}`), a submission's
  results, either, or `best_each`'s value and directions. A pick gives a
  submission's results, `otherwise` what both its inputs give, and every
  other block a number.
- **Names.** The contest's tasks, its board keys and a board's columns are
  one set of names, so `a` in `a.b` is always known; two names that read
  the same once each `-` is written `_` are refused too, since formula
  text writes them so.
- **`t.v`,** `{value: [<task>, <name>]}`, is read only inside a `where`, a
  `by` or a fold's `of` of a block over that task's results, and means the
  submission the innermost such block is looking at.
- **No loops,** within a board or across boards, and a column read from
  another board is one that board has.
- **`sort` and `display`:** each names a column of the board, once, never
  a pick column, and every sort key is displayed.

A board whose check finds anything, or that reads a column of such a
board, is not worked out on read (`BoardsCheck.readable`); the contest's
save refuses it first. `BoardsCheck.order` gives every column of the
readable boards after each column it reads, so they are worked out in one
pass with no recursion between them. `BoardsCheck.through` gives, for each
column giving a submission's results, every value and fact any column of
any board reads through it, which its picks read as well (guarantee 5);
`BoardsCheck.hiding` gives the same read by the boards a read works out
alone, which hide its picks where held back (stage 5 section 6, item 98).

Each column's tree is at most `MOST_NODES` nodes and `MOST_DEPTH` deep,
as a task's value is, a board has at most `MOST_COLUMNS` columns, and a
contest at most `MOST_LEADERBOARDS` boards, since every read of a board
works out each column for each row.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

from forge.domain.blocks.catalogue import (
    ASC,
    CATALOGUE,
    DESC,
    FACTS,
    PICKS,
    SKIP,
    BlockInput,
    Kind,
    Scope,
)
from forge.domain.blocks.check import (
    MOST_DEPTH,
    MOST_NODES,
    Found,
    form_of,
    forms_words,
    leaf_of,
    tree_size,
)
from forge.domain.definitions import VALUE_NAME, Leaderboard
from forge.domain.yaml_models import Path, is_number

MOST_COLUMNS = 100
"""The most columns one board has."""
MOST_LEADERBOARDS = 20
"""The most boards a contest has."""

BoardColumn = tuple[str, str]
"""A column by its board's key and its name."""

_LEAF_WORDS = (
    "Give a number, a block such as {block: add, of: [...]}, or what it reads: "
    "{value: [<task or pick column>, <value or fact>]}, {column: <name>} or "
    "{column: [<board>, <name>]}."
)


@dataclass(frozen=True, slots=True)
class ColumnShape:
    """What one column is, the same for every row: whether it gives a number
    or a submission's results, and for the latter which tasks' submissions;
    each task's value or fact it reads directly, through a pick or a
    `best_each` included, by task; the tasks whose results it reads; and
    the columns it reads, by board.
    """

    kind: Kind = Kind.NUMBER
    of_tasks: frozenset[str] = frozenset()
    reads: frozenset[tuple[str, str]] = frozenset()
    tasks: frozenset[str] = frozenset()
    columns: frozenset[BoardColumn] = frozenset()


@dataclass(frozen=True, slots=True)
class BoardsCheck:
    """Every problem with a contest's leaderboards, each at its path from
    the top of `contest.yaml`; each column's shape, by board; the boards
    that are worked out on read; their columns in an order that has each
    after every column it reads; for each column giving a submission's
    results, the values and facts any column reads through it, from any
    board, which its picks pass over a submission still waiting on
    (guarantee 5); and the same by the board whose columns read them.
    """

    problems: Found
    shapes: Mapping[str, Mapping[str, ColumnShape]] = field(default_factory=dict)
    readable: frozenset[str] = frozenset()
    order: tuple[BoardColumn, ...] = ()
    through: Mapping[BoardColumn, frozenset[str]] = field(default_factory=dict)
    through_by: Mapping[str, Mapping[BoardColumn, frozenset[str]]] = field(default_factory=dict)

    def hiding(self) -> dict[BoardColumn, frozenset[str]]:
        """What the boards `order` works out read through each column giving
        a submission's results: a pick is hidden where one of these is held
        back, so a board not read, staff's among them, hides no pick of a
        board that is (item 98). Its picks still pass over a submission
        waiting on anything in `through`, the same on every board."""
        boards = {board for board, _ in self.order}
        found: dict[BoardColumn, set[str]] = {}
        for board in boards:
            for column, names in self.through_by.get(board, {}).items():
                found.setdefault(column, set()).update(names)
        return {column: frozenset(names) for column, names in found.items()}

    def reads(self, task: str, name: str) -> list[str]:
        """The boards with a column reading `name` of `task`, directly or
        through the columns it reads, another board's among them."""
        return self._boards(lambda shape: (task, name) in shape.reads)

    def reading(self, task: str) -> list[str]:
        """The boards with a column reading the results of `task`, directly
        or through the columns it reads, another board's among them."""
        return self._boards(lambda shape: task in shape.tasks)

    def needed(self, boards: Collection[str]) -> BoardsCheck:
        """This, its `order` cut to the columns of `boards` and every column
        they read, so a read of some boards works out no other."""
        wanted = {(board, name) for board in boards for name in self.shapes.get(board, {})}
        waiting = list(wanted)
        while waiting:
            board, name = waiting.pop()
            for other in self.shapes[board][name].columns:
                if other not in wanted and other[1] in self.shapes.get(other[0], {}):
                    wanted.add(other)
                    waiting.append(other)
        return replace(self, order=tuple(each for each in self.order if each in wanted))

    def _boards(self, hit: Callable[[ColumnShape], bool]) -> list[str]:
        """The boards with a column that `hit`s, or reads one that does."""
        found = {
            (board, name)
            for board, columns in self.shapes.items()
            for name, shape in columns.items()
            if hit(shape)
        }
        grown = True
        while grown:
            more = {
                (board, name)
                for board, columns in self.shapes.items()
                for name, shape in columns.items()
                if (board, name) not in found and shape.columns & found
            }
            found |= more
            grown = bool(more)
        return [board for board in self.shapes if any(each[0] == board for each in found)]


def column_at(board: str, named: object) -> BoardColumn:
    """The column a `{column: ...}` leaf of `board` names: one of its own,
    or `[<board>, <name>]` another's."""
    if isinstance(named, list) and len(named) == 2:
        return (str(named[0]), str(named[1]))
    return (board, str(named))


def spelled(name: str) -> str:
    """A name as formula text writes it, each `-` as `_`."""
    return name.replace("-", "_")


def check_boards(boards: Mapping[str, Leaderboard], tasks: Sequence[str]) -> BoardsCheck:
    """Check a contest's `boards`, by key, against its `tasks`, by id."""
    if len(boards) > MOST_LEADERBOARDS:
        return BoardsCheck(
            [
                (
                    ("leaderboards",),
                    f"This contest has {len(boards)} leaderboards. A contest has at most "
                    f"{MOST_LEADERBOARDS}, so working them out stays quick.",
                )
            ]
        )
    problems: Found = []
    problems += _contest_names(boards, tasks)
    kinds = _kinds(boards)
    shapes: dict[str, dict[str, ColumnShape]] = {}
    through: dict[str, dict[BoardColumn, set[str]]] = {}
    troubled: set[str] = set()
    for key, board in boards.items():
        checker = _Checker(key, board, tasks, boards, kinds, through.setdefault(key, {}))
        checker.check()
        shapes[key] = checker.shapes
        if checker.problems:
            troubled.add(key)
        problems += checker.problems
    loops = _loops(shapes)
    for (key, column), path in loops.items():
        troubled.add(key)
        problems.append(
            (
                ("leaderboards", key, "columns", column),
                f"{column} reads itself, through {' -> '.join(_said(key, each) for each in path)}"
                ". A column cannot read one that reads it.",
            )
        )
    readable = _readable(shapes, troubled)
    by = {key: _through(boards, read) for key, read in through.items()}
    every: dict[BoardColumn, set[str]] = {}
    for read in by.values():
        for at, names in read.items():
            every.setdefault(at, set()).update(names)
    return BoardsCheck(
        problems,
        shapes,
        readable,
        _order(shapes, readable),
        {at: frozenset(names) for at, names in every.items()},
        by,
    )


def _said(board: str, column: BoardColumn) -> str:
    return column[1] if column[0] == board else f"{column[0]}.{column[1]}"


def _contest_names(boards: Mapping[str, Leaderboard], tasks: Sequence[str]) -> Found:
    """Board keys that read like a task or another key once `-` is `_`."""
    found: Found = []
    seen: dict[str, str] = {}
    for task in tasks:
        seen.setdefault(spelled(task), task)
    for key in boards:
        other = seen.get(spelled(key))
        if other is not None and other != key:
            found.append(
                (
                    ("leaderboards", key),
                    f"{key} reads as {other} in a formula, where each - is written _. "
                    "Name the leaderboard otherwise.",
                )
            )
        seen.setdefault(spelled(key), key)
    return found


def _kinds(boards: Mapping[str, Leaderboard]) -> dict[BoardColumn, tuple[Kind, frozenset[str]]]:
    """What each column gives, read from its top block alone: a pick's
    submission's results, with its task; what `otherwise` is given; another
    column's; else a number. Read before the full check, so a column may be
    read before the one it reads is checked; a loop reads as a number, and
    is refused on its own.
    """
    found: dict[BoardColumn, tuple[Kind, frozenset[str]]] = {}
    for key, board in boards.items():
        for name in board.columns:
            _kind_of(found, boards, (key, name))
    return found


def _kind_of(
    found: dict[BoardColumn, tuple[Kind, frozenset[str]]],
    boards: Mapping[str, Leaderboard],
    start: BoardColumn,
) -> tuple[Kind, frozenset[str]]:
    trail: list[BoardColumn] = []
    at = start
    while at not in found:
        board = boards.get(at[0])
        tree = board.columns.get(at[1]) if board is not None else None
        nodes, depth = tree_size(tree)
        if tree is None or nodes > MOST_NODES or depth > MOST_DEPTH or at in trail:
            found[at] = (Kind.NUMBER, frozenset())
            break
        trail.append(at)
        given = _top_kind(tree, at[0])
        if isinstance(given, _Same):
            at = given.column
            continue
        found[at] = given
        break
    for each in trail:
        found.setdefault(each, found[at])
    return found[start]


@dataclass(frozen=True, slots=True)
class _Same:
    """A tree that gives what another column gives."""

    column: BoardColumn


def _top_kind(tree: object, board: str) -> tuple[Kind, frozenset[str]] | _Same:
    """What a tree gives at its top, or the column it gives as it is."""
    node = tree
    while isinstance(node, dict):
        if "block" not in node:
            leaf = leaf_of(node)
            if leaf is not None and leaf[0] == "column":
                return _Same(column_at(board, leaf[1]))
            break
        name = node["block"]
        if name in PICKS:
            source = node.get("from")
            leaf = leaf_of(source) if isinstance(source, dict) else None
            if leaf is not None and leaf[0] == "results" and isinstance(leaf[1], str):
                return (Kind.SUBMISSION, frozenset({leaf[1]}))
            return (Kind.SUBMISSION, frozenset())
        if name == "otherwise" and isinstance(node.get("of"), list) and node["of"]:
            node = node["of"][0]
            continue
        break
    return (Kind.NUMBER, frozenset())


@dataclass
class _Reading:
    reads: set[tuple[str, str]] = field(default_factory=set)
    tasks: set[str] = field(default_factory=set)
    columns: set[BoardColumn] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Gives:
    kind: Kind
    tasks: frozenset[str] = frozenset()


_NUMBER = _Gives(Kind.NUMBER)


class _Checker:
    def __init__(
        self,
        key: str,
        board: Leaderboard,
        tasks: Sequence[str],
        boards: Mapping[str, Leaderboard],
        kinds: Mapping[BoardColumn, tuple[Kind, frozenset[str]]],
        through: dict[BoardColumn, set[str]],
    ) -> None:
        self.key = key
        self.board = board
        self.tasks = frozenset(tasks)
        self.boards = boards
        self.kinds = kinds
        self.through = through
        self.problems: Found = []
        self.shapes: dict[str, ColumnShape] = {}
        self.reading = _Reading()

    def refuse(self, at: Path, message: str) -> None:
        self.problems.append((at, message))

    def at(self, *path: str | int) -> Path:
        return ("leaderboards", self.key, *path)

    def check(self) -> None:
        board = self.board
        if len(board.columns) > MOST_COLUMNS:
            self.refuse(
                self.at("columns"),
                f"This leaderboard has {len(board.columns)} columns. A leaderboard has at most "
                f"{MOST_COLUMNS}, so working it out stays quick.",
            )
            return
        self._names()
        for name, tree in board.columns.items():
            self.reading = _Reading()
            at = self.at("columns", name)
            nodes, depth = tree_size(tree)
            if nodes > MOST_NODES or depth > MOST_DEPTH:
                self.refuse(
                    at,
                    f"This column has {nodes} parts and is {depth} deep. A column has at most "
                    f"{MOST_NODES} parts and is at most {MOST_DEPTH} deep, so working it out "
                    "stays quick.",
                )
                gives = _NUMBER
            else:
                gives = self.node(at, tree, BlockInput(Kind.EITHER), ()) or _NUMBER
            found = self.reading
            self.shapes[name] = ColumnShape(
                gives.kind,
                gives.tasks,
                frozenset(found.reads),
                frozenset(found.tasks),
                frozenset(found.columns),
            )
        self._sort_and_display()

    def _names(self) -> None:
        taken: dict[str, str] = {}
        for each in [*self.tasks, *self.boards]:
            taken.setdefault(spelled(each), each)
        for name in self.board.columns:
            at = self.at("columns", name)
            if name in self.tasks or name in self.boards:
                what = "a task" if name in self.tasks else "a leaderboard"
                self.refuse(
                    at,
                    f"{name} is {what} of the contest, and tasks, leaderboards and a "
                    "leaderboard's columns share one set of names, so a.b always reads one "
                    "thing. Name the column otherwise.",
                )
                continue
            other = taken.get(spelled(name))
            if other is not None:
                self.refuse(
                    at,
                    f"{name} reads as {other} in a formula, where each - is written _. Name the "
                    "column otherwise.",
                )
                continue
            taken[spelled(name)] = name

    def _sort_and_display(self) -> None:
        board = self.board
        shown: list[str] = []
        for index, (name, _) in enumerate(board.display):
            if self._listed(self.at("display", index), name, shown, "displays"):
                shown.append(name)
        sorted_by: list[str] = []
        for index, (name, _) in enumerate(board.sort):
            at = self.at("sort", index)
            if not self._listed(at, name, sorted_by, "sorts by"):
                continue
            sorted_by.append(name)
            if name not in shown:
                self.refuse(
                    at,
                    f"{name} is a sort key but not displayed. Display every column the rows are "
                    "sorted by, so an entrant can see why one row is above another.",
                )

    def _listed(self, at: Path, name: str, listed: list[str], does: str) -> bool:
        if name not in self.board.columns:
            self.refuse(at, f"This leaderboard has no column called {name}.")
            return False
        if name in listed:
            self.refuse(at, f"This leaderboard already {does} {name} above. Remove this one.")
            return False
        shape = self.shapes.get(name)
        if shape is not None and shape.kind is Kind.SUBMISSION:
            self.refuse(
                at,
                f"{name} gives a submission's results, not a number, so it is read by other "
                f"columns, such as {{value: [{name}, <value>]}}, and is not displayed or sorted "
                "by.",
            )
            return False
        return True

    def node(
        self, at: Path, node: object, wanted: BlockInput, scope: tuple[str, ...]
    ) -> _Gives | None:
        """Check one node given to an input taking `wanted`, `scope` the tasks
        whose results the blocks around it are looking at, innermost last;
        what it gives, or none when it is refused.
        """
        match wanted.kind:
            case Kind.NAME:
                leaf = leaf_of(node) if isinstance(node, dict) else None
                if (
                    leaf is None
                    or leaf[0] != "name"
                    or not isinstance(leaf[1], str)
                    or not VALUE_NAME.match(leaf[1])
                ):
                    self.refuse(at, "Give the task's value to work out again, as {name: total}.")
                return None
            case Kind.DIRECTIONS:
                self._directions(at, node)
                return None
            case Kind.MISSING:
                if node != SKIP and (isinstance(node, bool) or not is_number(node)):
                    self.refuse(
                        at,
                        f"Give what a missing input counts as: {SKIP}, to leave the submission "
                        "out, or a number.",
                    )
                return None
            case Kind.RESULTS:
                self._results(at, node)
                return None
        gives = self.gives(at, node, scope)
        if gives is None:
            return None
        if wanted.kind is Kind.NUMBER and gives.kind is not Kind.NUMBER:
            self.refuse(
                at,
                "This gives a submission's results, not a number. Read one of its values or "
                "facts, as {value: [<it>, <name>]}.",
            )
            return None
        return gives

    def _directions(self, at: Path, node: object) -> None:
        if (
            not isinstance(node, dict)
            or not node
            or any(not isinstance(name, str) or not VALUE_NAME.match(name) for name in node)
            or any(way not in (DESC, ASC) for way in node.values())
        ):
            self.refuse(
                at,
                "Give the direction of each value the task's value reads directly, desc for "
                "higher is better and asc for lower, such as {st1: desc, st2: desc}.",
            )

    def _results(self, at: Path, node: object) -> str | None:
        leaf = leaf_of(node) if isinstance(node, dict) else None
        if leaf is None or leaf[0] != "results":
            self.refuse(at, "Give the task whose results this goes over, as {results: <task>}.")
            return None
        task = leaf[1]
        if not isinstance(task, str) or task not in self.tasks:
            self.refuse(at, f"There is no task called {task} in this contest.")
            return None
        self.reading.tasks.add(task)
        return task

    def gives(self, at: Path, node: object, scope: tuple[str, ...]) -> _Gives | None:
        if isinstance(node, bool):
            self.refuse(at, "Give a number, 1 or 0, not true or false.")
            return None
        if is_number(node):
            return _NUMBER
        if not isinstance(node, dict):
            self.refuse(at, _LEAF_WORDS)
            return None
        if "block" in node:
            return self._block(at, node, scope)
        leaf = leaf_of(node)
        if leaf is None:
            self.refuse(at, _LEAF_WORDS)
            return None
        kind, named = leaf
        match kind:
            case "value":
                return self._value(at, named, scope)
            case "column":
                return self._column(at, named)
            case "results":
                self.refuse(
                    at,
                    "A task's results give no number by themselves. Pick one, or fold them, "
                    f"such as {{block: count, over: {{results: {named}}}}}.",
                )
            case "name":
                self.refuse(at, "{name: <value>} is the value best_each works out again, only.")
            case _:
                self.refuse(
                    at,
                    "A leaderboard reads a task's results, not its runs or a submission's facts "
                    "directly: read a value or a fact of a submission as {value: [<task or "
                    "pick column>, <name>]}.",
                )
        return None

    def _value(self, at: Path, named: object, scope: tuple[str, ...]) -> _Gives | None:
        if not isinstance(named, list) or len(named) != 2:
            self.refuse(
                at,
                "Give a submission's value or fact as {value: [<task, pick column or block>, "
                "<name>]}.",
            )
            return None
        of, name = named
        if not isinstance(name, str) or not (name in FACTS or VALUE_NAME.match(name)):
            self.refuse((*at, "value", 1), f"{name} cannot be a value's or a fact's name.")
            return None
        if isinstance(of, str) and of in self.tasks:
            if of not in scope:
                self.refuse(
                    at,
                    f"{of}.{name} reads the submission a block over {of}'s results is looking "
                    f"at, so it is read only in such a block's where, by or of. Elsewhere, read "
                    "a pick column, such as {value: [<pick column>, " + name + "]}.",
                )
                return None
            self.reading.reads.add((of, name))
            return _NUMBER
        if isinstance(of, str):
            if of not in self.board.columns:
                self.refuse(
                    (*at, "value", 0),
                    f"There is no task or column of this leaderboard called {of}.",
                )
                return None
            self.reading.columns.add((self.key, of))
            kind, tasks = self.kinds.get((self.key, of), (Kind.NUMBER, frozenset()))
            if kind is not Kind.SUBMISSION:
                self.refuse(
                    (*at, "value", 0),
                    f"{of} gives a number, not a submission's results, so it has no {name}.",
                )
                return None
            self.reading.reads.update((task, name) for task in tasks)
            self.through.setdefault((self.key, of), set()).add(name)
            return _NUMBER
        gives = self.gives((*at, "value", 0), of, scope)
        if gives is None:
            return None
        if gives.kind is not Kind.SUBMISSION:
            self.refuse(
                (*at, "value", 0),
                f"This gives a number, not a submission's results, so it has no {name}.",
            )
            return None
        self.reading.reads.update((task, name) for task in gives.tasks)
        for column in _submission_columns(of, self.key):
            self.through.setdefault(column, set()).add(name)
        return _NUMBER

    def _column(self, at: Path, named: object) -> _Gives | None:
        if isinstance(named, str):
            board, name = self.key, named
        elif (
            isinstance(named, list)
            and len(named) == 2
            and all(isinstance(part, str) for part in named)
        ):
            board, name = named
        else:
            self.refuse(
                at,
                "Give a column as {column: <name>}, or another leaderboard's as "
                "{column: [<leaderboard>, <name>]}.",
            )
            return None
        other = self.boards.get(board)
        if other is None:
            self.refuse(at, f"There is no leaderboard called {board}.")
            return None
        if name not in other.columns:
            whose = "This leaderboard" if board == self.key else board
            self.refuse(at, f"{whose} has no column called {name}.")
            return None
        self.reading.columns.add((board, name))
        kind, tasks = self.kinds.get((board, name), (Kind.NUMBER, frozenset()))
        return _Gives(kind, tasks)

    def _block(self, at: Path, node: Mapping[str, object], scope: tuple[str, ...]) -> _Gives | None:
        name = node["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        if block is None:
            self.refuse(
                (*at, "block"),
                f"{name} is not a block Unicon has. The blocks are {', '.join(CATALOGUE)}.",
            )
            return None
        form = form_of(block, node, Scope.TASK)
        if form is None:
            self.refuse(at, forms_words(block, Scope.TASK))
            return None
        if form.scope is Scope.TASK:
            self.refuse(
                at,
                f"{block.name} given {form.given} is for a task's value, never a leaderboard's "
                "column, which reads the task's results.",
            )
            return None
        for key in node:
            if key != "block" and key not in form.inputs:
                self.refuse(
                    (*at, str(key)),
                    f"{block.name} has no input called {key}. It takes {', '.join(form.inputs)}.",
                )
        over = form.goes_over
        inner = scope
        if over is not None and over in node:
            task = self._results((*at, over), node[over])
            inner = (*scope, task) if task is not None else scope
        gave: list[_Gives] = []
        ok = True
        for key, wanted in form.inputs.items():
            if key == over:
                continue
            if key not in node:
                if not wanted.optional:
                    self.refuse((*at, key), f"{block.name} needs its input {key}.")
                    ok = False
                continue
            got = self._given(
                block.name, (*at, key), node[key], wanted, inner if wanted.per_run else scope
            )
            if got is None:
                ok = False
            else:
                gave += got
        if over is not None and over not in node:
            self.refuse((*at, over), f"{block.name} needs its input {over}.")
            ok = False
        if block.name == "best_each":
            self._best_each(node)
        if not ok:
            return None
        if form.gives is Kind.SUBMISSION:
            task = inner[-1] if len(inner) > len(scope) else None
            return _Gives(Kind.SUBMISSION, frozenset({task}) if task is not None else frozenset())
        if form.gives is Kind.EITHER:
            kinds = {each.kind for each in gave}
            if len(kinds) > 1:
                self.refuse(
                    (*at, "of"),
                    "otherwise gives what it is given, so give it two numbers or two "
                    "submissions' results, not one of each.",
                )
                return None
            kind = kinds.pop() if kinds else Kind.NUMBER
            return _Gives(kind, frozenset().union(*(each.tasks for each in gave)))
        return _NUMBER

    def _best_each(self, node: Mapping[str, object]) -> None:
        source = node.get("from")
        leaf = leaf_of(source) if isinstance(source, dict) else None
        named = node.get("value")
        value = leaf_of(named) if isinstance(named, dict) else None
        if leaf is None or value is None or leaf[0] != "results" or value[0] != "name":
            return
        task, name = leaf[1], value[1]
        if isinstance(task, str) and isinstance(name, str) and task in self.tasks:
            self.reading.reads.add((task, name))
        directions = node.get("directions")
        if isinstance(task, str) and isinstance(directions, dict) and task in self.tasks:
            self.reading.reads.update((task, each) for each in directions if isinstance(each, str))

    def _given(
        self, block: str, at: Path, given: object, wanted: BlockInput, scope: tuple[str, ...]
    ) -> list[_Gives] | None:
        if wanted.plain:
            if isinstance(given, bool) or not isinstance(given, int) or given < 1:
                self.refuse(at, "Give a whole number written out here, at least 1, such as 4.")
                return None
            return []
        if wanted.least is None:
            if isinstance(given, list):
                self.refuse(at, f"This input of {block} takes one node, not a list.")
                return None
            got = self.node(at, given, wanted, scope)
            if got is None and wanted.kind in (Kind.NUMBER, Kind.EITHER):
                return None
            return [got] if got is not None else []
        if not isinstance(given, list):
            self.refuse(at, f"This input of {block} takes a list of {_count(wanted)}.")
            return None
        ok = True
        if len(given) < wanted.least or (wanted.most is not None and len(given) > wanted.most):
            self.refuse(at, f"{block} takes {_count(wanted)} here, not {len(given)}.")
            ok = False
        found: list[_Gives] = []
        for index, each in enumerate(given):
            got = self.node((*at, index), each, wanted, scope)
            if got is None:
                ok = False
            else:
                found.append(got)
        return found if ok else None


def _count(wanted: BlockInput) -> str:
    if wanted.most == wanted.least:
        return f"exactly {wanted.least} inputs"
    return f"at least {wanted.least} inputs"


def _loops(
    shapes: Mapping[str, Mapping[str, ColumnShape]],
) -> dict[BoardColumn, list[BoardColumn]]:
    """Each column that reads itself, with the columns from it back to it."""
    reads = {
        (board, name): shape.columns
        for board, columns in shapes.items()
        for name, shape in columns.items()
    }
    found: dict[BoardColumn, list[BoardColumn]] = {}
    for start in reads:
        waiting: list[list[BoardColumn]] = [[start]]
        seen: set[BoardColumn] = set()
        while waiting:
            path = waiting.pop()
            for each in sorted(reads.get(path[-1], ())):
                if each == start:
                    found[start] = [*path, each]
                    waiting = []
                    break
                if each not in seen:
                    seen.add(each)
                    waiting.append([*path, each])
    return found


def _submission_columns(tree: object, board: str) -> list[BoardColumn]:
    """The columns a tree of `board` gives the submission of as it is: a
    `{column: ...}` leaf, or one among `otherwise`'s inputs, at any depth."""
    if not isinstance(tree, dict):
        return []
    if "block" in tree:
        given = tree.get("of")
        if tree["block"] != "otherwise" or not isinstance(given, list):
            return []
        return [column for each in given for column in _submission_columns(each, board)]
    leaf = leaf_of(tree)
    if leaf is not None and leaf[0] == "column":
        return [column_at(board, leaf[1])]
    return []


def _through(
    boards: Mapping[str, Leaderboard], read: Mapping[BoardColumn, set[str]]
) -> dict[BoardColumn, frozenset[str]]:
    """What each column giving a submission's results has read through
    it, `read` what each is read for directly, handed on to each column it
    gives the submission of as it is (`_submission_columns`)."""
    found = {column: set(names) for column, names in read.items()}
    grown = True
    while grown:
        grown = False
        for column, names in list(found.items()):
            board = boards.get(column[0])
            tree = board.columns.get(column[1]) if board is not None else None
            for other in _submission_columns(tree, column[0]):
                had = found.setdefault(other, set())
                if not names <= had:
                    had |= names
                    grown = True
    return {column: frozenset(names) for column, names in found.items()}


def _readable(
    shapes: Mapping[str, Mapping[str, ColumnShape]], troubled: Collection[str]
) -> frozenset[str]:
    """The boards with no problem that read only such boards' columns."""
    good = {board for board in shapes if board not in troubled}
    grown = True
    while grown:
        grown = False
        for board in sorted(good):
            others = {other for shape in shapes[board].values() for other, _ in shape.columns}
            if not others <= good:
                good.discard(board)
                grown = True
    return frozenset(good)


def _order(
    shapes: Mapping[str, Mapping[str, ColumnShape]], readable: Collection[str]
) -> tuple[BoardColumn, ...]:
    """Every column of the `readable` boards, each after those it reads."""
    reads = {
        (board, name): shape.columns
        for board in shapes
        if board in readable
        for name, shape in shapes[board].items()
    }
    order: list[BoardColumn] = []
    placed: set[BoardColumn] = set()
    waiting = list(reads)
    while waiting:
        ready = [each for each in waiting if reads[each] <= placed]
        if not ready:
            break
        order.extend(ready)
        placed.update(ready)
        waiting = [each for each in waiting if each not in placed]
    return tuple(order)


__all__ = [
    "MOST_COLUMNS",
    "MOST_LEADERBOARDS",
    "BoardColumn",
    "BoardsCheck",
    "ColumnShape",
    "check_boards",
    "column_at",
    "spelled",
]
