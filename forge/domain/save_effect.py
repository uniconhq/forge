"""What a save of a task does to its contest, in the words an organiser reads:
the same lines ask the question a running contest puts before a save
publishes (#449), and answer a save that published (M66).

- The first line says whether anyone's result changes. Results change when
  the save grades a submission again, or when the task has a graded
  submission and the save moves what one shows: its groups, its values or
  when `show` shows a value, an output or a run's result. A change to the submission limits
  moves no result: a lowered `max` takes no result away and a raised one
  grades nothing by itself (#445), so they change only what entrants may
  still submit; nor does anything said under what entrants read.
- **Runs again:** how many submissions are graded again, and why, from what
  changed how the task grades; which of its steps run again, and that they
  wait for the save's check when it has one.
- **Scores:** each group and value added, removed or changed, a value
  changed with what it reads, and each value, output and run's result
  `show` shows at another moment,
  then each leaderboard counted again with it, then the submission limit
  and rate when they change; a value is named, never written out, since
  the editor shows its tree.
- **What entrants read:** whether the statement, the task's title or the
  files beside the statement, in `public/`, change, and each input `show`
  shows at another moment, a setting or a run's own.

Every list holds at least one line, a "nothing changes" line when nothing
does, so a page lays the four parts out as they come. No line names a key
of a file: a group or a value by its name (M58).
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from forge.domain.blocks.leaderboard_shape_check import LeaderboardsCheck, check_leaderboards
from forge.domain.blocks.tree_reads import reached
from forge.domain.definitions import (
    AFTER_DEADLINE,
    AFTER_RELEASE,
    ALWAYS,
    ContestDefinition,
    Rate,
    Scoring,
    TaskDefinition,
)
from forge.domain.run_task import listed, picks, shown_when
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
FIRST_READ = "Entrants read the task from its release."


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
    """Which of what entrants read a save changes: the statement, and the
    files beside it.
    """

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
    the task has a graded submission, and whether it has any. `steps` are
    the steps a submission runs again, `checked_first` that
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
        reading=_reading(first, before, after, read, _input_moves(before, after)),
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
    unless the first word is a name such as a group's.
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
    return tuple(found) or (NOTHING_READ_CHANGES,)


def _scores(
    task: str,
    before: TaskDefinition | None,
    after: TaskDefinition,
    contest: ContestDefinition | None,
    unread: bool,
) -> tuple[list[str], bool]:
    """Each scoring change in words, then the leaderboards it counts again, and
    whether any of it moves what a result is or shows.
    """
    entry = contest.entry(task) if contest is not None else None
    # No leaderboard counts a withdrawn task, so a save of one moves none.
    checked = (
        check_leaderboards(contest.leaderboards, [each.id for each in contest.tasks])
        if contest is not None and not (entry is not None and entry.withdrawn)
        else None
    )
    leaderboards = _titles(contest, checked, checked.reading(task)) if checked is not None else []
    if before is None and unread:
        return [UNREAD_SCORES, _scored_again(leaderboards)], True
    found, moves = _run_scores(
        task, before, after.scoring, contest, checked, _columns(before) | _columns(after)
    )
    if before is None:
        return found, moves
    # What an entrant may still submit moves no result.
    return [*found, *_submissions(before, after)], moves


def _titles(
    contest: ContestDefinition | None, checked: LeaderboardsCheck | None, keys: Sequence[str]
) -> list[str]:
    """The titles of the leaderboards `keys`, in the file's order."""
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
    columns: Collection[str] = (),
) -> tuple[list[str], bool]:
    """A task's scoring changes in words: each group and value added,
    removed or changed, a value changed when its tree is or when a group or
    value it reads, directly or through others, changed; each value, output
    and run's result `show` shows at another moment, `columns` the task's
    repeated inputs; then the leaderboards reading a value that changed,
    which count again, or at a first publication each leaderboard reading the
    task. Whether any of it moves what a result is or shows. A value is
    named, never written out: the editor shows its tree."""
    if before is None:
        named = _titles(contest, checked, checked.reading(task) if checked is not None else [])
        return [f"{task} now counts on the leaderboard {name}." for name in named], False
    then = before.scoring
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
    shown = _moves(_shown(then, columns, inputs=False), _shown(after, columns, inputs=False), "see")
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


def _columns(definition: TaskDefinition | None) -> set[str]:
    """The task's repeated inputs, a run's own inputs."""
    if definition is None:
        return set()
    return {name for axis in definition.repeat for name in axis.inputs}


def _shown(
    scoring: Scoring, columns: Collection[str], *, inputs: bool
) -> dict[str, frozenset[str]]:
    """Each thing `show` shows, in words, with the moments it shows it at:
    its values and outputs, and each field of a run entry that is a run's
    result, as `score of sample`; or, with `inputs`, its settings and each
    field of a run entry that is one of `columns`, the run's own inputs."""
    show = scoring.show
    found = (
        dict(listed(show, "inputs"))
        if inputs
        else {**listed(show, "values"), **listed(show, "outputs")}
    )
    for of, fields in picks(show).items():
        for name, moments in fields.items():
            if (name in columns) == inputs:
                found[f"{name} of {of}"] = moments
    return found


def _moves(
    before: Mapping[str, frozenset[str]],
    after: Mapping[str, frozenset[str]],
    verb: str,
    words: Callable[[frozenset[str]], str] = shown_when,
) -> list[str]:
    """Each thing `after` shows at other moments than `before`, or no longer
    shows, as entrants `verb` it, its moments in `words`."""
    found: list[str] = []
    for entry, moments in after.items():
        was = before.get(entry)
        if was is None:
            found.append(f"Entrants now {verb} {entry} {words(moments)}.")
        elif was != moments:
            found.append(f"Entrants {verb} {entry} {words(moments)} (was {words(was)}).")
    found += [
        f"Entrants no longer {verb} {entry} (was {words(was)})."
        for entry, was in before.items()
        if entry not in after
    ]
    return found


INPUT_WORDS = {
    ALWAYS: "with the statement",
    AFTER_DEADLINE: "after the deadline",
    AFTER_RELEASE: "once results are released",
}
"""When entrants may read an input `show` lists, by its moment: `always`
is whenever the statement is visible, since a run's inputs are needed
before anyone submits."""


def _input_moves(before: TaskDefinition | None, after: TaskDefinition) -> list[str]:
    """Each input `show` shows at other moments, or no longer shows: a
    setting, or a run's own input as a run entry's field. What entrants
    read, never a result."""
    if before is None:
        return []
    columns = _columns(before) | _columns(after)
    return _moves(
        _shown(before.scoring, columns, inputs=True),
        _shown(after.scoring, columns, inputs=True),
        "read",
        lambda moments: " or ".join(
            words for moment, words in INPUT_WORDS.items() if moment in moments
        ),
    )


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


def _submissions(before: TaskDefinition, after: TaskDefinition) -> list[str]:
    """The submission limit and rate a save changes, in words."""
    then, now = before.submissions, after.submissions
    found = []
    if then.max != now.max:
        found.append(f"Submission limit: {now.max} per entrant (was {then.max}).")
    if (then.rate.count, then.rate.per) != (now.rate.count, now.rate.per):
        found.append(f"Submission rate: {_rate_words(now.rate)} (was {_rate_words(then.rate)}).")
    return found


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
