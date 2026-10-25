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
- **Missing passes through picks, folds and `best_each`:** a submission
  whose `where`, `by`, folded `of` or value `best_each` reads is missing
  makes the block missing, a pick giving no submission and an `if` testing
  it missing, and the block counts it, so a mark still to be entered keeps
  a sum missing and its submission in the grading queue. The block's
  `missing`, written by the organiser, is the way round: `skip` leaves
  such a submission out, and a number counts the missing input as that
  number (stage 5 section 6, item 127).
- **Missing and none spread** through arithmetic, the comparisons, `and`,
  `or`, `not`, `r.v` and `sum_best`; `if` reads only the branch its test
  takes, a submission's results as its test true when there is one and a
  missing number giving missing; `otherwise` gives its second input when
  its first is missing or none, and a missing pick it gives stays missing;
  but it gives its first as it is, with no number, when that read anything
  hidden, or a value still waiting of a submission a block around it is
  looking at, as a task's `otherwise` waits (item 128), so that block
  leaves the submission out, never both counting it and saying it waits.
  A first left with no number by leaving out submissions still waiting,
  as a pick passing over them, still gives the second.
- **`best_each`** takes the best of each value the task's value reads
  directly over the results, each in its direction, the oldest on a tie,
  and works the value out again from them with the task's own tree; it is
  missing when that tree reads anything but those values and numbers
  through blocks over numbers (`check.direct_reads`), or a direction is
  not given, which the save refuses across files by the same rule
  (`forge.domain.board_checks`), so only a task's version still to be
  checked could give it.
- **Waiting.** A value of a submission still being worked out, or waiting
  for staff, is left out of every block that reads it, and the submission
  is recorded as left out waiting (guarantee 5). A
  pick reads every value and fact read through it, so it passes over a
  submission waiting on any of them, among those its `where` keeps: a
  pick column what any column of any board reads through it
  (`BoardsCheck.through`), a pick read in place, `{value: [<pick>, v]}`,
  its `v`.
- **Hidden.** A name a submission's results hold back from the reader
  (`Submitted.hidden`) gives no number, and every cell that reads it is
  hidden, the values a pick or a fold compares or tests included, and
  every column reading through such a pick (guarantee 2): a pick is hidden
  when anything the boards worked out read through it
  (`BoardsCheck.hiding`) is hidden on a submission it looked at. Which
  cells the reader is shown is `forge.domain.boards`'.

Each cell records the submissions it counted (the leaderboards note's
5.3): a pick its one; `max` and `min` the oldest that reached the result;
`best_each` one for each value; `sum` and `count` every one they kept; a
block made missing by a submission every one that made it so, and, for a
fold, every one it kept; a block reading other cells theirs.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from forge.domain.blocks.board_check import ColumnRef, LeaderboardsCheck, column_at
from forge.domain.blocks.catalogue import ASC, CATALOGUE, PICKS, SKIP
from forge.domain.blocks.check import direct_reads, form_of, leaf_of
from forge.domain.blocks.evaluate import over_numbers
from forge.domain.blocks.numbers import exact
from forge.domain.definitions import Leaderboard

SubmissionAt = tuple[str, int]
"""A submission by its task's id and its number."""


@dataclass(frozen=True, slots=True)
class Submitted:
    """One submission's results as a board reads them: its number, each
    value and fact that has a number, by name, the names still waiting, and
    the names held back from the reader. A name in none is missing."""

    number: int
    numbers: Mapping[str, Fraction] = field(default_factory=dict)
    waiting: frozenset[str] = frozenset()
    hidden: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Cell:
    """What one column gave a row: its number, or for a column giving a
    submission's results, the submission with its task, none for none;
    whether it gives a submission's results; the submissions it counted;
    those it left out as still waiting; whether it read anything held
    back from the reader; and, for a pick giving no submission, whether
    that is because a submission it looked at was missing what it chose
    by, rather than none being left."""

    number: Fraction | None = None
    picked: tuple[str, Submitted] | None = None
    submission: bool = False
    counted: frozenset[SubmissionAt] = frozenset()
    waiting: frozenset[SubmissionAt] = frozenset()
    hidden: bool = False
    missing: bool = False

    def having(self, *others: Cell) -> Cell:
        """This, with what `others` counted, left out and read added, and
        still missing when it was."""
        return Cell(
            self.number,
            self.picked,
            self.submission,
            self.counted.union(*(other.counted for other in others)),
            self.waiting.union(*(other.waiting for other in others)),
            self.hidden or any(other.hidden for other in others),
            self.missing,
        )

    @property
    def nothing(self) -> bool:
        """Whether it is missing, or none."""
        return self.number is None and self.picked is None


_MISSING = Cell()


def work_out_row(
    leaderboards: Mapping[str, Leaderboard],
    checked: LeaderboardsCheck,
    results: Mapping[str, Sequence[Submitted]],
    trees: Mapping[str, Mapping[str, object]],
) -> dict[ColumnRef, Cell]:
    """Every column of the boards the check reads, for one row: `results`
    the row's submissions to each task, oldest first, a task with none left
    out, and `trees` each task's values' trees, which `best_each` reads."""
    row = _Row(results, trees)
    hiding = checked.hiding()
    for leaderboard, name in checked.order:
        row.leaderboard = leaderboard
        through = checked.through.get((leaderboard, name), frozenset())
        hides = hiding.get((leaderboard, name), frozenset())
        row.done[(leaderboard, name)] = row.node(
            leaderboards[leaderboard].columns[name], {}, through, hides
        )
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
        self.done: dict[ColumnRef, Cell] = {}
        self.leaderboard = ""

    def node(
        self,
        tree: object,
        scope: Scope,
        through: frozenset[str] = frozenset(),
        hides: frozenset[str] = frozenset(),
    ) -> Cell:
        """What `tree` gives, `scope` the submissions the blocks around it
        are looking at by task, and, when it gives a submission's results,
        `through` what is read through it and `hides` what the boards
        worked out read through it."""
        if not isinstance(tree, dict | list):
            number = exact(tree)
            if number is None:
                raise ValueError(f"{tree!r} is not a node of a column's tree")
            return Cell(number)
        if not isinstance(tree, dict):
            raise ValueError(f"{tree!r} is not a node of a column's tree")
        if "block" in tree:
            return self.block(tree, scope, through, hides)
        leaf = leaf_of(tree)
        if leaf is None:
            raise ValueError(f"{tree!r} is not a node of a column's tree")
        kind, named = leaf
        if kind == "column":
            return self.done[column_at(self.leaderboard, named)]
        if kind == "value" and isinstance(named, list):
            of, name = named
            if isinstance(of, str) and of in scope:
                return _read(scope[of], name)
            picked = (
                self.done[(self.leaderboard, of)]
                if isinstance(of, str)
                else self.node(of, scope, frozenset({name}), frozenset({name}))
            )
            if picked.picked is None:
                return _MISSING.having(picked)
            return _read(picked.picked, name).having(picked)
        raise ValueError(f"{tree!r} gives no number on a leaderboard")

    def block(
        self,
        tree: Mapping[str, object],
        scope: Scope,
        through: frozenset[str],
        hides: frozenset[str],
    ) -> Cell:
        name = tree["block"]
        block = CATALOGUE.get(name) if isinstance(name, str) else None
        form = form_of(block, tree) if block is not None else None
        if block is None or form is None:
            raise ValueError(f"{tree!r} fits no block a leaderboard has")
        if form.goes_over is not None:
            source = tree[form.goes_over]
            assert isinstance(source, dict)
            task = str(source["results"])
            listed = self.results.get(task, ())
            if block.name in PICKS:
                return self.pick(block.name, tree, task, listed, scope, through, hides)
            if block.name == "best_each":
                return self.best_each(tree, task, listed)
            return self.fold(block.name, tree, task, listed, scope)
        if block.name == "if":
            test = self.node(tree["test"], scope)
            if test.submission and test.missing:
                return _MISSING.having(test)
            if test.submission:
                truth = test.picked is not None
            elif test.number is None:
                return _MISSING.having(test)
            else:
                truth = test.number != 0
            return self.node(tree["then"] if truth else tree["else"], scope).having(test)
        given = tree["of"]
        inputs = given if isinstance(given, list) else [given]
        if block.name == "otherwise":
            first = self.node(inputs[0], scope, through, hides)
            looked_at = {(task, each.number) for task, each in scope.values()}
            if not first.nothing or first.hidden or first.waiting & looked_at:
                return first
            return self.node(inputs[1], scope, through, hides).having(first)
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
        through: frozenset[str],
        hides: frozenset[str],
    ) -> Cell:
        waiting: set[SubmissionAt] = set()
        hidden = False
        lacking: set[SubmissionAt] = set()
        kept: list[tuple[Submitted, Fraction]] = []
        for each in listed:
            at = (task, each.number)
            where = self._looked_at(tree, "where", task, each, scope)
            if where is not None:
                waiting |= where.waiting
                hidden = hidden or where.hidden
                test = _given(where, tree)
                if test is None:
                    lacking.add(at)
                    continue
                if test is _LEFT_OUT or test == 0:
                    continue
            number = Fraction(0)
            by = self._looked_at(tree, "by", task, each, scope)
            if by is not None:
                waiting |= by.waiting
                hidden = hidden or by.hidden
                given = _given(by, tree)
                if given is None:
                    lacking.add(at)
                    continue
                if not isinstance(given, Fraction):
                    continue
                number = given
            # What is read through the pick is read of each submission it
            # looks at, so a name held back there hides the pick.
            hidden = hidden or bool(hides & each.hidden)
            if through & each.waiting:
                # What is read through the pick still waits: pass it over.
                waiting.add(at)
                continue
            kept.append((each, number))
        if lacking:
            return Cell(
                submission=True,
                counted=frozenset(lacking),
                waiting=frozenset(waiting),
                hidden=hidden,
                missing=True,
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
            return Cell(submission=True, waiting=frozenset(waiting), hidden=hidden)
        return Cell(
            picked=(task, found),
            submission=True,
            counted=frozenset({(task, found.number)}),
            waiting=frozenset(waiting),
            hidden=hidden,
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
        hidden = False
        lacking: set[SubmissionAt] = set()
        kept: list[tuple[Submitted, Fraction]] = []
        for each in listed:
            at = (task, each.number)
            where = self._looked_at(tree, "where", task, each, scope)
            if where is not None:
                waiting |= where.waiting
                hidden = hidden or where.hidden
                test = _given(where, tree)
                if test is None:
                    lacking.add(at)
                    continue
                if test is _LEFT_OUT or test == 0:
                    continue
            number = Fraction(1)
            of = self._looked_at(tree, "of", task, each, scope)
            if of is not None:
                waiting |= of.waiting
                hidden = hidden or of.hidden
                given = _given(of, tree)
                if given is None:
                    lacking.add(at)
                    continue
                if not isinstance(given, Fraction):
                    continue
                number = given
            kept.append((each, number))
        left = frozenset(waiting)
        if lacking:
            counted = frozenset({*lacking, *((task, each.number) for each, _ in kept)})
            return Cell(counted=counted, waiting=left, hidden=hidden)
        if name == "count":
            counted = frozenset((task, each.number) for each, _ in kept)
            return Cell(Fraction(len(kept)), counted=counted, waiting=left, hidden=hidden)
        if not kept:
            return Cell(waiting=left, hidden=hidden)
        if name == "sum":
            counted = frozenset((task, each.number) for each, _ in kept)
            total = over_numbers("add", [Fraction(0), *(number for _, number in kept)])
            return Cell(total, counted=counted, waiting=left, hidden=hidden)
        best = (max if name == "max" else min)(number for _, number in kept)
        first = next(each for each, number in kept if number == best)
        return Cell(best, counted=frozenset({(task, first.number)}), waiting=left, hidden=hidden)

    def best_each(self, tree: Mapping[str, object], task: str, listed: Sequence[Submitted]) -> Cell:
        named = tree["value"]
        directions = tree["directions"]
        assert isinstance(named, dict) and isinstance(directions, dict)
        value = str(named["name"])
        own = self.trees.get(task, {}).get(value)
        reads, odd = direct_reads(own)
        # It reads the value and each it reads directly, of every submission.
        hidden = any(
            name in each.hidden for each in listed for name in {value, *(() if odd else reads)}
        )
        if own is None or odd is not None or not reads <= set(directions):
            return Cell(hidden=hidden)
        missing = tree.get("missing")
        waiting: set[SubmissionAt] = set()
        counted: set[SubmissionAt] = set()
        lacking: set[SubmissionAt] = set()
        bests: dict[str, Fraction | None] = {}
        for each in sorted(reads):
            best: tuple[Submitted, Fraction] | None = None
            for submitted in listed:
                if each in submitted.waiting:
                    waiting.add((task, submitted.number))
                    continue
                number = submitted.numbers.get(each)
                if number is None and missing != SKIP:
                    number = exact(missing) if missing is not None else None
                    if number is None:
                        lacking.add((task, submitted.number))
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
        if lacking:
            return Cell(counted=frozenset(lacking), waiting=frozenset(waiting), hidden=hidden)
        return Cell(
            _again(own, bests),
            counted=frozenset(counted),
            waiting=frozenset(waiting),
            hidden=hidden,
        )


_LEFT_OUT = object()
"""A submission a block leaves out: what it read of it is hidden or still
waits, or is missing and the block's `missing` is `skip`."""


def _given(read: Cell, tree: Mapping[str, object]) -> Fraction | object | None:
    """What a block reads of one submission it looks at, its `where`, `by`
    or `of` as `read`: its number; `_LEFT_OUT` when that is hidden or still
    waits, or is missing and the block's `missing` is `skip`; the number
    `missing` gives for a missing one; or none, missing."""
    if read.number is not None:
        return read.number
    if read.hidden or read.waiting:
        return _LEFT_OUT
    missing = tree.get("missing")
    if missing == SKIP:
        return _LEFT_OUT
    return exact(missing) if missing is not None else None


def _read(picked: tuple[str, Submitted], name: str) -> Cell:
    task, submitted = picked
    if name in submitted.hidden:
        return Cell(hidden=True)
    if name in submitted.waiting:
        return Cell(waiting=frozenset({(task, submitted.number)}))
    return Cell(submitted.numbers.get(name))


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
