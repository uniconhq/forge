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
reports means for the task (`forge.domain.scoring.Measure`), its fold, its
direction with the task's values in and its bounds, as the workflow at its
pinned version said when the task published, so a grading is scored and a
board is checked without reading the workflow again; and every value the
workflow reports, in its order, with the title and unit a page names it by
(`forge.domain.meanings.ValueMeaning`), so no page reads the workflow for
those either.

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

A publication made before values were noted has none: its numbers are shown
on their tests and fold into nothing until the task publishes again. One
made before the reported values' words were noted lists its per-test
numbers by name alone, and a page names each by its key.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from forge.domain.ids import PublicationId, VersionId
from forge.domain.meanings import ValueMeaning, meanings_noted, with_measures
from forge.domain.scoring import Measure, measure_noted
from forge.domain.showing import NOTHING_SEALED, Sealed


@dataclass(frozen=True, slots=True)
class Publication:
    """One publication: its id, its number among the task's publications,
    the version it froze, what its note says and when it was made.
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


@dataclass(frozen=True, slots=True)
class Note:
    grading_changed: bool
    changes: tuple[str, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)
    sealed: Sealed = NOTHING_SEALED
    measures: Mapping[str, Measure] = field(default_factory=dict)
    titles: Mapping[str, str] = field(default_factory=dict)
    meanings: tuple[ValueMeaning, ...] = ()


def write_note(
    grading_changed: bool,
    changes: tuple[str, ...],
    workflows: Mapping[str, str] | None = None,
    sealed: Sealed = NOTHING_SEALED,
    measures: Mapping[str, Measure] | None = None,
    titles: Mapping[str, str] | None = None,
    meanings: Sequence[ValueMeaning] = (),
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
    )


def _titles(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(name): str(title) for name, title in value.items() if isinstance(title, str)}
