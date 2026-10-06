"""What a contestant is shown of one grading's result, and when
(TASK-FORMAT.md section 3.5), as far as it goes without points: each test
group by its `show`, the outcome over the groups shown, the once values of
steps that are not sealed, and what stopped the run unless a sealed step may
have.

- `always`: the group's outcome and its tests, each with its outcome and
  values, as soon as the grading ends.
- `verdict`: the group's outcome at once, its tests at the task's reveal.
- `after_close`: its name, and the time it is shown, until the reveal.

A group's outcome is the first outcome other than `accepted` among its tests
in test order, otherwise `accepted`. The outcome a contestant sees is the
run's `stopped` when a step that is not sealed stopped it; otherwise the
first group outcome other than `accepted` among the groups whose verdict is
shown, in `test_groups` order; otherwise `accepted`; and none when no group
is shown. Once the task has revealed, everything is shown. A run in
`system_error` shows nothing of itself: its row is told it is still being
graded, until staff regrade or end it.

A grading is shown with the latest publication's `test_groups`, the
organisers' current word; a test the result has that no group lists is
shown with nobody's group and so not at all before the reveal.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forge.domain.definitions import Group, Show

ACCEPTED = "accepted"


@dataclass(frozen=True, slots=True)
class Sealed:
    """What a publication's sealed steps hold back until the reveal: the
    run's `stopped`, when a step that runs once is sealed, and the values
    reported from sealed steps, by name.
    """

    stop: bool = False
    values: frozenset[str] = frozenset()


NOTHING_SEALED = Sealed()


@dataclass(frozen=True, slots=True)
class GroupShown:
    """One test group as a contestant sees it now: its name, its `show`, its
    outcome when its verdict is shown, its tests when they are, and when
    what is held back is shown.
    """

    group: str
    show: Show
    outcome: str | None
    tests: tuple[Mapping[str, Any], ...] | None
    shown_at: datetime | None


@dataclass(frozen=True, slots=True)
class Shown:
    """A result as a contestant sees it now."""

    stopped: str | None
    outcome: str | None
    groups: tuple[GroupShown, ...]
    values: Mapping[str, Any]


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
        tests = tuple(_row(row, hidden_values) for row in rows.get(name, ()))
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
    if sealed.stop and not revealed:
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
