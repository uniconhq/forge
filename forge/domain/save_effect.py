"""What a save of a task does to its contest, in the words an organiser reads:
the same lines ask the question a running contest puts before a save
publishes (#449), and answer a save that published (M66).

- The first line says whether anyone's result changes. Results change when
  the save grades a submission again, or when the task has a graded
  submission and the save moves what one earns or shows: a group's points,
  how a group earns them, a test's weight, what a test earns (`credit`), or
  when a group's results are shown; for a run task, its groups, its values
  or when `show` shows a group or value. A change to the submission limits
  moves no result: a lowered `max` takes no result away and a raised one
  grades nothing by itself (#445), so they change only what entrants may
  still submit; nor does anything said under what entrants read.
- **Runs again:** how many submissions are graded again, and why, from what
  changed how the task grades; for a run task, which of its steps run
  again, and that they wait for the save's check when it has one.
- **Scores:** each scoring change, new and old, and each leaderboard counted
  again with it. For a run task, each group and value added, removed or
  changed, a value changed with what it reads, and each entry `show`
  shows at another time; a value is named, never written out, since the
  editor shows its tree.
- **What entrants read:** whether the statement, the task's title or the
  files beside the statement change; for a run task, each task file or
  folder `show` lists at another time, never one only a check names.

Every list holds at least one line, a "nothing changes" line when nothing
does, so a page lays the four parts out as they come. No line names a key
of a file: a group by its name, a number to the digit, a reported value by
its title where the workflow gives one (M58).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from forge.domain.blocks.numbers import exact, written
from forge.domain.board_checks import group_max
from forge.domain.boards import in_scope
from forge.domain.checks import check_only
from forge.domain.definitions import (
    ContestDefinition,
    ContestTask,
    Group,
    Leaderboard,
    Over,
    Rate,
    Relative,
    Scoring,
    Show,
    TaskDefinition,
    worth_of,
)
from forge.domain.meanings import in_sentence
from forge.domain.run_task import SHOWN_WORDS, show_names, shown_times
from forge.domain.scoring import ZERO, rule_weight

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
WORDS_CHANGE = "The names and units results give their values change."
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
    """Which of what entrants read a save changes: the statement, the files
    beside it, and the words the task's results give their values.
    """

    statement: bool = False
    public: bool = False
    words: bool = False


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
    steps: Sequence[str] | None = None,
    checked_first: bool = False,
    programs: Sequence[str] = (),
) -> SaveEffect:
    """What saving `after` over the latest publication's `before` (none for
    a first publication, or `unread` when the latest publication's settings
    no longer read) does to the task named `task` in `contest`, given what
    changed how it grades, how many submissions that grades again, whether
    the task has a graded submission, and whether it has any; `titles` are
    what the workflow calls the values it reports, by key. For a run task,
    `steps` are the steps a submission runs again, `checked_first` that
    the regrade waits for the save's check to pass, and `programs` the
    values whose `exec` program runs again, each in a container per
    submission, which the save says the cost of.
    """
    first = before is None and not unread
    scores, moves = _scores(task, before, after, contest, bool(changes), titles or {}, unread)
    changes_results = (bool(changes) and regrades > 0) or (graded and moves)
    return SaveEffect(
        results_change=changes_results,
        summary=RESULTS_CHANGE if changes_results else NO_RESULT_CHANGES,
        runs_again=(
            *_runs_again(first, changes, regrades, submitted, steps, checked_first),
            *(_program_cost(name, regrades) for name in programs if changes and regrades),
        ),
        scores=tuple(scores) or (NO_SCORE_CHANGES,),
        reading=_reading(first, before, after, read, _file_moves(before, after)),
    )


def _runs_again(
    first: bool,
    changes: Sequence[str],
    regrades: int,
    submitted: bool,
    steps: Sequence[str] | None,
    checked_first: bool,
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
    if steps is not None:
        counted += (
            f", running {_joined(steps)} again"
            if steps
            else ", from what is stored, running no step again"
        )
    if checked_first:
        counted = f"Once the save's check passes, {counted[0].lower()}{counted[1:]}"
    if len(changes) == 1:
        return (f"{counted}: {_lowered(changes[0])}",)
    return (f"{counted}, for these changes:", *changes)


def _program_cost(value: str, regrades: int) -> str:
    """What a value's program running again costs, said once per value."""
    counted = "1 submission" if regrades == 1 else f"{regrades:,} submissions"
    return f"The program of {value} works out this value again for {counted}, in a container each."


def _lowered(sentence: str) -> str:
    """A sentence carried on after a colon: its first letter in lower case,
    unless the first word is a name such as a test group's.
    """
    first = sentence.split(" ", 1)[0]
    if first in {"The", "A", "An"}:
        return sentence[0].lower() + sentence[1:]
    return sentence


def _reading(
    first: bool,
    before: TaskDefinition | None,
    after: TaskDefinition,
    read: ReadChanges,
    files: Sequence[str],
) -> tuple[str, ...]:
    if first:
        return (FIRST_READ,)
    found: list[str] = [*files]
    if before is not None and before.name != after.name:
        found.append(f"The task's title changes to {after.name} (was {before.name}).")
    if read.statement:
        found.append(STATEMENT_CHANGES)
    if read.public:
        found.append(PUBLIC_CHANGES)
    if read.words:
        found.append(WORDS_CHANGE)
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
    if after.scoring is not None:
        return _run_scores(task, before, after.scoring, boards)
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


def _run_scores(
    task: str, before: TaskDefinition | None, after: Scoring, boards: Sequence[Leaderboard]
) -> tuple[list[str], bool]:
    """A run task's scoring changes in words: each group and value added,
    removed or changed, a value changed when its tree is or when a group or
    value it reads, directly or through others, changed; each entry `show`
    shows at another time; then the boards it counts again. Whether any of
    it moves what a result is or shows. A value is named, never written
    out: the editor shows its tree."""
    named = [board.name for board in boards]
    if before is None:
        return [f"{task} now counts on the leaderboard {name}." for name in named], False
    then = before.scoring or Scoring()
    found: list[str] = []
    groups = _changed(then.groups, after.groups)
    for name in after.groups:
        if name not in then.groups:
            found.append(f"The group {name} is new.")
        elif name in groups:
            found.append(f"The group {name} now holds other runs.")
    found += [f"The group {name} is gone." for name in then.groups if name not in after.groups]
    own = _changed(then.values, after.values)
    moved = _moved_values(after.values, own | groups | (set(then.groups) - set(after.groups)))
    for name in after.values:
        if name not in then.values:
            found.append(f"The value {name} is new.")
        elif name in own:
            found.append(f"The value {name} is worked out another way.")
        elif name in moved:
            found.append(f"The value {name} changes with {_joined(sorted(moved[name]))}.")
    found += [f"The value {name} is gone." for name in then.values if name not in after.values]
    rescored = bool(found)
    names = show_names(then) | show_names(after)
    shown = _shown_moves(then, after, names)
    found += shown
    if rescored:
        found.append(_scored_again(named))
    return found, bool(rescored or shown)


def _changed(before: Mapping[str, object], after: Mapping[str, object]) -> set[str]:
    """The names both hold whose definitions differ."""
    return {name for name, tree in after.items() if name in before and before[name] != tree}


def _moved_values(values: Mapping[str, object], changed: set[str]) -> dict[str, set[str]]:
    """Each value that reads one of `changed`, a group or value, directly
    or through the values it reads, with the changed names it reads."""
    reads = {name: _read_names(tree) for name, tree in values.items()}
    found: dict[str, set[str]] = {}
    for name in values:
        seen: set[str] = set()
        waiting = [*reads[name]]
        while waiting:
            current = waiting.pop()
            if current in seen:
                continue
            seen.add(current)
            waiting.extend(reads.get(current, ()))
        hit = (seen - {name}) & changed
        if hit:
            found[name] = hit
    return found


def _read_names(node: object) -> set[str]:
    """Every group and value a value's tree names: a fold's `{runs: g}`, a
    run's `{run: [g, column]}` and another value's `{value: name}`."""
    if isinstance(node, list):
        return {name for each in node for name in _read_names(each)}
    if not isinstance(node, Mapping):
        return set()
    found: set[str] = set()
    for key, each in node.items():
        if key in ("value", "runs") and isinstance(each, str):
            found.add(each)
        elif key == "run" and isinstance(each, list) and each and isinstance(each[0], str):
            found.add(each[0])
        else:
            found |= _read_names(each)
    return found


def _shown_moves(before: Scoring, after: Scoring, names: set[str]) -> list[str]:
    """Each group or value, of `names`, that `show` shows at another time,
    or no longer shows."""
    then = {entry: time for entry, time in shown_times(before.show).items() if entry in names}
    now = {entry: time for entry, time in shown_times(after.show).items() if entry in names}
    found: list[str] = []
    for entry, time in now.items():
        was = then.get(entry)
        if was is None:
            found.append(f"Entrants now see {entry} {SHOWN_WORDS[time]}.")
        elif was != time:
            found.append(f"Entrants see {entry} {SHOWN_WORDS[time]} (was {SHOWN_WORDS[was]}).")
    found += [
        f"Entrants no longer see {entry} (was {SHOWN_WORDS[was]})."
        for entry, was in then.items()
        if entry not in now
    ]
    return found


FILE_WORDS = {
    "now": "during the contest",
    "after_close": "from the reveal",
    "on_release": "once results are released",
}
"""When entrants may read a task file `show` lists, by its time."""


def _file_moves(before: TaskDefinition | None, after: TaskDefinition) -> list[str]:
    """Each task file or folder a run task's `show` lists at another time,
    or no longer lists, but one only a check names, which is never shown
    (`checks.check_only`). What entrants read, never a result."""
    if before is None or before.scoring is None or after.scoring is None:
        return []
    names = show_names(before.scoring) | show_names(after.scoring)

    def files(definition: TaskDefinition, scoring: Scoring) -> dict[str, str]:
        return {
            entry: time
            for entry, time in shown_times(scoring.show).items()
            if entry not in names and not check_only(definition, entry)
        }

    then, now = files(before, before.scoring), files(after, after.scoring)
    found: list[str] = []
    for entry, time in now.items():
        was = then.get(entry)
        if was is None:
            found.append(f"Entrants can read {entry} {FILE_WORDS[time]}.")
        elif was != time:
            found.append(f"Entrants can read {entry} {FILE_WORDS[time]} (was {FILE_WORDS[was]}).")
    found += [
        f"Entrants can no longer read {entry} (was {FILE_WORDS[was]})."
        for entry, was in then.items()
        if entry not in now
    ]
    return found


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
