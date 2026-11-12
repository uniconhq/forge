"""What each reader sees of a task's result, and when (SIMPLER-CORE.md
9.3, 9.4, 15 and 17): the one function every path that serves one reads
(`shown`), so there is one place to get it wrong.

An entrant sees, at each moment, what `show` lists under a moment that has
come: `always` at once, `after_deadline` once the task has closed for
everyone, `after_release` while its results are released. The moments are
separate: a release before the deadline shows `always` and
`after_release`, and `after_deadline`'s list waits for the deadline.
Anything not named is never shown: a workflow version adding an output
shows nothing by itself.

**Until the deadline, a release, or a hidden run's results shown, an
entrant reads the row's shown runs alone (#648):** those runs, the values
worked out from them, what its steps done once gave and `compiled`, as if
no other run were graded. A fault on a hidden run alone is not told then:
with nothing they read waiting, their submission reads as graded (#657).
From then on they read every run. A hidden run's results are shown before
the deadline only when a save moved them to `always` (phase 4.10). A value
a save moved to `always` that reads, directly or through other values, a
run this grading's plan did not show is missing until then, not worked out
with that run as never run (stage 5 section 6, item 134).

- **A value** listed is shown with its number, missing as none. One that
  waits for staff is told so ("being checked") only as its runs are: when
  every run it waits on is early, or shown; otherwise it is left out until
  they are. Each carries whether it reads, directly or through others, a
  column the entrant could set (#504). A value reading an entered mark is
  worked out by the caller as if no mark were entered until a leaderboard
  cell shown to the entrant counted the submission, and never when none
  did (`forge.domain.leaderboards.entries_open`, SIMPLER-CORE.md 14), so it
  reads then as on a submission with no mark.
- **The shared inputs** listed under `inputs`, the task's settings, by
  name: a scalar as it is, a file or folder as `{file: <path>}` or
  `{folder: <path>}`, served by its own route.
- **A run entry** shows the runs it picks that a row holds, with the
  fields it names and nothing else: each run with the entry's `of` and its
  place there from 1 ("sample, run 2"), never its id, which counts every
  run of the task; one tuple per axis, its title and the inputs of that
  axis the entry names; and of its result, `verdict`, `passed`, `label` and
  `message` where named, and each output named, a number by the title and
  unit its workflow gives it. One `of` listed at two moments that have
  come shows the fields of both. **`first_not_passed`** shows, of the runs
  the entrant may see now, the first in the task's order with a result
  whose `passed` is not 1, at place 1, labelled by the first group of
  `scoring.groups` holding it and its place there, or its place in `all`
  (`labelled`); nothing when every run passed.
- **What the steps done once gave** of the rows the entrant reads, each
  output `outputs` lists by its name, the compile log among them, and
  **`compiled`** when it lists that, none while it waits for its compile
  to run (`forge.domain.run_results`).
- `staff` outputs never.

A text is a reference to a stored output, which may be another entrant's
of the same org (stage 4 open 114), and whose id says when it was made.
So an entrant is never sent one (`bare`): a text is read by where it is
shown, its run's group and place, or the steps done once, and its name
(`TextAt`), which the server turns into the output (`addresses`).

A task none of whose results is shown during the contest, `always` and,
while released, `after_release`, has nothing to tell before the deadline
(`later`): its entrant's submission reads "Submitted, results after the
contest" (stage 4 open 128).

Organisers read everything at once from the row: every value with its
outcome, the moment `show` lists it under and what it read, every setting,
every run the row holds under `all` with its id, every field and its texts'
outputs, a run the live publication dropped after them marked `dropped`,
everything done once, and the facts. The staff preview counts the runs
that holds and an entrant's answer leaves out (`Shown.ids`).
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, NamedTuple

from forge.domain.blocks.catalogue import ALL, SHOWN_FACTS
from forge.domain.blocks.evaluate import SYSTEM_ERROR
from forge.domain.definitions import AFTER_DEADLINE, AFTER_RELEASE, ALWAYS, Scoring, ShowTimes
from forge.domain.run_results import RunGrading, Worked
from forge.domain.run_task import (
    FIRST_NOT_PASSED,
    RUN_FIELDS,
    Run,
    holds,
    key_json,
    listed,
    members,
    picks,
    results,
    value_moment,
)
from forge.domain.types import Type

ONE_TEXT = ("label", "message")
"""The texts of a run that are one each, beside those named in `texts`."""


class TextAt(NamedTuple):
    """Where an entrant is shown a text: its run's group and place, both
    none for the steps done once; `texts` with the text's name, or `label`
    or `message` with no name.
    """

    group: str | None
    place: int | None
    field: str
    name: str | None


@dataclass(frozen=True, slots=True)
class ValueShown:
    """One value as a reader sees it: its name, its number, none when it is
    missing or waiting, whether it waits for staff, and whether it reads a
    column the entrant could set. Organisers also read the first moment
    `show` lists it under, none when never, and what it read: the runs by
    id, the values and the facts.
    """

    name: str
    number: Fraction | None
    waiting: bool
    self_reported: bool
    show: str | None = None
    runs: tuple[str, ...] | None = None
    values: tuple[str, ...] | None = None
    facts: tuple[str, ...] | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class Shown:
    """A task's grading as one reader sees it now: its values, its runs
    by group and place, what the steps done once gave (`reports` and
    `texts`), `compiled`, none while not shown or while it waits, whether
    nothing of the task's results is shown before its deadline, and the
    shared inputs shown. Organisers also read the facts. `ids` are the runs
    shown, by id, which no reader is sent: what the staff preview counts
    the hidden runs against. `settled` when the reader reads the shown runs
    alone and nothing they read waits, no shown run, value or `compiled`:
    a fault elsewhere in the grading is not told yet (#657).
    """

    values: tuple[ValueShown, ...]
    runs: tuple[Mapping[str, Any], ...]
    once: Mapping[str, Any]
    compiled: int | None
    later: bool
    facts: Mapping[str, Fraction] = field(default_factory=dict)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    ids: frozenset[str] = frozenset()
    settled: bool = False


@dataclass(frozen=True, slots=True)
class TaskInputs:
    """What a task's page shows of its inputs now, before any submission:
    each setting `show` lists under `inputs`, by name, and each run whose
    inputs a run entry shows, by the entry's `of` and its place there,
    with its tuples' titles and those inputs. A file or folder is
    `{file: <path>}` or `{folder: <path>}`, served by its own route.
    """

    inputs: Mapping[str, Any] = field(default_factory=dict)
    runs: tuple[Mapping[str, Any], ...] = ()


def times_of(*, revealed: bool, released: bool) -> tuple[str, ...]:
    """The moments of `show` that have come for an entrant."""
    return (
        ALWAYS,
        *((AFTER_DEADLINE,) if revealed else ()),
        *((AFTER_RELEASE,) if released else ()),
    )


def later(scoring: Scoring, columns: Collection[str], *, released: bool) -> bool:
    """Whether nothing of a grading's results is shown during the contest:
    no value, no output and no run entry showing more than the runs'
    inputs, `columns` the task's repeated inputs."""
    moments = (ALWAYS, AFTER_RELEASE) if released else (ALWAYS,)
    show = scoring.show
    return not (
        listed(show, "values", moments)
        or listed(show, "outputs", moments)
        or any(results(of, fields, columns) for of, fields in picks(show, moments).items())
    )


def shown(
    scoring: Scoring,
    runs: Sequence[Run],
    whole: Worked,
    early: Callable[[], Worked],
    self_reported: Collection[str],
    *,
    revealed: bool,
    released: bool = False,
    everything: bool = False,
    hidden: Collection[str] = (),
    titles: Mapping[str, tuple[str | None, ...]] | None = None,
    axes: Sequence[Sequence[str]] = (),
    meanings: Mapping[str, tuple[str | None, str | None]] | None = None,
    settings: Mapping[str, object] | None = None,
    kinds: Mapping[str, Type] | None = None,
) -> Shown:
    """What a reader sees of a grading now: `scoring` and `runs` the task's
    live publication's, `whole` the grading worked out from its row and
    `early` giving it from its shown runs alone, called only before the
    deadline and the release, `self_reported` the values reading a column
    the entrant could set, and `hidden` the runs the grading's plan did not
    show;
    for an entrant, whether the task's deadline has passed for everyone
    (`revealed`) and whether its results are released; with `everything`,
    as organisers read it. `titles` is each run's tuple titles, one per
    axis; `axes` the column names of each axis, in the task's order, so a
    served run pairs each title with that axis's columns; `meanings` each
    output's title and unit, the name standing in where a title was not
    given; `settings` the task's settings by name, and `kinds` each workflow
    input's type, so a file or folder is told as one. Texts are as stored:
    `bare` takes them out for an entrant.
    """
    named = titles or {}
    columns = tuple(tuple(axis) for axis in axes)
    words = meanings or {}
    given = settings or {}
    types = kinds or {}
    if everything:
        return _everything(scoring, runs, whole, self_reported, named, columns, words, given, types)
    moments = times_of(revealed=revealed, released=released)
    inputs = {name for axis in columns for name in axis}
    view = whole
    unseen: frozenset[str] = frozenset()
    rows = _rows(scoring, runs, whole.grading, moments, inputs, named, columns, words, types)
    told = {run for run, row, result in rows if result}
    alone = not (revealed or released or told - whole.grading.early)
    if alone:
        view = early()
        unseen = frozenset(hidden)
    rows = _rows(scoring, runs, view.grading, moments, inputs, named, columns, words, types)
    rows += _first_not_passed(scoring, runs, view.grading, moments, named, columns, words, types)
    grading = view.grading
    seen = {run for run, row, result in rows if result}
    show = scoring.show
    named_values = listed(show, "values", moments)
    values: list[ValueShown] = []
    for name in scoring.values:
        outcome = view.outcomes[name]
        if name not in named_values:
            continue
        if outcome.runs & unseen:
            # Moved to `always` after this grading: missing until its hidden runs show.
            values.append(ValueShown(name, None, False, name in self_reported))
            continue
        if outcome.waiting:
            waits = {run for run in outcome.runs if grading.read[run].verdict == SYSTEM_ERROR}
            if not waits <= grading.early | seen:
                continue
        values.append(
            ValueShown(
                name,
                outcome.number,
                outcome.waiting,
                name in self_reported,
                text=outcome.text,
            )
        )
    outputs = listed(show, "outputs", moments)
    compiled = set(outputs) & set(SHOWN_FACTS)
    waiting = (
        any(grading.read[run].verdict == SYSTEM_ERROR for run in grading.early)
        or any(value.waiting for value in values)
        or (bool(compiled) and grading.compiled is None)
    )
    return Shown(
        tuple(values),
        tuple(row for _, row, _ in rows),
        _once(grading.once, set(outputs) - set(SHOWN_FACTS)),
        grading.compiled if compiled else None,
        later(scoring, inputs, released=released) and not revealed,
        inputs=_settings(show, given, types, moments),
        ids=frozenset(run for run, _, _ in rows),
        settled=alone and not waiting,
    )


def task_inputs(
    scoring: Scoring,
    runs: Sequence[Run],
    *,
    revealed: bool,
    released: bool = False,
    titles: Mapping[str, tuple[str | None, ...]] | None = None,
    axes: Sequence[Sequence[str]] = (),
    settings: Mapping[str, object] | None = None,
    kinds: Mapping[str, Type] | None = None,
) -> TaskInputs:
    """What the task's page shows of its inputs now (`TaskInputs`), for a
    reader who may see its statement: `always` whenever they may, the
    later moments as `shown` reads them. A run entry of `first_not_passed`
    picks no run before a submission, so it shows none here.
    """
    moments = times_of(revealed=revealed, released=released)
    columns = tuple(tuple(axis) for axis in axes)
    inputs = {name for axis in columns for name in axis}
    named = titles or {}
    types = kinds or {}
    keys = {run.id: run.key for run in runs}
    by_id = {run.id: run for run in runs}
    found: list[Mapping[str, Any]] = []
    for of, fields in picks(scoring.show, moments).items():
        wanted = [name for name in fields if name in inputs]
        if of == FIRST_NOT_PASSED or not wanted:
            continue
        for place, run in enumerate(members(of, scoring, keys), start=1):
            key = _inputs_of(by_id[run], wanted, types)
            found.append(
                {"group": of, "place": place, "tuples": _tuples(key, named.get(run), columns)}
            )
    return TaskInputs(_settings(scoring.show, settings or {}, types, moments), tuple(found))


def shown_file(
    inputs: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    name: str,
    group: str | None = None,
    place: int | None = None,
) -> tuple[Type, str] | None:
    """The file or folder an answer shows as the input `name`, with which it
    is and its path in the task: a shared input of `inputs` with no
    `group`, or the input of the run of `runs` at `group` and `place`; none
    when the answer shows no such file or folder.
    """
    if group is None:
        return _file_of(inputs.get(name))
    for run in runs:
        if run.get("group") == group and run.get("place") == place:
            for each in run.get("tuples") or ():
                key = each.get("key") or {}
                if name in key:
                    return _file_of(key[name])
    return None


def _file_of(value: object) -> tuple[Type, str] | None:
    if isinstance(value, Mapping) and len(value) == 1:
        for kind in (Type.FILE, Type.FOLDER):
            path = value.get(kind.value)
            if isinstance(path, str):
                return kind, path
    return None


def texts(runs: Sequence[Mapping[str, Any]], once: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Every text the `runs` and `once` organisers are shown name, as its
    output and that output's name: the only texts they may read.
    """
    found: set[tuple[str, str]] = set()
    named: list[object] = [*(once.get("texts") or {}).values()]
    for run in runs:
        named += [*(run.get("texts") or {}).values(), *(run.get(each) for each in ONE_TEXT)]
    for each in named:
        if _reference(each):
            assert isinstance(each, Mapping)
            found.add((str(each["output"]), str(each["name"])))
    return found


def addresses(
    runs: Sequence[Mapping[str, Any]], once: Mapping[str, Any]
) -> dict[TextAt, tuple[str, str]]:
    """Every text an entrant shown `runs` and `once` may read, by where it
    is shown, with the output and name that hold it."""
    found: dict[TextAt, tuple[str, str]] = {}

    def add(at: TextAt, each: object) -> None:
        if _reference(each):
            assert isinstance(each, Mapping)
            found[at] = (str(each["output"]), str(each["name"]))

    for name, each in (once.get("texts") or {}).items():
        add(TextAt(None, None, "texts", str(name)), each)
    for run in runs:
        group, place = str(run["group"]), int(run["place"])
        for name, each in (run.get("texts") or {}).items():
            add(TextAt(group, place, "texts", str(name)), each)
        for one in ONE_TEXT:
            add(TextAt(group, place, one, None), run.get(one))
    return found


def bare(
    runs: Sequence[Mapping[str, Any]], once: Mapping[str, Any]
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """`runs` and `once` as an entrant is sent them: each text there, with
    no output or name, read by where it is shown (`TextAt`)."""
    plain = tuple(
        {
            **run,
            **({"texts": {name: {} for name in run["texts"]}} if run.get("texts") else {}),
            **{one: {} for one in ONE_TEXT if run.get(one) is not None},
        }
        for run in runs
    )
    named = once.get("texts") or {}
    return plain, {**once, "texts": {name: {} for name in named}}


def _reference(each: object) -> bool:
    return isinstance(each, Mapping) and {"output", "name"} <= set(each)


def _everything(
    scoring: Scoring,
    runs: Sequence[Run],
    whole: Worked,
    self_reported: Collection[str],
    titles: Mapping[str, tuple[str | None, ...]],
    axes: Sequence[Sequence[str]],
    meanings: Mapping[str, tuple[str | None, str | None]],
    settings: Mapping[str, object],
    kinds: Mapping[str, Type],
) -> Shown:
    grading, outcomes = whole.grading, whole.outcomes
    values = tuple(
        ValueShown(
            name,
            outcome.number,
            outcome.waiting,
            name in self_reported,
            value_moment(scoring.show, name),
            tuple(run.id for run in runs if run.id in outcome.runs),
            tuple(sorted(outcome.values)),
            tuple(sorted(outcome.facts)),
            text=outcome.text,
        )
        for name, outcome in ((name, outcomes[name]) for name in scoring.values)
    )
    ran = [
        (run.id, _inputs_of(run, None, kinds), grading.results[run.id], False)
        for run in runs
        if run.id in grading.results
    ]
    ran += [
        (name, dict(entry.get("key") or {}), entry, True)
        for name, entry in sorted(grading.dropped.items())
    ]
    shown_runs = tuple(
        {
            "group": ALL,
            "place": place,
            "id": name,
            "tuples": _tuples(key, titles.get(name), axes),
        }
        | _fields(entry, None, meanings)
        | ({"dropped": True} if dropped else {})
        for place, (name, key, entry, dropped) in enumerate(ran, start=1)
    )
    return Shown(
        values,
        shown_runs,
        _once(grading.once),
        grading.compiled,
        later=False,
        facts=dict(whole.facts),
        inputs={name: _input(value, kinds.get(name)) for name, value in settings.items()},
        ids=frozenset(name for name, _, _, dropped in ran if not dropped),
    )


def _rows(
    scoring: Scoring,
    runs: Sequence[Run],
    grading: RunGrading,
    moments: Collection[str],
    inputs: Collection[str],
    titles: Mapping[str, tuple[str | None, ...]],
    axes: Sequence[Sequence[str]],
    meanings: Mapping[str, tuple[str | None, str | None]],
    kinds: Mapping[str, Type],
) -> list[tuple[str, dict[str, Any], bool]]:
    """Each run a run entry listed at one of `moments` shows that a row
    holds, `first_not_passed` aside, in the order listed, with its id,
    what is shown of it, and whether that is more than its inputs.
    """
    keys = {run.id: run.key for run in runs}
    by_id = {run.id: run for run in runs}
    found: list[tuple[str, dict[str, Any], bool]] = []
    for of, fields in picks(scoring.show, moments).items():
        if of == FIRST_NOT_PASSED:
            continue
        for place, run in enumerate(members(of, scoring, keys), start=1):
            result = grading.results.get(run)
            if result is None:
                continue
            row = _row(by_id[run], of, place, result, fields, titles, axes, meanings, kinds)
            found.append((run, row, results(of, fields, inputs)))
    return found


def _first_not_passed(
    scoring: Scoring,
    runs: Sequence[Run],
    grading: RunGrading,
    moments: Collection[str],
    titles: Mapping[str, tuple[str | None, ...]],
    axes: Sequence[Sequence[str]],
    meanings: Mapping[str, tuple[str | None, str | None]],
    kinds: Mapping[str, Type],
) -> list[tuple[str, dict[str, Any], bool]]:
    """The run `first_not_passed` shows, when a run entry listed at one of
    `moments` picks it: of the runs `grading` holds, the first in the
    task's order with a result, not waiting on a fault, whose `passed`,
    stored as 0 or 1, is not 1; none when every one passed.
    """
    fields = picks(scoring.show, moments).get(FIRST_NOT_PASSED)
    if fields is None:
        return []
    for run in runs:
        result = grading.results.get(run.id)
        if result is None or grading.read[run.id].verdict == SYSTEM_ERROR:
            continue
        if result.get("passed") != 1:
            row = _row(run, FIRST_NOT_PASSED, 1, result, fields, titles, axes, meanings, kinds)
            return [(run.id, row | {"labelled": _labelled(scoring, runs, run)}, True)]
    return []


def _labelled(scoring: Scoring, runs: Sequence[Run], run: Run) -> dict[str, Any]:
    """The group and place a run is told by where `first_not_passed` shows
    it: the first group of `scoring.groups` holding it and its place
    there, from 1, or its place in `all` when no group holds it.
    """
    keys = {each.id: each.key for each in runs}
    for name, picked in scoring.groups.items():
        if holds(picked, run.key):
            return {"group": name, "place": members(name, scoring, keys).index(run.id) + 1}
    return {"group": ALL, "place": list(keys).index(run.id) + 1}


def _row(
    run: Run,
    of: str,
    place: int,
    result: Mapping[str, Any],
    fields: Collection[str],
    titles: Mapping[str, tuple[str | None, ...]],
    axes: Sequence[Sequence[str]],
    meanings: Mapping[str, tuple[str | None, str | None]],
    kinds: Mapping[str, Type],
) -> dict[str, Any]:
    key = _inputs_of(run, fields, kinds)
    return {
        "group": of,
        "place": place,
        "tuples": _tuples(key, titles.get(run.id), axes),
    } | _fields(result, fields, meanings)


def _inputs_of(
    run: Run, fields: Collection[str] | None, kinds: Mapping[str, Type]
) -> dict[str, Any]:
    """The run's key, the inputs of it `fields` names, or all of it, each
    file or folder told as one."""
    return {
        name: _input(value, kinds.get(name))
        for name, value in key_json(run.key).items()
        if fields is None or name in fields
    }


def _input(value: object, kind: Type | None) -> object:
    """An input's value as a reader is shown it: a file or folder of the
    task as `{file: <path>}` or `{folder: <path>}`, any other as it is."""
    if kind in (Type.FILE, Type.FOLDER) and isinstance(value, str):
        return {kind.value: value}
    return value


def _settings(
    show: ShowTimes,
    settings: Mapping[str, object],
    kinds: Mapping[str, Type],
    moments: Collection[str],
) -> dict[str, Any]:
    """The settings `show` lists under `inputs` at `moments`, by name."""
    return {
        name: _input(settings[name], kinds.get(name))
        for name in listed(show, "inputs", moments)
        if name in settings
    }


def _tuples(
    key: Mapping[str, Any],
    titles: tuple[str | None, ...] | None,
    axes: Sequence[Sequence[str]],
) -> list[dict[str, Any]]:
    """One `{title, key}` per axis: that tuple's title, none where it has
    none, and the columns of the axis that `key` still holds. A column of
    no axis, such as a run the task has since dropped, sits in one more
    tuple with no title. With no axes, the whole key is that one tuple.
    """
    named = list(titles) if titles else []
    if not axes:
        title = named[0] if len(named) == 1 else None
        return [{"title": title, "key": dict(key)}]
    used: set[str] = set()
    found: list[dict[str, Any]] = []
    for index, columns in enumerate(axes):
        part = {name: key[name] for name in columns if name in key}
        used.update(part)
        found.append({"title": named[index] if index < len(named) else None, "key": part})
    rest = {name: value for name, value in key.items() if name not in used}
    if rest:
        found.append({"title": None, "key": rest})
    return found


def _fields(
    result: Mapping[str, Any],
    fields: Collection[str] | None,
    meanings: Mapping[str, tuple[str | None, str | None]],
) -> dict[str, Any]:
    """What a run's result shows of the `fields` named, or of every field:
    its verdict, `passed`, `label` and `message` where named and given, and
    each output named, a number among `reports` and a text among `texts`.
    """
    shown: dict[str, Any] = {
        name: result[name]
        for name in RUN_FIELDS
        if name in result and (fields is None or name in fields)
    }
    reports = result.get("reports")
    named = result.get("texts")
    shown["reports"] = _reports(
        {
            name: number
            for name, number in (reports.items() if isinstance(reports, Mapping) else ())
            if fields is None or name in fields
        },
        meanings,
    )
    shown["texts"] = {
        name: each
        for name, each in (named.items() if isinstance(named, Mapping) else ())
        if fields is None or name in fields
    }
    return shown


def _reports(
    raw: Mapping[str, Any], meanings: Mapping[str, tuple[str | None, str | None]]
) -> list[dict[str, Any]]:
    """Each number a run reported, in the order it was stored, named by the
    title its workflow gives it, or by its own name, with its unit."""
    found: list[dict[str, Any]] = []
    for name, number in raw.items():
        title, unit = meanings.get(str(name), (None, None))
        found.append(
            {
                "title": title if isinstance(title, str) and title != "" else str(name),
                "unit": unit if isinstance(unit, str) and unit != "" else None,
                "number": number,
            }
        )
    return found


def _once(once: Mapping[str, Any], listed: Collection[str] | None = None) -> dict[str, Any]:
    """The reports and texts of the steps done once, only those `listed`
    names when given."""
    reports = dict(once.get("reports") or {})
    named = dict(once.get("texts") or {})
    if listed is not None:
        reports = {name: each for name, each in reports.items() if name in listed}
        named = {name: each for name, each in named.items() if name in listed}
    return {"reports": reports, "texts": named}


__all__ = [
    "Shown",
    "TaskInputs",
    "TextAt",
    "ValueShown",
    "addresses",
    "bare",
    "later",
    "shown",
    "shown_file",
    "task_inputs",
    "texts",
    "times_of",
]
