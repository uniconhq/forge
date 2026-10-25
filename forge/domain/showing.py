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
everything is shown. A run in `system_error` shows nothing of itself: a
submission with no result whose latest attempt is one is told it is
`delayed`, until its retry runs or staff end it by cancelling it, when its
row is told it is `cancelled`, with the sentence staff gave (`told`). A
submission that has a result is told that result, whatever its later
attempts read as.

A grading is shown with the publication it ran under (`under`, TASK-FORMAT.md
section 2, check 12): its sealed facts always, since they say what that
plan's steps read; and the `test_groups` and value meanings of the task's
latest publication while no grading change was published since, so a
change to `show` alone counts at once, and its own otherwise, until its
regrade ends, so a result's rows are always grouped with the tests they ran
on. Nothing is scored: a task graded test by test gives no points, credit
or folded value since stage 5, which scores run tasks alone (stage 5
section 6, item 123).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from forge.domain.definitions import Group, Show
from forge.domain.meanings import ValueMeaning

ACCEPTED = "accepted"


@dataclass(frozen=True, slots=True)
class Seen:
    """What a viewer below the organisers is shown of a task at a moment:
    before its reveal, verdicts of groups not `after_close` and tests of
    `always` groups; from it, everything. Organisers see everything live,
    which is `Seen(revealed=True)`. A run whose stop a sealed step made is
    `held` until the reveal: nothing of it is shown, so it reads as a run
    whose every group is still hidden.
    """

    revealed: bool
    held: bool = False

    def verdict(self, show: Show) -> bool:
        return not self.held and (self.revealed or show is not Show.AFTER_CLOSE)

    def tests(self, show: Show) -> bool:
        return not self.held and (self.revealed or show is Show.ALWAYS)


def group_of(test: str) -> str:
    return test.split("/", 1)[0]


def first_failed(outcomes: Iterable[str | None]) -> str | None:
    """The first of `outcomes` other than `accepted`, in their order;
    `accepted` when every one is; none when there is none. A group's
    outcome over its tests and a submission's over its shown groups are
    both this rule.
    """
    given = [outcome for outcome in outcomes if outcome is not None]
    if not given:
        return None
    return next((outcome for outcome in given if outcome != ACCEPTED), ACCEPTED)


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
    """A publication as a grading made under it is shown with: the tests
    its plan lists, its `test_groups`, what its sealed steps hold back, and
    its generation, the number of the latest publication up to it that
    changed how the task grades, which publications grading the same test
    content share; and every value its workflow reports, with the words a
    page names it by.
    """

    tests: tuple[str, ...]
    groups: Mapping[str, Group]
    sealed: Sealed
    generation: int = 0
    meanings: tuple[ValueMeaning, ...] = ()


def under(own: Graded, latest: Graded) -> Graded:
    """What a grading made under `own` is shown with while `latest` is the
    task's latest publication: `own`'s tests and sealed facts, and
    `latest`'s `test_groups` and meanings, the values' words among them,
    while no grading change came between them, `own`'s otherwise.
    """
    if own.generation != latest.generation or own.tests != latest.tests:
        return own
    return replace(own, groups=latest.groups, meanings=latest.meanings)


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
    that did not run has no outcome, no tests and nothing held back; while a
    sealed step's stop is held to the reveal, every group reads as hidden,
    the ones it kept from running too.
    """

    group: str
    show: Show
    outcome: str | None
    tests: tuple[Mapping[str, Any], ...] | None
    shown_at: datetime | None
    ran: bool = True


@dataclass(frozen=True, slots=True)
class Shown:
    """A result as a contestant sees it now: what stopped it, its outcome,
    its groups and its once values.
    """

    stopped: str | None
    outcome: str | None
    groups: tuple[GroupShown, ...]
    values: Mapping[str, Any]


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
    seen = seen_of(result, sealed, revealed=revealed)
    found: list[GroupShown] = []
    for name, group in groups.items():
        show = group.shown
        if name not in rows and not seen.held:
            found.append(GroupShown(name, show, None, (), None, ran=False))
            continue
        tests = tuple(_row(row, hidden_values) for row in rows.get(name, ()))
        tests_shown = seen.tests(show)
        found.append(
            GroupShown(
                group=name,
                show=show,
                outcome=_outcome(tests) if seen.verdict(show) else None,
                tests=tests if tests_shown else None,
                shown_at=None if tests_shown else reveal_at,
            )
        )
    stopped = None if seen.held else result.get("stopped")
    values = {
        name: value
        for name, value in (result.get("values") or {}).items()
        if name not in hidden_values
    }
    return Shown(stopped, _viewed(stopped, found), tuple(found), values)


def filled_in(row: Shown, revealed: Shown) -> Shown:
    """A result as organisers read it: `revealed`, the result shown as after
    the task's reveal, with nothing held back, and each group's `shown_at`
    kept from `row`, what its contestant is shown now, as the note of when
    they see it.
    """
    when = {group.group: group.shown_at for group in row.groups}
    return replace(
        revealed,
        groups=tuple(replace(group, shown_at=when.get(group.group)) for group in revealed.groups),
    )


def seen_of(result: Mapping[str, Any], sealed: Sealed, *, revealed: bool) -> Seen:
    """What a contestant is shown of `result` now: held, nothing, while the
    step that stopped it is sealed and the task has not revealed.
    """
    return Seen(revealed, held=not revealed and result.get("stopped_by") in sealed.steps)


def _row(row: Mapping[str, Any], hidden: Collection[str]) -> Mapping[str, Any]:
    values = {
        name: value for name, value in (row.get("values") or {}).items() if name not in hidden
    }
    return {"test": row["test"], "outcome": row["outcome"], "values": values}


def _outcome(tests: Sequence[Mapping[str, Any]]) -> str:
    return first_failed(str(row["outcome"]) for row in tests) or ACCEPTED


def _viewed(stopped: str | None, groups: Sequence[GroupShown]) -> str | None:
    if stopped is not None:
        return stopped
    return first_failed(group.outcome for group in groups)
