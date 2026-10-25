"""What a contest's leaderboards ask of `contest.yaml` and of each task they
read (SIMPLER-CORE.md 10.1, the leaderboards note's 6.2), checked at the
contest's save and at the save of every task a board reads.

- **Within `contest.yaml`** (`check_contest`): each board against the
  catalogue (`forge.domain.blocks.board_check`), a loop across boards
  among it; and a task's `marks` given only while a board reads that
  task's `marked`, since a mark changes nothing else. A row may mark the
  entry's `marks` submissions of a task, or one when a board reads its
  `marked` and the entry gives none (`marks_of`).
- **The hold** (`hold_problems`): its `from` after the contest's `start`
  and before its `end`, checked again whenever either moves, so neither
  can move past it; and never set or moved to a time already passed,
  which would hide submissions others already saw.
- **Across files** (`across`), against each task scored by its runs whose
  publication is known (`Scored`): every value a column reads is one the
  task gives, or a fact; no block that chooses which submission counts
  chooses by a value reading a mark staff enter, directly or through other
  values or columns (#505, widened by #512), since the grading queue would
  wait on the mark itself; and `best_each` works out again a value that
  reads only other values and numbers directly, each given a direction. The
  blocks that choose are a pick, by its `where` and `by`; a fold over a
  task's results, by its `where`, and `max`, `min` and `sum` by their `of`
  too, since `max` and `min` count the oldest submission reaching the result
  and `sum` counts only the submissions whose `of` is there, so one waiting
  on a mark would never be counted; and `best_each`, by the values it takes
  the best of. What a pick reads through it only to skip a submission still
  waiting (guarantee 5) is no choice. The contest's save resolves each task
  against its live publication, and against its newest too while a save row
  still checks that one, since it then takes over with no save to check the
  boards again (`forge.services.boards`). A task with neither is not
  resolved there; its first save is checked against the boards instead, so
  every name is resolved by one save or the other. A task graded test by
  test is not resolved at all: it gives a board no results, so its cells are
  missing (stage 5 section 6, item 70), and its format goes with the rest
  of today's path (stage 4 open 132).

Each refusal across files names its board and column and carries its path
from the top of `contest.yaml`, so the editor can point at it, and the
words a task's save says when the task would break the board (`Across`).
Nothing is stored from the checks.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from forge.domain.blocks.board_check import (
    ColumnRef,
    LeaderboardsCheck,
    check_leaderboards,
    column_at,
)
from forge.domain.blocks.catalogue import (
    CATALOGUE,
    ENTERED,
    FACTS,
    PICKS,
    RESULTS,
    SKIP,
)
from forge.domain.blocks.check import Found, direct_reads, form_of, leaf_of
from forge.domain.blocks.evaluate import using
from forge.domain.definitions import ContestDefinition, ContestTask
from forge.domain.release import in_words
from forge.domain.yaml_models import Path

MARKED = "marked"


@dataclass(frozen=True, slots=True)
class Scored:
    """A task scored by its runs as a board's names resolve against it: its
    values' trees in one publication of it, by name."""

    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Across:
    """One refusal across files: the board and column it is in, by key and
    name; its path from the top of `contest.yaml`; the task, and the value
    of it, it is about; what the contest's save says; and what the task's
    save says, when it is the task that would break the board."""

    leaderboard: str
    column: str
    at: Path
    task: str
    value: str
    message: str
    saved: str


def leaderboards_of(settings: ContestDefinition) -> LeaderboardsCheck:
    """The contest's boards checked, as they are read."""
    return check_leaderboards(settings.leaderboards, [task.id for task in settings.tasks])


def check_contest(
    settings: ContestDefinition, tasks: Mapping[str, Scored] | None = None
) -> tuple[Found, LeaderboardsCheck]:
    """Every problem with the contest's boards, each at its path, and a
    task's `marks` that no board reads; with `tasks`, each refusal across
    files against them (`across`); and the boards as checked."""
    checked = leaderboards_of(settings)
    found = list(checked.problems)
    for index, entry in enumerate(settings.tasks):
        if entry.marks is not None and not checked.reads(entry.id, MARKED):
            found.append(
                (
                    ("tasks", index, "marks"),
                    "This applies only when a leaderboard reads which of this task's "
                    f"submissions an entrant marks, as {{value: [{entry.id}, marked]}}.",
                )
            )
    if tasks:
        found += [(each.at, each.message) for each in across(settings, checked, tasks)]
    return found, checked


def hold_problems(
    before: ContestDefinition | None, after: ContestDefinition, now: datetime
) -> Found:
    """The hold's `from` inside the contest of `after`, said at the date
    that moved past it when the hold stayed as it was; and, when the save
    sets or moves it, not a time already passed."""
    hold = after.hold
    if hold is None:
        return []
    old = before.hold if before is not None else None
    moved = old is None or old.from_ != hold.from_
    found: Found = []
    if not after.start < hold.from_ < after.end:
        if moved:
            found.append(
                (
                    ("hold", "from"),
                    f"The hold starts on {in_words(hold.from_)}, which must be after the "
                    f"contest's start, {in_words(after.start)}, and before its end, "
                    f"{in_words(after.end)}: a hold elsewhere holds nothing of the contest.",
                )
            )
        else:
            late = hold.from_ >= after.end
            found.append(
                (
                    ("end",) if late else ("start",),
                    f"The leaderboards' hold starts on {in_words(hold.from_)}, so the contest "
                    f"must {'end after' if late else 'start before'} it. Move the hold's from "
                    "first, or take the hold out.",
                )
            )
    if moved and hold.from_ <= now:
        found.append(
            (
                ("hold", "from"),
                f"{in_words(hold.from_)} has passed, so a hold from then would hide submissions "
                "others have already seen on the leaderboards. Give a time still to come.",
            )
        )
    return found


def across(
    settings: ContestDefinition, checked: LeaderboardsCheck, tasks: Mapping[str, Scored]
) -> list[Across]:
    """Each refusal across files of the boards whose own check found
    nothing, against `tasks`, by name; a task not among them is not
    resolved."""
    marked = {name: using(scored.values, ENTERED) for name, scored in tasks.items()}
    found: list[Across] = []
    for key, leaderboard in settings.leaderboards.items():
        if any(path[:2] == ("leaderboards", key) for path, _ in checked.problems):
            continue
        for column, tree in leaderboard.columns.items():
            walk = _Walk(checked, tasks, marked, key, leaderboard.title_or(key), column, settings)
            walk.node(("leaderboards", key, "columns", column), tree)
            found += walk.found
    return found


_CHOOSE = {
    "max": ("where", "of"),
    "min": ("where", "of"),
    "sum": ("where",),
    "count": ("where",),
}
"""What a fold over a task's results chooses the submissions it counts by:
a sum counts every submission its `where` keeps, a missing `of` making it
missing and counting the submission too, so it may add up marks (stage 5
section 6, item 127), but not with `missing: skip`, which leaves out a
submission whose mark is still to come."""


class _Walk:
    """One column's tree walked for what it asks of the tasks it reads."""

    def __init__(
        self,
        checked: LeaderboardsCheck,
        tasks: Mapping[str, Scored],
        marked: Mapping[str, frozenset[str]],
        leaderboard: str,
        title: str,
        column: str,
        settings: ContestDefinition,
    ) -> None:
        self.checked = checked
        self.tasks = tasks
        self.marked = marked
        self.leaderboard = leaderboard
        self.title = title
        self.column = column
        self.ids = frozenset(task.id for task in settings.tasks)
        self.found: list[Across] = []

    def refuse(self, at: Path, task: str, value: str, message: str, saved: str) -> None:
        self.found.append(Across(self.leaderboard, self.column, at, task, value, message, saved))

    def node(self, at: Path, node: object) -> None:
        if isinstance(node, list):
            for index, each in enumerate(node):
                self.node((*at, index), each)
            return
        if not isinstance(node, dict):
            return
        if "block" not in node:
            leaf = leaf_of(node)
            if leaf is not None and leaf[0] == "value" and isinstance(leaf[1], list):
                of, name = leaf[1]
                if isinstance(of, dict):
                    self.node((*at, "value", 0), of)
                for task in sorted(self.given(of)):
                    self.resolve((*at, "value", 1), task, str(name))
            return
        name = str(node["block"])
        block = CATALOGUE[name]
        form = form_of(block, node)
        chooses: tuple[str, ...] = ()
        if name in PICKS:
            chooses = ("where", "by")
        elif form is not None and form.given == RESULTS and name in _CHOOSE:
            chooses = _CHOOSE[name]
            if node.get("missing") == SKIP:
                chooses = ("where", "of")
        for key in chooses:
            if key in node:
                self.choice(name, (*at, key), node[key])
        skipped = {"block"}
        if name == "best_each":
            self.best_each(at, node)
            skipped |= {"value", "directions"}
        for key, given in node.items():
            if key not in skipped:
                self.node((*at, key), given)

    def given(self, of: object) -> frozenset[str]:
        """The tasks whose submission `of`, the first part of a `{value:
        [...]}` leaf, gives: a task's own, a pick column's, or a node's."""
        if isinstance(of, str):
            if of in self.ids:
                return frozenset({of})
            shape = self.checked.shapes.get(self.leaderboard, {}).get(of)
            return shape.of_tasks if shape is not None else frozenset()
        if not isinstance(of, dict):
            return frozenset()
        if "block" not in of:
            leaf = leaf_of(of)
            if leaf is None or leaf[0] != "column":
                return frozenset()
            leaderboard, name = column_at(self.leaderboard, leaf[1])
            shape = self.checked.shapes.get(leaderboard, {}).get(name)
            return shape.of_tasks if shape is not None else frozenset()
        if of["block"] in PICKS:
            source = of.get("from")
            leaf = leaf_of(source) if isinstance(source, dict) else None
            if leaf is not None and leaf[0] == "results" and isinstance(leaf[1], str):
                return frozenset({leaf[1]})
            return frozenset()
        if of["block"] == "otherwise" and isinstance(of.get("of"), list):
            return frozenset().union(*(self.given(each) for each in of["of"]))
        return frozenset()

    def resolve(self, at: Path, task: str, name: str) -> None:
        """`name` of `task` is a fact, or a value the task gives."""
        scored = self.tasks.get(task)
        if name in FACTS or scored is None:
            return
        if name not in scored.values:
            self.refuse(
                at,
                task,
                name,
                f"{task} has no value called {name}. {_values_of(scored.values)} A leaderboard "
                f"also reads the facts {_facts()}.",
                f"The leaderboard {self.title} reads {name} of {task} in its column "
                f"{self.column}, so {task} must keep a value called {name}. Take it out of that "
                "column first.",
            )

    def choice(self, block: str, at: Path, given: object) -> None:
        """What `block` chooses the submissions it counts by, at `at`, reads
        no value reading a mark."""
        for task, value in sorted(self.reads_in(given)):
            if value in self.marked.get(task, frozenset()):
                self.refuse(
                    at,
                    task,
                    value,
                    f"{block} chooses which submission of {task} counts by {value}, which reads "
                    "a mark staff enter, so the grading queue, which lists the submissions each "
                    "cell counts for marking, would wait on the mark itself. Choose by a value "
                    "that reads no mark.",
                    f"The leaderboard {self.title} chooses which submission of {task} counts by "
                    f"{value} in its column {self.column}, so {value} cannot read a mark staff "
                    "enter: the grading queue would wait on the mark itself. Change that column "
                    "first.",
                )
                return

    def reads_in(self, tree: object) -> set[tuple[str, str]]:
        """Each task's value or fact a tree reads, through the columns it
        reads too, by task."""
        found: set[tuple[str, str]] = set()
        columns: set[ColumnRef] = set()
        waiting = [tree]
        while waiting:
            node = waiting.pop()
            if isinstance(node, list):
                waiting.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if "block" in node:
                if node["block"] == "best_each":
                    found |= _best_each_reads(node)
                    continue
                waiting.extend(given for key, given in node.items() if key != "block")
                continue
            leaf = leaf_of(node)
            if leaf is None:
                continue
            if leaf[0] == "column":
                columns.add(column_at(self.leaderboard, leaf[1]))
            elif leaf[0] == "value" and isinstance(leaf[1], list):
                of, name = leaf[1]
                found |= {(task, str(name)) for task in self.given(of)}
                if isinstance(of, dict):
                    waiting.append(of)
                elif isinstance(of, str) and of not in self.ids:
                    columns.add((self.leaderboard, of))
        return found | self.through_columns(columns)

    def through_columns(self, columns: set[ColumnRef]) -> set[tuple[str, str]]:
        """What `columns` read, and the columns they read, by task."""
        found: set[tuple[str, str]] = set()
        seen: set[ColumnRef] = set()
        waiting = list(columns)
        while waiting:
            at = waiting.pop()
            if at in seen:
                continue
            seen.add(at)
            shape = self.checked.shapes.get(at[0], {}).get(at[1])
            if shape is not None:
                found |= shape.reads
                waiting.extend(shape.columns)
        return found

    def best_each(self, at: Path, node: Mapping[str, object]) -> None:
        source, named, directions = node["from"], node["value"], node["directions"]
        assert isinstance(source, dict) and isinstance(named, dict)
        assert isinstance(directions, dict)
        task, value = str(source["results"]), str(named["name"])
        scored = self.tasks.get(task)
        if scored is None:
            return
        self.resolve((*at, "value"), task, value)
        for name in directions:
            self.resolve((*at, "directions", str(name)), task, str(name))
        if value not in scored.values:
            return
        works = (
            f"The leaderboard {self.title} works {value} of {task} out again with best_each in "
            f"its column {self.column}"
        )
        direct, odd = direct_reads(scored.values[value])
        if odd is not None:
            self.refuse(
                (*at, "value"),
                task,
                value,
                f"best_each works {value} out again from the best of each value it reads "
                f"directly, but {value} reads {odd} directly, so no one best of it would be "
                "right. Give best_each a value that reads only other values and numbers.",
                f"{works}, so {value} must read only other values and numbers directly, not "
                f"{odd}. Change that column first.",
            )
            return
        missing = sorted(direct - {str(name) for name in directions})
        if missing:
            listed = ", ".join(missing)
            self.refuse(
                (*at, "directions"),
                task,
                value,
                f"{value} reads {listed} directly, so give each of them a direction here, desc "
                "for higher is better or asc for lower.",
                f"{works}, which gives no direction for {listed}. Give it one there first.",
            )
            return
        for each in [*sorted(direct), value]:
            if each in self.marked.get(task, frozenset()):
                self.refuse(
                    (*at, "value"),
                    task,
                    each,
                    f"best_each chooses the submissions of {task} it counts by the best of each "
                    f"value {value} reads, and {each} reads a mark staff enter, so the grading "
                    "queue would wait on the mark itself. Give best_each a value that reads no "
                    "mark.",
                    f"{works}, so {each} cannot read a mark staff enter: the grading queue "
                    "would wait on the mark itself. Change that column first.",
                )
                return


def _best_each_reads(node: Mapping[str, object]) -> set[tuple[str, str]]:
    """The task's value a `best_each` works out again, and each value its
    directions name, by task."""
    source, named, directions = node.get("from"), node.get("value"), node.get("directions")
    if not (isinstance(source, dict) and isinstance(named, dict)):
        return set()
    task = str(source.get("results"))
    names = {str(named.get("name")), *(directions if isinstance(directions, dict) else ())}
    return {(task, str(name)) for name in names}


def _facts() -> str:
    return ", ".join(FACTS[:-1]) + f" and {FACTS[-1]}"


def _values_of(values: Mapping[str, object]) -> str:
    if not values:
        return "It has no values."
    names = sorted(values)
    if len(names) == 1:
        return f"Its one value is {names[0]}."
    return f"Its values are {', '.join(names[:-1])} and {names[-1]}."


def marks_of(settings: ContestDefinition, entry: ContestTask) -> int | None:
    """How many submissions a row may mark: the entry's, or 1 when a board
    reads the task's `marked`, or none."""
    if entry.marks is not None:
        return entry.marks
    return 1 if leaderboards_of(settings).reads(entry.id, MARKED) else None


__all__ = [
    "MARKED",
    "Across",
    "Scored",
    "across",
    "check_contest",
    "hold_problems",
    "leaderboards_of",
    "marks_of",
]
