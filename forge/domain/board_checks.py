"""What a board asks of the tasks it covers (TASK-FORMAT.md section 2: T8,
T9 and C4), checked from either side: a task's save against every board
covering it, with the other covered tasks as their latest publications give
them, and a contest's save against every covered task's latest publication.

- Every value a board ranks is a number each covered task reports per test
  with a `fold`, whose `better` resolves, the same direction and fold in
  every covered task.
- A value first on a board covering more than one task is `better: higher`
  with `at_least` at least 0 in each: a task that does not count adds 0 to
  a row's sum, so 0 has to be the worst any task can report.
- A value first is carried by a group of each covered task in the board's
  scope whose tests the scope reads: `always` for `live`, `after_close` for
  `after_close`, any group for `all`.
- A `marked` board's fallback, what the `live` scope showed before the
  reveal, carries its first key: a `live` group with a rule weight for
  `points`, of a task that gives points, an `always` group for a value.
- A board ranking `points` counts nothing from a task that gives none, or
  that has no group with a rule weight in its scope; that is reported, not
  refused.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from forge.domain.boards import PENALTY, POINTS, in_scope
from forge.domain.definitions import (
    ContestDefinition,
    Group,
    Leaderboard,
    Over,
    Select,
    Show,
    key_name,
)
from forge.domain.scoring import ZERO, Better, Measure, rule_weight, written


@dataclass(frozen=True, slots=True)
class Covered:
    """A covered task as a board reads it: what each per-test number means,
    and each group's `show` and whether it has a rule weight.
    """

    measures: Mapping[str, Measure]
    groups: Mapping[str, tuple[Show, bool]]

    @property
    def gives_points(self) -> bool:
        return any(weighted for _, weighted in self.groups.values())


def covered(measures: Mapping[str, Measure], groups: Mapping[str, Group]) -> Covered:
    return Covered(
        measures, {name: (group.shown, rule_weight(group) > 0) for name, group in groups.items()}
    )


@dataclass(frozen=True, slots=True)
class Breach:
    """What a board refuses of one covered task: which `order` key, none
    for its scope or its `select`, whether it is the task's `workflow` or
    its `test_groups` that would mend it, and why.
    """

    task: str
    key: int | None
    mended_in: str
    message: str


def covered_tasks(contest: ContestDefinition, board: Leaderboard) -> tuple[str, ...]:
    """The tasks a board covers among the contest's."""
    return tuple(entry.id for entry in contest.tasks if board.covers(entry.id))


def breaches(
    board: Leaderboard,
    tasks: Mapping[str, Covered],
    *,
    many: bool,
    only: str | None = None,
) -> list[Breach]:
    """Everything the board refuses of `tasks`, its covered tasks with a
    publication, in the contest's order; `many` when it covers more than
    one task of the contest. With `only`, just what is that task's, each
    comparison made against every other task as it stands; without, each
    task compared with those before it, so a disagreement is named once.
    """
    found: list[Breach] = []
    names = [key_name(key) for key in board.order]
    first = names[0]
    for index, name in enumerate(names):
        if name in (POINTS, PENALTY):
            continue
        found.extend(_value(board, tasks, index, name, many=many, only=only))
    for task, of in tasks.items():
        if only is not None and task != only:
            continue
        reported = first not in (POINTS, PENALTY) and of.measures.get(first) is not None
        if reported and not _carries_value(of, board.over):
            found.append(Breach(task, None, "test_groups", _scope_message(board, task, first)))
        if board.select is Select.MARKED and not _fallback_carries(of, first):
            found.append(Breach(task, None, "test_groups", _fallback_message(board, task, first)))
    return found


def counts_nothing(board: Leaderboard, tasks: Mapping[str, Covered]) -> list[str]:
    """The covered tasks a board ranking `points` first counts nothing from:
    those giving no points, or with no weighted group in its scope.
    """
    if key_name(board.order[0]) != POINTS:
        return []
    scope = in_scope(board.over)
    return [
        task
        for task, of in tasks.items()
        if not any(weighted and scope(show) for show, weighted in of.groups.values())
    ]


def counts_nothing_message(board: Leaderboard, task: str) -> str:
    return f"{board.name} counts nothing from {task}."


def _value(
    board: Leaderboard,
    tasks: Mapping[str, Covered],
    index: int,
    name: str,
    *,
    many: bool,
    only: str | None,
) -> list[Breach]:
    found: list[Breach] = []
    given = [(task, of.measures.get(name)) for task, of in tasks.items()]
    for position, (task, measure) in enumerate(given):
        if only is not None and task != only:
            continue
        against = given if only is not None else given[:position]
        if measure is None or measure.fold is None:
            found.append(
                Breach(
                    task,
                    index,
                    "workflow",
                    f"{board.name} ranks {name}, which {task} does not report per test "
                    "with a fold.",
                )
            )
            continue
        if measure.better is None:
            found.append(
                Breach(
                    task,
                    index,
                    "workflow",
                    f"{board.name} ranks {name}, which {task} gives no better, and a value "
                    "without one is never ranked.",
                )
            )
            continue
        other = _first_other(against, task, measure)
        if other is not None:
            message = _differs(board, name, task, measure, *other)
            found.append(Breach(task, index, "workflow", message))
            continue
        if index == 0 and many:
            problem = _sum_problem(board, name, measure)
            if problem is not None:
                found.append(Breach(task, index, "workflow", problem))
    return found


def _first_other(
    given: Sequence[tuple[str, Measure | None]], task: str, measure: Measure
) -> tuple[str, Measure] | None:
    """The first other covered task giving the value another direction or
    fold.
    """
    for other, theirs in given:
        if other == task:
            continue
        if theirs is None or theirs.fold is None or theirs.better is None:
            continue
        if theirs.better != measure.better or theirs.fold != measure.fold:
            return other, theirs
    return None


def _differs(
    board: Leaderboard, name: str, task: str, measure: Measure, other: str, theirs: Measure
) -> str:
    if theirs.better != measure.better:
        assert measure.better is not None and theirs.better is not None
        return (
            f"{board.name} ranks {name}, which is {measure.better} is better in {task} and "
            f"{theirs.better} is better in {other}."
        )
    assert measure.fold is not None and theirs.fold is not None
    return (
        f"{board.name} ranks {name}, which folds by {measure.fold} in {task} and by "
        f"{theirs.fold} in {other}."
    )


def _sum_problem(board: Leaderboard, name: str, measure: Measure) -> str | None:
    """C4's rule for a value first over more than one task."""
    if measure.better is Better.LOWER:
        return (
            f"{board.name} ranks {name}, which is lower is better, over more than one task: "
            "rank it on a board of one task."
        )
    if measure.at_least is None:
        return (
            f"{name} has no lower bound, so on {board.name} a row that skips a task would "
            "beat one that scored below 0 on it: rank it on a board of one task."
        )
    if measure.at_least < ZERO:
        return (
            f"{name} can be as low as {written(measure.at_least)}, so on {board.name} a row "
            "that skips a task would beat one that scored below 0 on it: rank it on a board "
            "of one task."
        )
    return None


def _carries_value(of: Covered, over: Over) -> bool:
    """Whether a group of the task in the board's scope has tests the scope
    reads: an `always` one for `live`, an `after_close` one for
    `after_close`, any for `all`.
    """
    shows = [show for show, _ in of.groups.values()]
    if over is Over.LIVE:
        return Show.ALWAYS in shows
    if over is Over.AFTER_CLOSE:
        return Show.AFTER_CLOSE in shows
    return bool(shows)


def _fallback_carries(of: Covered, first: str) -> bool:
    if first == POINTS:
        return not of.gives_points or any(
            weighted and show is not Show.AFTER_CLOSE for show, weighted in of.groups.values()
        )
    return any(show is Show.ALWAYS for show, _ in of.groups.values())


def _scope_message(board: Leaderboard, task: str, first: str) -> str:
    wanted = {
        Over.LIVE: "a group shown always, since a verdict group's tests show only at the reveal",
        Over.AFTER_CLOSE: "a group shown after_close",
        Over.ALL: "a group",
    }[board.over]
    return f"{board.name} ranks {first} over {board.over}, so {task} needs {wanted}."


def _fallback_message(board: Leaderboard, task: str, first: str) -> str:
    wanted = (
        "a group with a rule weight not shown after_close"
        if first == POINTS
        else "a group shown always"
    )
    return (
        f"{board.name} counts, for a row that marked nothing, what it saw before the "
        f"reveal, so {task} needs {wanted}."
    )


def group_max(groups: Mapping[str, Group], worth: Fraction) -> dict[str, Fraction]:
    """`max_g` of each group: `worth * R_g / W`, none on a task that gives
    no points.
    """
    total = sum((rule_weight(group) for group in groups.values()), ZERO)
    if total <= 0:
        return {}
    return {name: worth * rule_weight(group) / total for name, group in groups.items()}
