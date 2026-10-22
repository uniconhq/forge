"""Every block a task's value may use, with its inputs and the kind each
takes (SIMPLER-CORE.md 14, #511; the leaderboards note's 6.1). It is the
one allowlist: the editor offers these and nothing else, and the forge runs
these and nothing else, so a block a version is saved with is never
dropped, since a published task grades the same forever.

A value is saved as its tree, never as formula text: a node is
`{block: <name>, <input>: ..., ...}`, an input taking one node or a list of
them, and a leaf is one of:

- a number, never true or false;
- `{runs: <group>}`, a group's runs, for a fold's `over`;
- `{run: [<group>, <column>]}`, that column of each run, only inside a fold
  over that group, in its `of` or its `where`;
- `{value: <name>}`, another of the task's values;
- `{fact: <name>}`, a fact of the submission (`TASK_FACTS`).

Every block gives a number. A fold (`min_runs`, `max_runs`, `sum_runs`,
`count_runs`) reads a group's runs: its `of` and `where` are read once for
each of them, `weights` gives a run a weight by one repeated input's value,
`{<input>: {<value>: <weight>}}`, a run it does not name weighing 1, and
`missing` says what a missing report counts as, `skip` to leave the run
out, or a number.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    """What an input of a block takes."""

    NUMBER = "number"
    RUNS = "runs"
    WEIGHTS = "weights"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class BlockInput:
    """One input of a block: the kind it takes; a list of at least `least`
    and at most `most` when `least` is given, one node otherwise; whether it
    may be left out; and whether a fold reads it once for each run.
    """

    kind: Kind
    least: int | None = None
    most: int | None = None
    optional: bool = False
    per_run: bool = False


@dataclass(frozen=True, slots=True)
class Block:
    """One block: its name, what it gives in words, and its inputs."""

    name: str
    words: str
    inputs: Mapping[str, BlockInput] = field(default_factory=dict)

    @property
    def fold(self) -> bool:
        """Whether the block folds over a group's runs."""
        return "over" in self.inputs


SKIP = "skip"
"""A fold's `missing` that leaves out a run whose report is missing."""
ALL = "all"
"""The group every task has, every run, built in."""
FACTS = ("attempt", "seconds", "seconds_late", "marked", "compiled")
"""A submission's facts (the leaderboards note's 5.2)."""
TASK_FACTS = ("seconds_late", "seconds", "compiled")
"""The facts a task's value may read (SIMPLER-CORE.md 9.2): not `attempt`,
which depends on the entrant's other submissions, nor `marked`, which
changes until the close."""
LEAVES = ("runs", "run", "value", "fact")

_PAIR = BlockInput(Kind.NUMBER, least=2, most=2)
_SOME = BlockInput(Kind.NUMBER, least=2)
_ONE = BlockInput(Kind.NUMBER)


def _number(name: str, words: str, of: BlockInput) -> Block:
    return Block(name, words, {"of": of})


def _fold(name: str, words: str, *, of: bool, weights: bool) -> Block:
    inputs = {"over": BlockInput(Kind.RUNS)}
    if of:
        inputs["of"] = BlockInput(Kind.NUMBER, per_run=True)
    inputs["where"] = BlockInput(Kind.NUMBER, optional=True, per_run=True)
    if weights:
        inputs["weights"] = BlockInput(Kind.WEIGHTS, optional=True)
    inputs["missing"] = BlockInput(Kind.MISSING, optional=True)
    return Block(name, words, inputs)


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
            "then when test is not 0, else else, reading only the branch it takes",
            {"test": _ONE, "then": _ONE, "else": _ONE},
        ),
        _number("min", "the least of its inputs", _SOME),
        _number("max", "the most of its inputs", _SOME),
        _number("floor", "its input rounded down to a whole number", _ONE),
        _number("ceil", "its input rounded up to a whole number", _ONE),
        _fold("min_runs", "the least of a column over a group's runs", of=True, weights=False),
        _fold("max_runs", "the most of a column over a group's runs", of=True, weights=False),
        _fold("sum_runs", "a column summed over a group's runs", of=True, weights=True),
        _fold("count_runs", "how many of a group's runs there are", of=False, weights=True),
    )
}
