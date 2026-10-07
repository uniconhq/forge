"""A publication as the port hands it back, and the note it carries. A
publication is a protected version of a task at the commit a valid save
wrote; it is what grading reads, and the latest one is what grades. Its note
is a short YAML document the package writes when it publishes and reads back
when it lists them: whether the publication changed how the task grades,
what changed, in words a person reads, which workflow each workflow name
the task used was, by the forge's own id for it, so a later save can tell
when the same name has come to mean another workflow, and what the task's
sealed steps hold back until its reveal (`forge.domain.showing.Sealed`):
the sealed steps that run once, whose stop is held back, and the values
reported from sealed steps.

    grading_changed: true
    changes:
    - plans/plan.json changed
    workflows:
      acme/sorting: "412"
    sealed_steps: [validate]
    sealed_values: [accuracy]
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from forge.domain.ids import PublicationId, VersionId
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


@dataclass(frozen=True, slots=True)
class Note:
    grading_changed: bool
    changes: tuple[str, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)
    sealed: Sealed = NOTHING_SEALED


def write_note(
    grading_changed: bool,
    changes: tuple[str, ...],
    workflows: Mapping[str, str] | None = None,
    sealed: Sealed = NOTHING_SEALED,
) -> str:
    """The note a publication is made with."""
    document: dict[str, object] = {"grading_changed": grading_changed, "changes": list(changes)}
    if workflows:
        document["workflows"] = dict(sorted(workflows.items()))
    if sealed.steps:
        document["sealed_steps"] = sorted(sealed.steps)
    if sealed.values:
        document["sealed_values"] = sorted(sealed.values)
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _names(value: object) -> frozenset[str]:
    return frozenset(str(name) for name in value) if isinstance(value, list) else frozenset()


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
    return Note(document.get("grading_changed") is True, listed, pinned, sealed)
