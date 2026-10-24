"""A publication as the port hands it back, and the note it carries. A
publication is a protected version of a task at the commit a valid save
wrote; it is what grading reads, and the latest one is what grades. Its note
is a short YAML document the package writes when it publishes and reads back
when it lists them: whether the publication changed how the task grades,
what changed, in words a person reads, which workflow each workflow name
the task used was, by the forge's own id for it, so a later save can tell
when the same name has come to mean another workflow, what the task's
sealed steps hold back until its reveal (`forge.domain.showing.Sealed`):
the sealed steps that run once, whose stop is held back, and the values
reported from sealed steps; and what each per-test number the workflow
reports means for the task (`forge.domain.meanings.Measure`), its fold, its
direction with the task's values in and its bounds, as the workflow at its
pinned version said when the task published, so a page says how each value
folds without reading the workflow again; and every value the
workflow reports, in its order, with the title and unit a page names it by
(`forge.domain.meanings.ValueMeaning`), so no page reads the workflow for
those either; and, for a run task's publication whose plan holds steps done
at the save, `checked_first`, so it is held back until a save row checks it,
whether or not that row was ever written (`forge.services.entrant_view.live`).

    grading_changed: true
    changes:
    - The way the task grades changed.
    workflows:
      acme/sorting: "412"
    sealed_steps: [validate]
    sealed_values: [accuracy]
    values:
      time_ms: {fold: max, better: lower, at_least: "0"}
    reported:
      time_ms: {title: Run time, unit: ms}
      log: {title: Compile log}
    checked_first: true

A publication made before values were noted has none: its numbers are shown
on their tests with no fold until the task publishes again. One
made before the reported values' words were noted lists its per-test
numbers by name alone, and a page names each by its key.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import yaml

from forge.domain.ids import PublicationId, VersionId
from forge.domain.meanings import (
    Measure,
    ValueMeaning,
    meanings_noted,
    measure_noted,
    with_measures,
)
from forge.domain.showing import NOTHING_SEALED, Sealed

Check = Literal["checking", "refused", "replaced", "unchecked"]
"""Where a run task's publication stands while its save row has not let it
take over: still `checking`; `refused`, its checking ended without every
run accepted and every check given what it expects; `unchecked`, its
checking never began, since its save failed after publishing; or
`replaced`, its checking stopped by a later save."""


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One run of a check as its save graded it: its id, its key, its
    verdict and its number reports."""

    id: str
    key: Mapping[str, object]
    verdict: str
    reports: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FailedCheck:
    """A check that refused its save, for the organiser (SIMPLER-CORE.md
    9.6): its place in `checks`, from 1, why it failed, a sentence each,
    its runs, the outcome of each step done once, the number each value's
    program reported, and the fault of its steps, if any."""

    index: int
    failing: tuple[str, ...]
    runs: tuple[CheckRun, ...]
    outcomes: Mapping[str, str]
    values: Mapping[str, object]
    error: str | None


def failed_checks(result: Mapping[str, object] | None) -> tuple[FailedCheck, ...]:
    """Each check of a save row's `result` that did not get what it
    expects."""
    entries = (result or {}).get("checks")
    if not isinstance(entries, list):
        return ()
    found: list[FailedCheck] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or entry.get("passed") is True:
            continue
        once = entry.get("once") or {}
        found.append(
            FailedCheck(
                index=index,
                failing=tuple(str(each) for each in entry.get("failing") or ()),
                runs=tuple(
                    CheckRun(
                        id=str(run["id"]),
                        key=dict(run.get("key") or {}),
                        verdict=str(run["verdict"]),
                        reports=dict(run.get("reports") or {}),
                    )
                    for run in entry.get("runs") or ()
                ),
                outcomes={
                    str(step): str(outcome)
                    for step, outcome in (once.get("outcomes") or {}).items()
                },
                values=dict(entry.get("values") or {}),
                error=entry.get("error") if isinstance(entry.get("error"), str) else None,
            )
        )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Publication:
    """One publication: its id, its number among the task's publications,
    the version it froze, what its note says and when it was made; and for
    an organiser, `check`, where a run task's one stands while it has not
    taken over, none once it has or when it needs no checking;
    `checked_first` when it is not the task's until a save row passes; and
    when its check is `refused`, each of the task's checks that failed it.
    """

    id: PublicationId
    number: int
    version: VersionId
    grading_changed: bool
    changes: tuple[str, ...]
    at: datetime
    workflows: Mapping[str, str] = field(default_factory=dict)
    sealed: Sealed = NOTHING_SEALED
    measures: Mapping[str, Measure] = field(default_factory=dict)
    titles: Mapping[str, str] = field(default_factory=dict)
    meanings: tuple[ValueMeaning, ...] = ()
    check: Check | None = None
    checked_first: bool = False
    failed_checks: tuple[FailedCheck, ...] = ()


@dataclass(frozen=True, slots=True)
class Note:
    grading_changed: bool
    changes: tuple[str, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)
    sealed: Sealed = NOTHING_SEALED
    measures: Mapping[str, Measure] = field(default_factory=dict)
    titles: Mapping[str, str] = field(default_factory=dict)
    meanings: tuple[ValueMeaning, ...] = ()
    checked_first: bool = False


def write_note(
    grading_changed: bool,
    changes: tuple[str, ...],
    workflows: Mapping[str, str] | None = None,
    sealed: Sealed = NOTHING_SEALED,
    measures: Mapping[str, Measure] | None = None,
    titles: Mapping[str, str] | None = None,
    meanings: Sequence[ValueMeaning] = (),
    *,
    checked_first: bool = False,
) -> str:
    """The note a publication is made with."""
    document: dict[str, object] = {"grading_changed": grading_changed, "changes": list(changes)}
    if workflows:
        document["workflows"] = dict(sorted(workflows.items()))
    if sealed.steps:
        document["sealed_steps"] = sorted(sealed.steps)
    if sealed.values:
        document["sealed_values"] = sorted(sealed.values)
    if measures:
        document["values"] = {name: measures[name].noted() for name in sorted(measures)}
    if titles:
        document["titles"] = dict(sorted(titles.items()))
    if meanings:
        document["reported"] = {meaning.name: meaning.noted() for meaning in meanings}
    if checked_first:
        document["checked_first"] = True
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _names(value: object) -> frozenset[str]:
    return frozenset(str(name) for name in value) if isinstance(value, list) else frozenset()


def _measures(value: object) -> dict[str, Measure]:
    if not isinstance(value, dict):
        return {}
    found: dict[str, Measure] = {}
    for name, noted in value.items():
        measure = measure_noted(noted)
        if measure is not None:
            found[str(name)] = measure
    return found


def read_note(text: str | None) -> Note:
    """What a publication's note says. A note that is missing or does not
    read says nothing changed, since only the platform writes one.
    """
    try:
        document = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return Note(False, ())
    if not isinstance(document, dict):
        return Note(False, ())
    changes = document.get("changes")
    listed = tuple(str(change) for change in changes) if isinstance(changes, list) else ()
    workflows = document.get("workflows")
    pinned = (
        {str(name): str(key) for name, key in workflows.items()}
        if isinstance(workflows, dict)
        else {}
    )
    sealed = Sealed(
        steps=_names(document.get("sealed_steps")), values=_names(document.get("sealed_values"))
    )
    measures = _measures(document.get("values"))
    return Note(
        document.get("grading_changed") is True,
        listed,
        pinned,
        sealed,
        measures,
        _titles(document.get("titles")),
        with_measures(meanings_noted(document.get("reported")), measures),
        document.get("checked_first") is True,
    )


def _titles(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(name): str(title) for name, title in value.items() if isinstance(title, str)}
