"""What a save of a task does to its contest, in the words an organiser reads:
the same lines ask the question a running contest puts before a save
publishes (#449), and answer a save that published (M66).

- The first line says whether anyone's result changes. Results change when
  the save grades a submission again, or when the task has a graded
  submission and the save moves what one earns or shows: a group's points,
  how a group earns them, a test's weight, what a test earns (`credit`), or
  when a group's results are shown. A change to the submission limits
  moves no result: a lowered `max` takes no result away and a raised one
  grades nothing by itself (#445), so they change only what entrants may
  still submit; nor does anything said under what entrants read.
- **Runs again:** how many submissions are graded again, and why, from what
  changed how the task grades.
- **Scores:** each scoring change, new and old, and each leaderboard counted
  again with it.
- **What entrants read:** whether the statement, the task's title or the
  files beside the statement change.

Every list holds at least one line, a "nothing changes" line when nothing
does, so a page lays the four parts out as they come. No line names a key
of a file: a group by its name, a number to the digit, a reported value by
its title where the workflow gives one (M58).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from forge.domain.board_checks import group_max
from forge.domain.boards import in_scope
from forge.domain.definitions import (
    ContestDefinition,
    ContestTask,
    Group,
    Leaderboard,
    Over,
    Rate,
    Relative,
    Show,
    TaskDefinition,
    worth_of,
)
from forge.domain.meanings import in_sentence
from forge.domain.scoring import ZERO, exact, rule_weight, written

RESULTS_CHANGE = "Results change."
NO_RESULT_CHANGES = "Nobody's result changes."
NOTHING_RUNS_AGAIN = "No submission runs again: the tests and how they run are the same."
FIRST_PUBLICATION = "No submission runs again: this is the task's first publication."
NONE_MADE = "No submission runs again: there are none yet."
NONE_REGRADED = "No submission runs again: none of those made is graded again."
UNREAD_SCORES = (
    "The latest publication's settings no longer read, so its scores cannot be compared: "
    "every result is scored as this save says."
)
NO_SCORE_CHANGES = "No score changes."
NOTHING_READ_CHANGES = "Nothing entrants read changes."
STATEMENT_CHANGES = "The statement changes."
PUBLIC_CHANGES = "The pictures and files beside the statement change."
FIRST_READ = "Entrants read the task from its release."

SHOWN = {
    Show.ALWAYS: "right away",
    Show.VERDICT: "as pass or fail right away, and in full once the task closes",
    Show.AFTER_CLOSE: "once the task closes",
}
"""When entrants see a group's results, in the words the task's form uses."""


@dataclass(frozen=True, slots=True)
class SaveEffect:
    """What a save does, laid out as a page shows it: the first line, then
    what runs again, the scores and what entrants read, each a list of
    sentences. `results_change` is the first line as a flag.
    """

    results_change: bool
    summary: str
    runs_again: tuple[str, ...]
    scores: tuple[str, ...]
    reading: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadChanges:
    """Which of what entrants read a save changes."""

    statement: bool = False
    public: bool = False


def effect_of(
    task: str,
    before: TaskDefinition | None,
    after: TaskDefinition,
    contest: ContestDefinition | None,
    *,
    changes: Sequence[str],
    regrades: int,
    graded: bool,
    read: ReadChanges,
    titles: Mapping[str, str] | None = None,
    submitted: bool = False,
    unread: bool = False,
) -> SaveEffect:
    """What saving `after` over the latest publication's `before` (none for
    a first publication, or `unread` when the latest publication's settings
    no longer read) does to the task named `task` in `contest`, given what
    changed how it grades, how many submissions that grades again, whether
    the task has a graded submission, and whether it has any; `titles` are
    what the workflow calls the values it reports, by key.
    """
    first = before is None and not unread
    scores, moves = _scores(task, before, after, contest, bool(changes), titles or {}, unread)
    changes_results = (bool(changes) and regrades > 0) or (graded and moves)
    return SaveEffect(
        results_change=changes_results,
        summary=RESULTS_CHANGE if changes_results else NO_RESULT_CHANGES,
        runs_again=_runs_again(first, changes, regrades, submitted),
        scores=tuple(scores) or (NO_SCORE_CHANGES,),
        reading=_reading(first, before, after, read),
    )


def _runs_again(
    first: bool, changes: Sequence[str], regrades: int, submitted: bool
) -> tuple[str, ...]:
    if first:
        return (FIRST_PUBLICATION,)
    if not changes:
        return (NOTHING_RUNS_AGAIN,)
    if regrades == 0:
        return (NONE_REGRADED if submitted else NONE_MADE, *changes)
    counted = (
        "1 submission is graded again"
        if regrades == 1
        else f"{regrades} submissions are graded again"
    )
    if len(changes) == 1:
        return (f"{counted}: {_lowered(changes[0])}",)
    return (f"{counted}, for these changes:", *changes)


def _lowered(sentence: str) -> str:
    """A sentence carried on after a colon: its first letter in lower case,
    unless the first word is a name such as a test group's.
    """
    first = sentence.split(" ", 1)[0]
    if first in {"The", "A", "An"}:
        return sentence[0].lower() + sentence[1:]
    return sentence


def _reading(
    first: bool, before: TaskDefinition | None, after: TaskDefinition, read: ReadChanges
) -> tuple[str, ...]:
    if first:
        return (FIRST_READ,)
    found: list[str] = []
    if before is not None and before.name != after.name:
        found.append(f"The task's title changes to {after.name} (was {before.name}).")
    if read.statement:
        found.append(STATEMENT_CHANGES)
    if read.public:
        found.append(PUBLIC_CHANGES)
    return tuple(found) or (NOTHING_READ_CHANGES,)


def _scores(
    task: str,
    before: TaskDefinition | None,
    after: TaskDefinition,
    contest: ContestDefinition | None,
    grading_changed: bool,
    titles: Mapping[str, str],
    unread: bool,
) -> tuple[list[str], bool]:
    """Each scoring change in words, then the boards it counts again, and
    whether any of it moves what a result earns or shows.
    """
    entry = contest.entry(task) if contest is not None else None
    # No board counts a withdrawn task, so a save of one moves none.
    boards = (
        [board for board in contest.leaderboards if board.covers(task)]
        if contest is not None and not (entry is not None and entry.withdrawn)
        else []
    )
    if before is None and unread:
        return [UNREAD_SCORES, _scored_again([board.name for board in boards])], True
    if before is None:
        found = _points_first(after, _worth(entry, after))
        found += [f"{task} now counts on the leaderboard {board.name}." for board in boards]
        return found, False
    worth_before, worth_after = _worth(entry, before), _worth(entry, after)
    points = _points(before, after, worth_before, worth_after)
    scales = (_scale(before, worth_before), _scale(after, worth_after))
    earning = [
        line
        for name, group in after.test_groups.items()
        if name in before.test_groups
        for line in _earning(name, before.test_groups[name], group, *scales)
    ]
    credit = _credit(before, after, titles)
    shown = [
        f"Results of {name} shown {SHOWN[group.shown]} (was {SHOWN[then.shown]})."
        for name, group in after.test_groups.items()
        if (then := before.test_groups.get(name)) is not None and then.shown != group.shown
    ]
    limit, rate = _submissions(before, after)
    rescored = bool(points or earning or credit)
    found = [*points, *earning, *credit, *shown, *limit, *rate]
    found += _boards(task, boards, before, after, rescored=rescored, regraded=grading_changed)
    return found, bool(rescored or shown)


def _worth(entry: ContestTask | None, definition: TaskDefinition) -> Fraction | None:
    if entry is None:
        return None
    worth = worth_of(entry, definition)
    return exact(worth) if worth is not None else None


def _points_first(after: TaskDefinition, worth: Fraction | None) -> list[str]:
    """Each group's most points, at a first publication."""
    if worth is None:
        return []
    listed = [
        f"{name} up to {written(points)}"
        for name, points in group_max(after.test_groups, worth).items()
        if points
    ]
    if not listed:
        return []
    listed[0] = listed[0].replace(" up to ", " is worth up to ", 1)
    return [f"Points: {_joined(listed)}."]


def _points(
    before: TaskDefinition,
    after: TaskDefinition,
    worth_before: Fraction | None,
    worth_after: Fraction | None,
) -> list[str]:
    """One line per group whose most points moved, the old number with it."""
    was = group_max(before.test_groups, worth_before) if worth_before is not None else {}
    now = group_max(after.test_groups, worth_after) if worth_after is not None else {}
    found: list[str] = []
    for name in after.test_groups:
        points, then = now.get(name, ZERO), was.get(name, ZERO)
        if name not in before.test_groups:
            if points:
                found.append(f"Points: {name} is new, worth up to {written(points)}.")
        elif points != then:
            found.append(
                f"Points: {name} gives no points now (was up to {written(then)})."
                if not points
                else f"Points: {name} is now worth up to {written(points)} (was {written(then)})."
            )
    for name in before.test_groups:
        if name not in after.test_groups and was.get(name):
            found.append(f"Points: {name} is gone (was worth up to {written(was[name])}).")
    return found


def _scale(definition: TaskDefinition, worth: Fraction | None) -> Fraction:
    """What one unit of rule weight is worth in points: the task's worth over
    its groups' total, or 1 where the task is in no contest that says.
    """
    total = sum((rule_weight(group) for group in definition.test_groups.values()), ZERO)
    return worth / total if worth is not None and total else Fraction(1)


def _parts(group: Group) -> list[tuple[str, Fraction]]:
    """Each way the group earns, with its rule weight, in the file's order."""
    parts: list[tuple[str, Fraction]] = []
    for kind, value in (("each", group.each), ("worst", group.worst), ("pass", group.pass_)):
        weight = exact(value)
        if weight:
            parts.append((kind, weight))
    return parts


def _part_words(kind: str, group: Group) -> str:
    if kind == "each":
        return "shared over its tests"
    if kind == "worst":
        return "by its lowest test score"
    share = exact(group.pass_at)
    if share is None or share >= 1:
        return "when every test passes"
    return f"when {written(share * 100)}% of its tests pass"


def _rule_words(group: Group, scale: Fraction) -> str:
    """How the group earns, part by part, each with its points."""
    return _joined(
        [f"{written(weight * scale)} {_part_words(kind, group)}" for kind, weight in _parts(group)]
    )


def _earning(
    name: str, before: Group, after: Group, scale_before: Fraction, scale_after: Fraction
) -> list[str]:
    """How the group earns its points, when its rules moved other than by
    its total alone, and its tests' weights when those moved.
    """
    found: list[str] = []
    then, now = rule_weight(before), rule_weight(after)
    if then and now:
        shape_before = [(kind, weight / then) for kind, weight in _parts(before)]
        shape_after = [(kind, weight / now) for kind, weight in _parts(after)]
        moved = shape_before != shape_after or (
            any(kind == "pass" for kind, _ in shape_after)
            and exact(before.pass_at) != exact(after.pass_at)
        )
        if moved:
            found.append(
                f"How {name} earns its points: {_rule_words(after, scale_after)} "
                f"(was {_rule_words(before, scale_before)})."
            )
    if (before.test_weights or {}) != (after.test_weights or {}):
        found.append(f"{name} weighs its tests differently.")
    return found


def _credit_words(credit: str | Relative | None, titles: Mapping[str, str]) -> str:
    """What each test earns, the value it reads named by its title, "its run
    time against the best any entrant reached", or by its key where it has
    none.
    """
    if credit is None:
        return "all or nothing, as it passes or fails"
    name = credit.relative if isinstance(credit, Relative) else credit
    said = in_sentence(titles[name]) if name in titles else name
    if isinstance(credit, Relative):
        return f"its {said} against the best any entrant reached"
    return f"the {said} it reports, from 0 to 1"


def _credit(before: TaskDefinition, after: TaskDefinition, titles: Mapping[str, str]) -> list[str]:
    if before.credit == after.credit:
        return []
    now, then = _credit_words(after.credit, titles), _credit_words(before.credit, titles)
    if now == then:
        return ["What each test earns is now read from another value the workflow reports."]
    return [f"What each test earns: {now} (was {then})."]


def _rate_words(rate: Rate) -> str:
    seconds = rate.per
    if seconds % 3600 == 0:
        unit, count = "hour", seconds // 3600
    elif seconds % 60 == 0:
        unit, count = "minute", seconds // 60
    else:
        unit, count = "second", seconds
    window = f"every {unit}" if count == 1 else f"every {count} {unit}s"
    return f"{rate.count} {window}"


def _submissions(before: TaskDefinition, after: TaskDefinition) -> tuple[list[str], list[str]]:
    then, now = before.submissions, after.submissions
    limit = (
        [f"Submission limit: {now.max} per entrant (was {then.max})."]
        if then.max != now.max
        else []
    )
    rate = (
        [f"Submission rate: {_rate_words(now.rate)} (was {_rate_words(then.rate)})."]
        if (then.rate.count, then.rate.per) != (now.rate.count, now.rate.per)
        else []
    )
    return limit, rate


def _boards(
    task: str,
    boards: Sequence[Leaderboard],
    before: TaskDefinition,
    after: TaskDefinition,
    *,
    rescored: bool,
    regraded: bool,
) -> list[str]:
    """Each board covering the task that the save counts again, and each
    whose scope a change to `show` moves.
    """
    named = [board.name for board in boards]
    found: list[str] = []
    if rescored:
        found.append(_scored_again(named))
    elif regraded and named:
        found.append(
            f"The leaderboard {named[0]} is counted again."
            if len(named) == 1
            else f"The leaderboards {_joined(named)} are counted again."
        )
    found += [
        f"A change to when entrants see results changes what the leaderboard {board.name} "
        f"counts of {task}."
        for board in boards
        if board.over is not Over.ALL and _scope_moved(board, before, after)
    ]
    return found


def _scored_again(named: Sequence[str]) -> str:
    if not named:
        return "Every result is scored again."
    if len(named) == 1:
        return f"Every result is scored again, and the leaderboard {named[0]} with it."
    return f"Every result is scored again, and the leaderboards {_joined(named)} with them."


def _scope_moved(board: Leaderboard, before: TaskDefinition, after: TaskDefinition) -> bool:
    """Whether a group of the task moved into or out of the board's scope."""
    scope = in_scope(board.over)
    return any(
        name in before.test_groups and scope(before.test_groups[name].shown) != scope(group.shown)
        for name, group in after.test_groups.items()
    )


def _joined(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def as_extra(effect: SaveEffect) -> Mapping[str, object]:
    """The effect as the members of a refusal that asks first."""
    return {
        "results_change": effect.results_change,
        "summary": effect.summary,
        "runs_again": [*effect.runs_again],
        "scores": [*effect.scores],
        "reading": [*effect.reading],
    }
