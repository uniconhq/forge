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
plan's steps read; and the `test_groups`, `credit` and value meanings of the
task's latest publication while no grading change was published since, so a
change to rule weights, `show` or `credit` alone counts at once, and its own
otherwise, until its regrade ends, so a result's rows are always folded with
the tests they ran on and a credit always names a value its plan reported.
With a grading scored (`forge.domain.scoring`), each group shown carries
its points and most points and each test row its credit.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from fractions import Fraction
from typing import Any

from forge.domain.definitions import Group, Relative, Show
from forge.domain.grading import GradingStatus
from forge.domain.scoring import Measure, Scored, Seen, TestScore

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
    """A publication as a grading made under it is shown and scored with:
    the tests its plan lists, its `test_groups`, what its sealed steps hold
    back, its `credit`, what each per-test number its workflow reports
    means, and its generation, the number of the latest publication up to
    it that changed how the task grades, which publications grading the
    same test content share.
    """

    tests: tuple[str, ...]
    groups: Mapping[str, Group]
    sealed: Sealed
    credit: str | Relative | None = None
    measures: Mapping[str, Measure] = field(default_factory=dict)
    generation: int = 0


def under(own: Graded, latest: Graded) -> Graded:
    """What a grading made under `own` is shown with while `latest` is the
    task's latest publication: `own`'s tests and sealed facts, and
    `latest`'s `test_groups`, `credit` and meanings while no grading change
    came between them, `own`'s otherwise.
    """
    if own.generation != latest.generation or own.tests != latest.tests:
        return own
    return replace(own, groups=latest.groups, credit=latest.credit, measures=latest.measures)


def generation_of(changed: Sequence[tuple[int, bool]], number: int) -> int:
    """The generation of publication `number`, from every publication's
    number and whether it changed how the task grades: the latest number up
    to it that did, the first publication counting as one.
    """
    found = 0
    for each, grading_changed in sorted(changed):
        if each > number:
            break
        if grading_changed or not found:
            found = each
    return found


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
    points: Fraction | None = None
    max: Fraction | None = None


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
    scored: Scored | None = None,
) -> Shown:
    """What of `result` a contestant sees under `groups`, the task's
    `test_groups`, with its sealed facts, before or after the task's
    reveal at `reveal_at`; with `scored`, the grading scored under those
    groups, each group's points and most points, its points only once its
    verdict is shown, and each test's credit.
    """
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in result.get("tests") or ():
        rows.setdefault(group_of(str(row["test"])), []).append(row)
    hidden_values = sealed.values if not revealed else frozenset()
    scores = {group.group: group for group in scored.groups} if scored is not None else {}
    seen = Seen(revealed)
    found: list[GroupShown] = []
    for name, group in groups.items():
        show = group.shown
        score = scores.get(name)
        most = score.max if score is not None else None
        if name not in rows:
            found.append(
                GroupShown(
                    name,
                    show,
                    None,
                    (),
                    None,
                    ran=False,
                    points=score.points if score is not None and seen.verdict(show) else None,
                    max=most,
                )
            )
            continue
        credits = {test.test: test for test in score.tests} if score is not None else {}
        tests = tuple(_row(row, hidden_values, credits.get(str(row["test"]))) for row in rows[name])
        verdict_shown = seen.verdict(show)
        tests_shown = seen.tests(show)
        found.append(
            GroupShown(
                group=name,
                show=show,
                outcome=_outcome(tests) if verdict_shown else None,
                tests=tests if tests_shown else None,
                shown_at=None if tests_shown else reveal_at,
                points=score.points if score is not None and verdict_shown else None,
                max=most,
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


def _row(
    row: Mapping[str, Any], hidden: Collection[str], score: TestScore | None = None
) -> Mapping[str, Any]:
    values = {
        name: value for name, value in (row.get("values") or {}).items() if name not in hidden
    }
    found: dict[str, Any] = {"test": row["test"], "outcome": row["outcome"], "values": values}
    if score is not None:
        found["credit"] = score.credit
        if score.best is not None:
            found["best"] = score.best
    return found


def _outcome(tests: Sequence[Mapping[str, Any]]) -> str:
    return next((str(row["outcome"]) for row in tests if row["outcome"] != ACCEPTED), ACCEPTED)


def _viewed(stopped: str | None, groups: Sequence[GroupShown]) -> str | None:
    if stopped is not None:
        return stopped
    outcomes = [group.outcome for group in groups if group.outcome is not None]
    if not outcomes:
        return None
    return next((outcome for outcome in outcomes if outcome != ACCEPTED), ACCEPTED)
