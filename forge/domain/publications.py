"""A publication as the port hands it back, and the note it carries. A
publication is a protected version of a task at the commit a valid save
wrote; it is what grading reads, and the latest one is what grades. Its note
is a short YAML document the package writes when it publishes and reads back
when it lists them: whether the publication changed how the task grades, and
what changed, in words a person reads.

    grading_changed: true
    changes:
    - plans/default.json changed
"""

from dataclasses import dataclass
from datetime import datetime

import yaml

from forge.domain.ids import PublicationId, VersionId


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


@dataclass(frozen=True, slots=True)
class Note:
    grading_changed: bool
    changes: tuple[str, ...]


def write_note(grading_changed: bool, changes: tuple[str, ...]) -> str:
    """The note a publication is made with."""
    document = {"grading_changed": grading_changed, "changes": list(changes)}
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


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
    return Note(document.get("grading_changed") is True, listed)
