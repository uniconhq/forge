"""What each reader sees of a run task's result, and when (SIMPLER-CORE.md
9.3, 9.4, 15 and 17): the one function every path that serves one reads
(`shown`), so there is one place to get it wrong.

An entrant sees, at each moment, what `show` lists under a time that has
come: `now` at once, `after_close` from the task's reveal, `on_release`
while its results are released. Anything not listed is never shown.

**Until the reveal, a release, or a late run shown, an entrant reads the
early row alone:** its runs, the values worked out from them, what its
steps done once gave and `compiled`, as if no late row were made. Nothing
they read then moves when the late row is made, runs or ends, so they
never learn its timing (the order rule). From then on they read both
rows. A late run is shown before the reveal only when a save moved its
group to `now` (phase 4.10).

- **A value** listed is shown with its number, missing as none. One that
  waits for staff is told so ("being checked") only as its runs are: when
  every run it waits on is early, or shown; otherwise it is left out until
  they are. Each carries whether it reads, directly or through others, a
  column the entrant could set (#504). A value reading an entered mark is
  worked out by the caller as if no mark were entered until a board cell
  shown to the entrant counted the submission, or, when none counted it,
  until the reveal (`forge.domain.boards.entries_open`, SIMPLER-CORE.md
  14), so it reads then as on a submission with no mark.
- **A group** listed shows its runs that a row holds: each with the
  group's name and its place in the group from 1 ("Test 3"), never its id,
  which counts every run of the task; its key, a file or folder it names
  left out unless `show` shows it then; its verdict, `passed` and reports;
  and its texts, `label` and `message`. A run in two groups listed is
  shown in each. **A run no group listed holds is never shown,** early or
  not: a run can be early only because a value shown then reads it.
- **What the steps done once reported,** the compile log among them, of
  the rows the entrant reads: each report `show` lists, by its name, once
  any run is shown (stage 5 section 6 item 30); one not listed never, nor
  one named like a group or a value, since the entry names that.
- **`compiled`** is shown from the time `show` lists it under, by its
  name as a value is, and once any run is shown, since the shown runs'
  verdicts already tell it (stage 5 section 6 item 124).
- `staff` outputs never.

A text is a reference to a stored output, which may be another entrant's
of the same org (stage 4 open 114), and whose id says when it was made.
So an entrant is never sent one (`bare`): a text is read by where it is
shown, its run's group and place, or the steps done once, and its name
(`TextAt`), which the server turns into the output (`addresses`).

A task none of whose values or groups is shown during the contest, `now`
and, while released, `on_release`, has nothing to tell before the reveal
(`later`): its entrant's submission reads "Submitted, results after the
contest" (stage 4 open 128).

Organisers read everything at once from both rows: every value with its
outcome, the time `show` lists it under and what it read, every run a row
holds under `all` with its id and its texts' outputs, a run the live
publication dropped after them marked `dropped`, everything done once,
and the facts.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, NamedTuple

from forge.domain.blocks.catalogue import ALL, SHOWN_FACTS
from forge.domain.blocks.evaluate import SYSTEM_ERROR
from forge.domain.content import named_by
from forge.domain.definitions import Scoring
from forge.domain.run_results import RunGrading, Worked
from forge.domain.run_task import Run, file_time, holds, key_json, show_names, shown_times

SHOWN_RUN = ("verdict", "passed", "reports", "texts", "label", "message")
"""What a reader sees of a run's result beside its place and key."""
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
    column the entrant could set. Organisers also read the time `show`
    lists it under, none when never, and what it read: the runs by id, the
    values and the facts.
    """

    name: str
    number: Fraction | None
    waiting: bool
    self_reported: bool
    show: str | None = None
    runs: tuple[str, ...] | None = None
    values: tuple[str, ...] | None = None
    facts: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Shown:
    """A run task's grading as one reader sees it now: its values, its runs
    by group, what the steps done once reported (`reports` and `texts`),
    `compiled`, none while not shown, and whether nothing of the task is
    shown before its reveal. Organisers also read the facts.
    """

    values: tuple[ValueShown, ...]
    runs: tuple[Mapping[str, Any], ...]
    once: Mapping[str, Any]
    compiled: int | None
    later: bool
    facts: Mapping[str, Fraction] = field(default_factory=dict)


def times_of(*, revealed: bool, released: bool) -> tuple[str, ...]:
    """The times of `show` that have come for an entrant."""
    return (
        "now",
        *(("after_close",) if revealed else ()),
        *(("on_release",) if released else ()),
    )


def later(scoring: Scoring, *, released: bool) -> bool:
    """Whether no value or group is shown during the contest."""
    names = show_names(scoring)
    during = [*scoring.show.now, *(scoring.show.on_release if released else ())]
    return not any(entry in names for entry in during)


def shown(
    scoring: Scoring,
    runs: Sequence[Run],
    files: Collection[str],
    whole: Worked,
    early: Worked,
    self_reported: Collection[str],
    *,
    revealed: bool,
    released: bool = False,
    everything: bool = False,
) -> Shown:
    """What a reader sees of a grading now: `scoring`, `runs` and `files` the
    task's live publication's, `whole` the grading worked out from both its
    rows and `early` from its early row alone, and `self_reported` the
    values reading a column the entrant could set; for an entrant, whether
    the task has revealed and whether its results are released; with
    `everything`, as organisers read it. Texts are as stored: `bare` takes
    them out for an entrant.
    """
    if everything:
        return _everything(scoring, runs, whole, self_reported)
    times = times_of(revealed=revealed, released=released)
    listed = {entry for time in times for entry in scoring.show.lists()[time]}
    held = _held(scoring, runs, whole.grading, files, times)
    view = whole
    if not (revealed or released or {name for name, _ in held} - whole.grading.early):
        view = early
        held = _held(scoring, runs, early.grading, files, times)
    grading = view.grading
    seen = {name for name, _ in held}
    values: list[ValueShown] = []
    for name in scoring.values:
        outcome = view.outcomes[name]
        if name not in listed:
            continue
        if outcome.waiting:
            waits = {run for run in outcome.runs if grading.read[run].verdict == SYSTEM_ERROR}
            if not waits <= grading.early | seen:
                continue
        values.append(ValueShown(name, outcome.number, outcome.waiting, name in self_reported))
    return Shown(
        tuple(values),
        tuple(entry for _, entry in held),
        _once(
            [grading.once_early, grading.once_late] if seen else [], listed - show_names(scoring)
        ),
        grading.compiled if seen or listed & set(SHOWN_FACTS) else None,
        later(scoring, released=released) and not revealed,
    )


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
) -> Shown:
    grading, outcomes = whole.grading, whole.outcomes
    when = shown_times(scoring.show)
    values = tuple(
        ValueShown(
            name,
            outcome.number,
            outcome.waiting,
            name in self_reported,
            when.get(name),
            tuple(run.id for run in runs if run.id in outcome.runs),
            tuple(sorted(outcome.values)),
            tuple(sorted(outcome.facts)),
        )
        for name, outcome in ((name, outcomes[name]) for name in scoring.values)
    )
    ran = [
        (run.id, key_json(run.key), grading.results[run.id], False)
        for run in runs
        if run.id in grading.results
    ]
    ran += [
        (name, dict(entry.get("key") or {}), entry, True)
        for name, entry in sorted(grading.dropped.items())
    ]
    shown_runs = tuple(
        {"group": ALL, "place": place, "id": name, "key": key}
        | _seen(entry)
        | ({"dropped": True} if dropped else {})
        for place, (name, key, entry, dropped) in enumerate(ran, start=1)
    )
    return Shown(
        values,
        shown_runs,
        _once([grading.once_early, grading.once_late]),
        grading.compiled,
        later=False,
        facts=dict(whole.facts),
    )


def _held(
    scoring: Scoring,
    runs: Sequence[Run],
    grading: RunGrading,
    files: Collection[str],
    times: Sequence[str],
) -> list[tuple[str, dict[str, Any]]]:
    """Each run shown by a group listed under one of `times`, in the order
    listed, with its id beside what is shown of it.
    """
    listed = [entry for time in times for entry in scoring.show.lists()[time]]
    found: list[tuple[str, dict[str, Any]]] = []
    for group in dict.fromkeys(listed):
        if group == ALL:
            members = list(runs)
        elif group in scoring.groups:
            members = [run for run in runs if holds(scoring.groups[group], run.key)]
        else:
            continue
        for place, run in enumerate(members, start=1):
            result = grading.results.get(run.id)
            if result is None:
                continue
            key = {
                name: value
                for name, value in key_json(run.key).items()
                if _file_shown(scoring, value, files, times)
            }
            found.append((run.id, {"group": group, "place": place, "key": key} | _seen(result)))
    return found


def _seen(result: Mapping[str, Any]) -> dict[str, Any]:
    return {name: result[name] for name in SHOWN_RUN if name in result}


def _once(
    parts: Sequence[Mapping[str, Any]], listed: Collection[str] | None = None
) -> dict[str, Any]:
    """The reports and texts of the steps done once in `parts`, only those
    `listed` names when given."""
    reports: dict[str, Any] = {}
    named: dict[str, Any] = {}
    for part in parts:
        reports |= part.get("reports") or {}
        named |= part.get("texts") or {}
    if listed is not None:
        reports = {name: each for name, each in reports.items() if name in listed}
        named = {name: each for name, each in named.items() if name in listed}
    return {"reports": reports, "texts": named}


def _file_shown(
    scoring: Scoring, value: object, files: Collection[str], times: Sequence[str]
) -> bool:
    """Whether a run's key may name `value`: it names no file or folder of
    the task, or `show` shows each file it names at one of `times`.
    """
    if not isinstance(value, str):
        return True
    named = named_by(files, value)
    return not named or all(file_time(scoring, path) in times for path in named)


__all__ = [
    "SHOWN_RUN",
    "Shown",
    "TextAt",
    "ValueShown",
    "addresses",
    "bare",
    "later",
    "shown",
    "texts",
    "times_of",
]
