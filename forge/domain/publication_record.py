"""A publication as the port hands it back, and the note it carries. A
publication is a protected version of a task at the commit a valid save
wrote; it is what grading reads, and the latest one is what grades. Its note
is a short YAML document the package writes when it publishes and reads back
when it lists them: whether the publication changed how the task grades,
what changed, in words a person reads, which workflow each workflow name
the task used was, by the forge's own id for it, so a later save can tell
when the same name has come to mean another workflow; and, for a
publication whose plan holds steps done at the save, `checked_first`, so it
is held back until a save row checks it, whether or not that row was ever
written (`forge.services.entrant_view.live`).

    grading_changed: true
    changes:
    - The way the task grades changed.
    workflows:
      acme/sorting: "412"
    checked_first: true
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import yaml

from forge.domain.ids import PublicationId, VersionId

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
    check: Check | None = None
    checked_first: bool = False
    failed_checks: tuple[FailedCheck, ...] = ()


@dataclass(frozen=True, slots=True)
class Note:
    grading_changed: bool
    changes: tuple[str, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)
    checked_first: bool = False


def write_note(
    grading_changed: bool,
    changes: tuple[str, ...],
    workflows: Mapping[str, str] | None = None,
    *,
    checked_first: bool = False,
) -> str:
    """The note a publication is made with."""
    document: dict[str, object] = {"grading_changed": grading_changed, "changes": list(changes)}
    if workflows:
        document["workflows"] = dict(sorted(workflows.items()))
    if checked_first:
        document["checked_first"] = True
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
    workflows = document.get("workflows")
    pinned = (
        {str(name): str(key) for name, key in workflows.items()}
        if isinstance(workflows, dict)
        else {}
    )
    return Note(
        document.get("grading_changed") is True,
        listed,
        pinned,
        document.get("checked_first") is True,
    )
