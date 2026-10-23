"""Every block a task's value or a board's column may use, with its inputs
and the kind each takes (SIMPLER-CORE.md 10.1 and 14, #511 and #512; the
leaderboards note's 5.3 and 6.1). It is the
one allowlist: the editor offers these and nothing else, and the forge runs
these and nothing else. Once the simpler core ships, a block a version is
saved with is never dropped, since a published task grades the same
forever; until then nothing is published (#513), so stage 4's fold names
could give way to the spec's.

A value is saved as its tree, never as formula text: a node is
`{block: <name>, <input>: ..., ...}`, an input taking one node or a list of
them, and a leaf is one of:

- a number, never true or false;
- `{runs: <group>}`, a group's runs, for a fold's `over`;
- `{run: [<group>, <column>]}`, that column of each run, only inside a fold
  over that group, in its `of` or its `where`;
- `{value: <name>}`, another of the task's values;
- `{fact: <name>}`, a fact of the submission (`TASK_FACTS`).

Every block gives a number. A block has one form or more, each the inputs
it takes for one kind of thing it is given: `min` and `max` take numbers or
a group's runs, and the check tells which form a node means from what it is
given. A fold, a form given a group's runs (`min`, `max`, `sum`, `count`),
reads them: its `of`
and `where` are read once for each of them, `weights` gives a run a weight
by one repeated input's value, `{<input>: {<value>: <weight>}}`, a run it
does not name weighing 1, and `missing` says what a missing report counts
as, `skip` to leave the run out, or a number.

Each form says where it may be used (`Form.scope`): a task's value, a
board's column, or both. `min` and `max` of numbers are both's, a fold
over a group's runs is a value's, and a fold over a task's results is a
column's; `exec` and `entered` are a value's alone, so a board has no
`exec` (14.2).

A board's column reads, besides numbers (the leaderboards note's 5.10):

- `{results: <task>}`, the row's results for a task, one per submission
  that counts, oldest first, for a pick's `from`, a fold's `over` and
  `best_each`'s `from`;
- `{value: [<of>, <name>]}`, value or fact `name` of one submission's
  results: `<of>` a task, meaning the submission a `where`, a `by` or a
  fold's `of` is looking at, a pick column of the board, or a node giving
  a submission's results;
- `{column: <name>}`, another column of the same row, or `{column:
  [<board>, <name>]}`, a column of another board's;
- `{name: <value>}`, the value `best_each` works out again.

A pick (`first`, `latest`, `highest`, `lowest`) gives one submission's
results, or none; `otherwise` gives what it is given, a number or a
submission's results; every other block gives a number. `if`'s test may
be a submission's results on a board: true when there is one.

`entered` reads no node: it is the mark a manager of the task enters for
the submission, its bounds numbers written out, and a value holds one at
most, so an entry is the value's own. `exec` is the number a program of
the task reports from a group's runs: `program` a folder of the task with
its `program.yaml`, `language` one of `LANGUAGES`, and its limits numbers
written out, exec's own when left out. A value holds one at most, since
its program's steps are named after it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    """What an input of a block takes: a number; a group's runs; a task's
    results; one submission's results; a number or a submission's results
    (`either`); `best_each`'s value by name and its directions; or what a
    fold's `weights`, `missing` and an `exec` block's program and language
    are written as."""

    NUMBER = "number"
    RUNS = "runs"
    RESULTS = "results"
    SUBMISSION = "submission"
    EITHER = "either"
    NAME = "name"
    DIRECTIONS = "directions"
    WEIGHTS = "weights"
    MISSING = "missing"
    PATH = "path"
    LANGUAGE = "language"


class Scope(StrEnum):
    """Where a form may be used: a task's value, a board's column, or both."""

    TASK = "task"
    BOARD = "board"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class BlockInput:
    """One input of a block: the kind it takes; a list of at least `least`
    and at most `most` when `least` is given, one node otherwise; whether it
    may be left out; whether a fold or a pick reads it once for each run
    or result it goes over; and whether it takes only a number written
    out, never a block or a leaf.
    """

    kind: Kind
    least: int | None = None
    most: int | None = None
    optional: bool = False
    per_run: bool = False
    plain: bool = False


@dataclass(frozen=True, slots=True)
class Form:
    """One way a block is given its inputs: what it is given, in words, its
    inputs, where it may be used, and what it gives: a number, a
    submission's results, or, for `otherwise`, what it is given.
    """

    given: str
    inputs: Mapping[str, BlockInput] = field(default_factory=dict)
    scope: Scope = Scope.BOTH
    gives: Kind = Kind.NUMBER

    @property
    def fold(self) -> bool:
        """Whether the form folds over a group's runs, reading an input once
        for each of them; `exec` reads a group's runs whole.
        """
        over = self.inputs.get("over")
        return (
            over is not None
            and over.kind is Kind.RUNS
            and any(each.per_run for each in self.inputs.values())
        )

    @property
    def goes_over(self) -> str | None:
        """The input whose task's results the form goes over, reading its
        per-result inputs once for each: a pick's `from`, a fold's `over`,
        `best_each`'s `from`; none for a form over no results."""
        return next(
            (name for name, given in self.inputs.items() if given.kind is Kind.RESULTS), None
        )


@dataclass(frozen=True, slots=True)
class Block:
    """One block: its name, what it gives in words, and its forms."""

    name: str
    words: str
    forms: tuple[Form, ...]


SKIP = "skip"
"""A fold's `missing` that leaves out a run whose report is missing."""
ALL = "all"
"""The group every task has, every run, built in."""
ROW_FACTS = ("attempt", "seconds", "seconds_late", "marked")
"""The facts every viewer of a row reads with it, which a task's `show`
needs no entry for, since none is a grading's: which of the row's
submissions it is, when it was made and how late by the task's due, all
known as it is made, and whether the row marked it (stage 5 section 6,
item 124)."""
SHOWN_FACTS = ("compiled",)
"""The facts a grading gives, each listed in a task's `show` by its name,
as a value is, so that anything a board reads of a task its `show` can
show (item 124)."""
FACTS = (*ROW_FACTS, *SHOWN_FACTS)
"""A submission's facts (the leaderboards note's 5.2), which a board's
column reads beside its values."""
TASK_FACTS = ("seconds_late", "seconds", "compiled")
"""The facts a task's value may read (SIMPLER-CORE.md 9.2): not `attempt`,
which depends on the entrant's other submissions, nor `marked`, which
changes until the close."""
LEAVES = ("runs", "run", "value", "fact", "results", "column", "name")
TASK_LEAVES = ("runs", "run", "value", "fact")
"""The leaves a task's value may hold; a board's column holds `value`,
`results`, `column` and `name`."""
BOARD_LEAVES = ("value", "results", "column", "name")
DESC = "desc"
ASC = "asc"
PICKS = ("first", "latest", "highest", "lowest")
"""The blocks that give one submission's results."""

NUMBERS = "numbers"
RUNS = "a group's runs"
RESULTS = "a task's results"
SUBMISSIONS = "submissions' results"

_PAIR = BlockInput(Kind.NUMBER, least=2, most=2)
_SOME = BlockInput(Kind.NUMBER, least=2)
_ONE = BlockInput(Kind.NUMBER)
_BOUND = BlockInput(Kind.NUMBER, plain=True)

ENTERED = "entered"
"""The block a manager of the task enters a mark for, one in a value at
most (SIMPLER-CORE.md 14.2)."""
EXEC = "exec"
"""The block a program of the task works out from a group's runs, one in a
value at most (SIMPLER-CORE.md 14)."""
LANGUAGES = ("c", "cpp", "java", "python", "ipynb")
"""The languages an `exec` block's program may be written in: those
`unicon/compile@v3` takes."""
EXEC_TIME_LIMIT = 2
"""The seconds of CPU an `exec` block's program may use when it gives none,
`unicon/exec`'s own."""
EXEC_MEMORY_LIMIT = 256
"""The megabytes an `exec` block's program may use when it gives none,
`unicon/exec`'s own."""
_LIMIT = BlockInput(Kind.NUMBER, optional=True, plain=True)


def _numbers(of: BlockInput) -> Form:
    return Form(NUMBERS, {"of": of})


def _number(name: str, words: str, of: BlockInput) -> Block:
    return Block(name, words, (_numbers(of),))


def _fold(*, of: bool, weights: bool) -> Form:
    inputs = {"over": BlockInput(Kind.RUNS)}
    if of:
        inputs["of"] = BlockInput(Kind.NUMBER, per_run=True)
    inputs["where"] = BlockInput(Kind.NUMBER, optional=True, per_run=True)
    if weights:
        inputs["weights"] = BlockInput(Kind.WEIGHTS, optional=True)
    inputs["missing"] = BlockInput(Kind.MISSING, optional=True)
    return Form(RUNS, inputs, Scope.TASK)


_WHERE = BlockInput(Kind.NUMBER, optional=True, per_run=True)


def _over_results(*, of: bool) -> Form:
    """A fold over a task's results, a board's: `of` read for each result
    `where` keeps."""
    inputs = {"over": BlockInput(Kind.RESULTS)}
    if of:
        inputs["of"] = BlockInput(Kind.NUMBER, per_run=True)
    inputs["where"] = _WHERE
    return Form(RESULTS, inputs, Scope.BOARD)


def _pick(name: str, words: str, *, by: bool) -> Block:
    inputs = {"from": BlockInput(Kind.RESULTS)}
    if by:
        inputs["by"] = BlockInput(Kind.NUMBER, per_run=True)
    inputs["where"] = _WHERE
    return Block(name, words, (Form(RESULTS, inputs, Scope.BOARD, Kind.SUBMISSION),))


CATALOGUE: Mapping[str, Block] = {
    block.name: block
    for block in (
        _number("add", "the sum of its inputs", _SOME),
        _number("sub", "the first input less the second", _PAIR),
        _number("mul", "the product of its inputs", _SOME),
        _number("div", "the first input over the second, missing when it is 0", _PAIR),
        _number("lt", "1 when the first input is less than the second, else 0", _PAIR),
        _number("le", "1 when the first input is at most the second, else 0", _PAIR),
        _number("gt", "1 when the first input is more than the second, else 0", _PAIR),
        _number("ge", "1 when the first input is at least the second, else 0", _PAIR),
        _number("eq", "1 when the two inputs are equal, else 0", _PAIR),
        _number("ne", "1 when the two inputs differ, else 0", _PAIR),
        _number("and", "1 when no input is 0, else 0", _SOME),
        _number("or", "1 when some input is not 0, else 0", _SOME),
        _number("not", "1 when its input is 0, else 0", _ONE),
        Block(
            "if",
            "then when test is not 0, else else, reading only the branch it takes; on a "
            "board, a test that is a submission's results is true when there is one",
            (
                Form(
                    NUMBERS,
                    {"test": BlockInput(Kind.EITHER), "then": _ONE, "else": _ONE},
                ),
            ),
        ),
        Block(
            "min",
            "the least of its inputs, of a column over a group's runs, or of a value over a "
            "task's results",
            (_numbers(_SOME), _fold(of=True, weights=False), _over_results(of=True)),
        ),
        Block(
            "max",
            "the most of its inputs, of a column over a group's runs, or of a value over a "
            "task's results",
            (_numbers(_SOME), _fold(of=True, weights=False), _over_results(of=True)),
        ),
        _number("floor", "its input rounded down to a whole number", _ONE),
        _number("ceil", "its input rounded up to a whole number", _ONE),
        Block(
            "sum",
            "a column summed over a group's runs, or a value over a task's results",
            (_fold(of=True, weights=True), _over_results(of=True)),
        ),
        Block(
            "count",
            "how many of a group's runs there are, their weights summed when weighed, or how "
            "many of a task's results there are, 0 over none",
            (_fold(of=False, weights=True), _over_results(of=False)),
        ),
        _pick("first", "the oldest of a task's results that where keeps, or none", by=False),
        _pick("latest", "the newest of a task's results that where keeps, or none", by=False),
        _pick(
            "highest",
            "the one of a task's results with the highest by, the oldest on a tie, or none",
            by=True,
        ),
        _pick(
            "lowest",
            "the one of a task's results with the lowest by, the oldest on a tie, or none",
            by=True,
        ),
        Block(
            "best_each",
            "the task's value worked out again from the best of each value it reads "
            "directly over the task's results, each in the direction given",
            (
                Form(
                    RESULTS,
                    {
                        "from": BlockInput(Kind.RESULTS),
                        "value": BlockInput(Kind.NAME),
                        "directions": BlockInput(Kind.DIRECTIONS),
                    },
                    Scope.BOARD,
                ),
            ),
        ),
        Block(
            "otherwise",
            "the first input, or the second when the first is missing or none",
            (
                Form(
                    SUBMISSIONS,
                    {"of": BlockInput(Kind.EITHER, least=2, most=2)},
                    Scope.BOARD,
                    Kind.EITHER,
                ),
            ),
        ),
        Block(
            "sum_best",
            "the sum of the k largest of its inputs, keeping the best k",
            (
                Form(
                    NUMBERS,
                    {
                        "k": BlockInput(Kind.NUMBER, plain=True),
                        "of": BlockInput(Kind.NUMBER, least=1),
                    },
                    Scope.BOARD,
                ),
            ),
        ),
        Block(
            EXEC,
            "the one number a program in a folder of the task reports from a group's runs, "
            "which it reads as runs.json",
            (
                Form(
                    RUNS,
                    {
                        "program": BlockInput(Kind.PATH),
                        "language": BlockInput(Kind.LANGUAGE),
                        "over": BlockInput(Kind.RUNS),
                        "time_limit": _LIMIT,
                        "memory_limit": _LIMIT,
                    },
                    Scope.TASK,
                ),
            ),
        ),
        Block(
            ENTERED,
            "the mark a manager of the task enters for the submission, between at_least "
            "and at_most, missing until one is entered",
            (Form(NUMBERS, {"at_least": _BOUND, "at_most": _BOUND}, Scope.TASK),),
        ),
    )
}
