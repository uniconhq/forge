"""What a contestant is shown of one grading's result, and when
(TASK-FORMAT.md section 3.5), as far as it goes without points: each test
group by its `show`, the outcome over the groups shown, the once values of
steps that are not sealed, and what stopped the run unless a sealed step
did, as the result's `stopped_by` names it.

- `always`: the group's outcome and its tests, each with its outcome and
  values, as soon as the grading ends.
- `verdict`: the group's outcome at once, its tests at the task's reveal.
- `after_close`: its name, and the time it is shown, until the reveal.

A group's outcome is the first outcome other than `accepted` among its tests
in test order, otherwise `accepted`. A group with no rows in the result did
not run on this grading: it has no outcome and adds nothing. The outcome a
contestant sees is the run's `stopped` when a step that is not sealed
stopped it; otherwise the first group outcome other than `accepted` among
the groups whose verdict is shown, in `test_groups` order; otherwise
`accepted`; and none when no group is shown. Once the task has revealed,
everything is shown. A run in `system_error` shows nothing of itself: its
row is told it is still being graded, until staff regrade it or end it by
cancelling it, when its row is told it is `cancelled`, with the sentence
staff gave (`told`).

A grading is shown with the publication it ran under (`under`, TASK-FORMAT.md
section 2, check 12): its sealed facts always, since they say what that
plan's steps read; and the `test_groups` of the task's latest publication
when the two plans list the same tests, so a change to rule weights or
`show` alone counts at once, and its own `test_groups` otherwise, so a
result's rows are always folded with the tests they ran on.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forge.domain.definitions import Group, Show
from forge.domain.grading import GradingStatus

ACCEPTED = "accepted"


@dataclass(frozen=True, slots=True)
class Sealed:
    """What a publication's sealed steps hold back until the reveal: the
    run's `stopped`, when the step that stopped it is one of `steps`, the
    sealed steps that run once, and the values reported from sealed steps,
    by name.
    """

    steps: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()


NOTHING_SEALED = Sealed()


@dataclass(frozen=True, slots=True)
class Graded:
    """A publication as a grading made under it is shown with: the tests its
    plan lists, its `test_groups`, and what its sealed steps hold back.
    """

    tests: tuple[str, ...]
    groups: Mapping[str, Group]
    sealed: Sealed


def under(own: Graded, latest: Graded) -> Graded:
    """What a grading made under `own` is shown with while `latest` is the
    task's latest publication: `own`'s sealed facts, and `latest`'s
    `test_groups` when the two plans list the same tests, `own`'s otherwise.
    """
    groups = latest.groups if own.tests == latest.tests else own.groups
    return Graded(own.tests, groups, own.sealed)


@dataclass(frozen=True, slots=True)
class GroupShown:
    """One test group as a contestant sees it now: its name, its `show`, its
    outcome when its verdict is shown, its tests when they are, when what is
    held back is shown, and whether it ran on this grading at all. A group
    that did not run has no outcome, no tests and nothing held back.
    """

    group: str
    show: Show
    outcome: str | None
    tests: tuple[Mapping[str, Any], ...] | None
    shown_at: datetime | None
    ran: bool = True


@dataclass(frozen=True, slots=True)
class Shown:
    """A result as a contestant sees it now."""

    stopped: str | None
    outcome: str | None
    groups: tuple[GroupShown, ...]
    values: Mapping[str, Any]


def told(status: GradingStatus) -> GradingStatus:
    """Where a grading stands as its own row is told: as it reads, but for a
    run in `system_error`, which is still being graded to its row until
    staff end it, a fault of the platform's or of setter code and never of
    the contestant's.
    """
    return GradingStatus.RUNNING if status is GradingStatus.SYSTEM_ERROR else status


def group_of(test: str) -> str:
    return test.split("/", 1)[0]


def shown(
    result: Mapping[str, Any],
    groups: Mapping[str, Group],
    sealed: Sealed,
    *,
    revealed: bool,
    reveal_at: datetime | None,
) -> Shown:
    """What of `result` a contestant sees under `groups`, the task's
    `test_groups`, with its sealed facts, before or after the task's
    reveal at `reveal_at`.
    """
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in result.get("tests") or ():
        rows.setdefault(group_of(str(row["test"])), []).append(row)
    hidden_values = sealed.values if not revealed else frozenset()
    found: list[GroupShown] = []
    for name, group in groups.items():
        show = group.shown
        if name not in rows:
            found.append(GroupShown(name, show, None, (), None, ran=False))
            continue
        tests = tuple(_row(row, hidden_values) for row in rows[name])
        verdict_shown = revealed or show is not Show.AFTER_CLOSE
        tests_shown = revealed or show is Show.ALWAYS
        found.append(
            GroupShown(
                group=name,
                show=show,
                outcome=_outcome(tests) if verdict_shown else None,
                tests=tests if tests_shown else None,
                shown_at=None if tests_shown else reveal_at,
            )
        )
    stopped = result.get("stopped")
    if result.get("stopped_by") in sealed.steps and not revealed:
        stopped = None
    values = {
        name: value
        for name, value in (result.get("values") or {}).items()
        if name not in hidden_values
    }
    return Shown(stopped, _viewed(stopped, found), tuple(found), values)


def _row(row: Mapping[str, Any], hidden: Collection[str]) -> Mapping[str, Any]:
    values = {
        name: value for name, value in (row.get("values") or {}).items() if name not in hidden
    }
    return {"test": row["test"], "outcome": row["outcome"], "values": values}


def _outcome(tests: Sequence[Mapping[str, Any]]) -> str:
    return next((str(row["outcome"]) for row in tests if row["outcome"] != ACCEPTED), ACCEPTED)


def _viewed(stopped: str | None, groups: Sequence[GroupShown]) -> str | None:
    if stopped is not None:
        return stopped
    outcomes = [group.outcome for group in groups if group.outcome is not None]
    if not outcomes:
        return None
    return next((outcome for outcome in outcomes if outcome != ACCEPTED), ACCEPTED)
