"""What a save of a task does to its contest, in the words an organiser reads:
the same lines ask the question a running contest puts before a save
publishes (#449), and answer a save that published (M66).

- The first line says whether anyone's result changes. Results change when
  the save grades a submission again, or when the task has a graded
  submission and the save moves what one shows: when a group's results are
  shown; for a run task, its groups, its values or when `show` shows a
  group or value. A task graded test by test gives no points since stage 5
  (section 6, item 123), so its rule weights, test weights and `credit`
  move nothing. A change to the submission limits
  moves no result: a lowered `max` takes no result away and a raised one
  grades nothing by itself (#445), so they change only what entrants may
  still submit; nor does anything said under what entrants read.
- **Runs again:** how many submissions are graded again, and why, from what
  changed how the task grades; for a run task, which of its steps run
  again, and that they wait for the save's check when it has one.
- **Scores:** for a run task, each group and value added, removed or
  changed, a value changed with what it reads, and each entry `show`
  shows at another time, then each leaderboard counted again with it; a
  value is named, never written out, since the editor shows its tree. For
  a task graded test by test, each group shown at another time and the
  submission limits.
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

from forge.domain.blocks.leaderboard_shape_check import LeaderboardsCheck, check_leaderboards
from forge.domain.blocks.tree_reads import reached
from forge.domain.checks import check_only
from forge.domain.definitions import ContestDefinition, Rate, Scoring, Show, TaskDefinition
from forge.domain.run_task import SHOWN_WORDS, show_names, shown_times
from forge.domain.yaml_models import joined

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
    the task has a graded submission, and whether it has any. For a run task,
    `steps` are the steps a submission runs again, `checked_first` that
    the regrade waits for the save's check to pass, and `programs` the
    values whose `exec` program runs again, each in a container per
    submission, which the save says the cost of.
    """
    first = before is None and not unread
    scores, moves = _scores(task, before, after, contest, unread)
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
            f", running {joined(steps)} again"
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
    unread: bool,
) -> tuple[list[str], bool]:
    """Each scoring change in words, then the boards it counts again, and
    whether any of it moves what a result is or shows.
    """
    entry = contest.entry(task) if contest is not None else None
    # No board counts a withdrawn task, so a save of one moves none; and a
    # board reads no task graded test by test (stage 5 section 6, item 70).
    checked = (
        check_leaderboards(contest.leaderboards, [each.id for each in contest.tasks])
        if contest is not None
        and not (entry is not None and entry.withdrawn)
        and after.scoring is not None
        else None
    )
    leaderboards = _titles(contest, checked, checked.reading(task)) if checked is not None else []
    if before is None and unread:
        return [UNREAD_SCORES, _scored_again(leaderboards)], True
    if after.scoring is not None:
        return _run_scores(task, before, after.scoring, contest, checked)
    if before is None:
        return [], False
    shown = [
        f"Results of {name} shown {SHOWN[group.shown]} (was {SHOWN[then.shown]})."
        for name, group in after.test_groups.items()
        if (then := before.test_groups.get(name)) is not None and then.shown != group.shown
    ]
    limit, rate = _submissions(before, after)
    return [*shown, *limit, *rate], bool(shown)


def _titles(
    contest: ContestDefinition | None, checked: LeaderboardsCheck | None, keys: Sequence[str]
) -> list[str]:
    """The titles of the boards `keys`, in the file's order."""
    if contest is None or checked is None:
        return []
    return [
        leaderboard.title_or(key)
        for key, leaderboard in contest.leaderboards.items()
        if key in keys
    ]


def _run_scores(
    task: str,
    before: TaskDefinition | None,
    after: Scoring,
    contest: ContestDefinition | None,
    checked: LeaderboardsCheck | None,
) -> tuple[list[str], bool]:
    """A run task's scoring changes in words: each group and value added,
    removed or changed, a value changed when its tree is or when a group or
    value it reads, directly or through others, changed; each entry `show`
    shows at another time; then the boards reading a value that changed,
    which count again, or at a first publication each board reading the
    task. Whether any of it moves what a result is or shows. A value is
    named, never written out: the editor shows its tree."""
    if before is None:
        named = _titles(contest, checked, checked.reading(task) if checked is not None else [])
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
            found.append(f"The value {name} changes with {joined(sorted(moved[name]))}.")
    found += [f"The value {name} is gone." for name in then.values if name not in after.values]
    rescored = bool(found)
    names = show_names(then) | show_names(after)
    shown = _shown_moves(then, after, names)
    found += shown
    if rescored:
        changed = (
            own
            | set(moved)
            | {name for name in after.values if name not in then.values}
            | {name for name in then.values if name not in after.values}
        )
        keys = (
            {key for name in changed for key in checked.reads(task, name)}
            if checked is not None
            else set()
        )
        found.append(_scored_again(_titles(contest, checked, sorted(keys))))
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
        seen = reached(reads[name], reads)
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


def _scored_again(named: Sequence[str]) -> str:
    if not named:
        return "Every result is scored again."
    if len(named) == 1:
        return f"Every result is scored again, and the leaderboard {named[0]} with it."
    return f"Every result is scored again, and the leaderboards {joined(named)} with them."


def as_extra(effect: SaveEffect) -> Mapping[str, object]:
    """The effect as the members of a refusal that asks first."""
    return {
        "results_change": effect.results_change,
        "summary": effect.summary,
        "runs_again": [*effect.runs_again],
        "scores": [*effect.scores],
        "reading": [*effect.reading],
    }
