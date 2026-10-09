"""Every number a grading's result gives, worked out on read (TASK-FORMAT.md
section 3), so changing how a task scores regrades nothing.

- A test's **credit**, 0 to 1 (3.2): 0 unless accepted; then 1, or the
  value `credit` names, or, relative, the value against `B`, the best any
  contestant row's candidate reached on that test.
- A group's **points** (3.3): `E_g = each * M_g + worst * N_g + pass * P_g`
  over its tests' credits and test weights, `R_g` its rule weights summed,
  `W` the sum over every group; the task gives points when `W` is above 0,
  and a group's points are `worth * late_factor * E_g / W`, its most
  `worth * R_g / W`. Nothing is divided by the rule weight of a subset.
- The **late factor**: `max(0, 1 - late_per_day * d)` for `d` started days
  after the row's due, 1 on time.
- A **value**'s fold over a set of tests (3.4), a test without the value
  counting as its worst declared bound, and the fold having no value when
  that bound is not declared or the set is empty.
- What is **shown** at a moment (3.5): a group's verdict, its outcome and
  points, unless it is `after_close` and the task has not revealed; its
  tests when it is `always` or the task has revealed. Everything is shown
  once revealed, which is also how organisers see every number live.

Every number is the exact rational of the decimal it was written as: a
result's numbers arrive as `Decimal` (`forge.domain.exact_json`) and a
YAML number as the shortest decimal that reads back as it (`repr`), so
`0.8` is 4/5 whichever file it came from. Only `decimal_of` rounds, and
only a rational that is no finite decimal, such as 1/3.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Any

from forge.domain.definitions import Group, Relative, Show
from forge.domain.workflow_definition import Fold

ACCEPTED = "accepted"
SIGNIFICANT = 30
"""The significant digits a rational that is no finite decimal is given in,
far past any two numbers a board tells apart."""

ZERO = Fraction(0)
ONE = Fraction(1)


def exact(value: object) -> Fraction | None:
    """The exact rational of a number as written, or none for anything that
    is not a finite number (text, `true`, a missing value).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value) if value.is_finite() else None
    if isinstance(value, float):
        return Fraction(repr(value)) if math.isfinite(value) else None
    if isinstance(value, Fraction):
        return value
    return None


def decimal_of(number: Fraction) -> Decimal:
    """`number` as a decimal: exactly when it is one, and to `SIGNIFICANT`
    digits when it is not.
    """
    rest = number.denominator
    twos = fives = 0
    while rest % 2 == 0:
        rest //= 2
        twos += 1
    while rest % 5 == 0:
        rest //= 5
        fives += 1
    if rest == 1:
        scale = max(twos, fives)
        digits = number.numerator * 10**scale // number.denominator
        return Decimal(f"{digits}e-{scale}") if scale else Decimal(digits)
    with localcontext() as context:
        context.prec = SIGNIFICANT
        return Decimal(number.numerator) / Decimal(number.denominator)


def written(number: Fraction) -> str:
    """`number` as the plain decimal digits it is served and noted as."""
    return format(decimal_of(number), "f")


class Better(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"


@dataclass(frozen=True, slots=True)
class Measure:
    """What a per-test number means, as its workflow declares it with the
    task's values in: how it folds over tests, which way is better, and its
    bounds.
    """

    fold: Fold | None = None
    better: Better | None = None
    at_least: Fraction | None = None
    at_most: Fraction | None = None

    @property
    def worst(self) -> Fraction | None:
        """The bound a test without the value counts as: `at_least` when
        higher is better, `at_most` when lower is, none otherwise.
        """
        if self.better is Better.HIGHER:
            return self.at_least
        if self.better is Better.LOWER:
            return self.at_most
        return None

    def noted(self) -> dict[str, str]:
        """The measure as a publication's note keeps it."""
        found: dict[str, str] = {}
        if self.fold is not None:
            found["fold"] = self.fold.value
        if self.better is not None:
            found["better"] = self.better.value
        if self.at_least is not None:
            found["at_least"] = written(self.at_least)
        if self.at_most is not None:
            found["at_most"] = written(self.at_most)
        return found


def measure_noted(value: object) -> Measure | None:
    """A measure as a publication's note keeps it, or none for one that does
    not read.
    """
    if not isinstance(value, dict):
        return None
    try:
        fold = Fold(value["fold"]) if "fold" in value else None
        better = Better(value["better"]) if "better" in value else None
        at_least = Fraction(str(value["at_least"])) if "at_least" in value else None
        at_most = Fraction(str(value["at_most"])) if "at_most" in value else None
    except ValueError:
        return None
    return Measure(fold, better, at_least, at_most)


def late_factor(days: int, late_per_day: object) -> Fraction:
    """What points are multiplied by `days` started days after the row's
    due: 1 on time, and never below 0.
    """
    per_day = exact(late_per_day)
    if days <= 0 or per_day is None:
        return ONE
    return max(ZERO, ONE - per_day * days)


def rule_weight(group: Group) -> Fraction:
    """`R_g`, what the group can earn: its rule weights summed."""
    return sum(
        (exact(value) or ZERO for value in (group.each, group.worst, group.pass_)),
        ZERO,
    )


def credit_of(
    outcome: str | None,
    values: Mapping[str, Any],
    credit: str | Relative | None,
    measures: Mapping[str, Measure],
    best: Fraction | None,
) -> Fraction:
    """What one test earned (3.2). `best` is `B` for relative credit, none
    when no contestant row has a value on the test.
    """
    if outcome != ACCEPTED:
        return ZERO
    if credit is None:
        return ONE
    if isinstance(credit, Relative):
        value = exact(values.get(credit.relative))
        if value is None:
            return ZERO
        measure = measures.get(credit.relative)
        better = measure.better if measure is not None else None
        if best is None or better is None:
            return ONE
        if (value >= best) if better is Better.HIGHER else (value <= best):
            return ONE
        found = value / best if better is Better.HIGHER else best / value
        return min(ONE, max(ZERO, found))
    value = exact(values.get(credit))
    if value is None:
        return ZERO
    return min(ONE, max(ZERO, value))


def fold(measure: Measure, values: Sequence[Fraction | None]) -> Fraction | None:
    """The value over a set of tests by its fold, a test without it counting
    as its worst declared bound; none without a fold, over no tests, or when
    a test lacks it and no worst bound is declared.
    """
    if measure.fold is None or not values:
        return None
    filled: list[Fraction] = []
    for value in values:
        if value is None:
            if measure.worst is None:
                return None
            value = measure.worst
        filled.append(value)
    match measure.fold:
        case Fold.SUM:
            return sum(filled, ZERO)
        case Fold.MEAN:
            return sum(filled, ZERO) / len(filled)
        case Fold.MAX:
            return max(filled)


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


EVERYTHING = Seen(revealed=True)
BEFORE_REVEAL = Seen(revealed=False)


@dataclass(frozen=True, slots=True)
class TestScore:
    """One test of a grading: its id, its outcome, none when the result has
    no row for it, its credit, the values it reported as they were written,
    and `B` beside them under relative credit.
    """

    test: str
    outcome: str | None
    credit: Fraction
    values: Mapping[str, Any]
    best: Fraction | None = None


@dataclass(frozen=True, slots=True)
class GroupScore:
    """One group of a grading: its `show`, whether it ran, its outcome, what
    it earned (`E_g`) and can earn (`R_g`), its points and most points, none
    on a task that gives no points, and its tests.
    """

    group: str
    show: Show
    ran: bool
    outcome: str | None
    earned: Fraction
    weight: Fraction
    points: Fraction | None
    max: Fraction | None
    tests: tuple[TestScore, ...]


@dataclass(frozen=True, slots=True)
class Scored:
    """A grading scored: each group in `test_groups` order, the late factor
    its points include, and what each per-test number means.
    """

    groups: tuple[GroupScore, ...]
    factor: Fraction
    measures: Mapping[str, Measure]
    gives_points: bool

    def points(self, scope: Callable[[Show], bool], seen: Seen) -> Fraction | None:
        """Its points on the groups in `scope` whose verdict is shown; none
        on a task that gives no points.
        """
        if not self.gives_points:
            return None
        return sum(
            (
                group.points or ZERO
                for group in self.groups
                if scope(group.show) and seen.verdict(group.show)
            ),
            ZERO,
        )

    def pending(self, seen: Seen) -> Fraction | None:
        """The most its groups whose verdict is not shown yet can add, with
        the late factor; none on a task that gives no points.
        """
        if not self.gives_points:
            return None
        return sum(
            (
                (group.max or ZERO) * self.factor
                for group in self.groups
                if not seen.verdict(group.show)
            ),
            ZERO,
        )

    def value(self, name: str, scope: Callable[[Show], bool], seen: Seen) -> Fraction | None:
        """The value folded over the tests of the groups in `scope` whose
        tests are shown; none without a fold or a value.
        """
        measure = self.measures.get(name)
        if measure is None:
            return None
        return fold(
            measure,
            [
                exact(test.values.get(name))
                for group in self.groups
                if scope(group.show) and seen.tests(group.show)
                for test in group.tests
            ],
        )

    def values(self, seen: Seen) -> dict[str, Fraction]:
        """Every value with a fold, over every shown test, that has one."""
        found: dict[str, Fraction] = {}
        for name in self.measures:
            value = self.value(name, everywhere, seen)
            if value is not None:
                found[name] = value
        return found

    def outcome(self, seen: Seen) -> str | None:
        """The first outcome other than `accepted` among the groups whose
        verdict is shown, in `test_groups` order; `accepted` when there is
        none; none when no group that ran is shown.
        """
        outcomes = [
            group.outcome
            for group in self.groups
            if group.outcome is not None and seen.verdict(group.show)
        ]
        if not outcomes:
            return None
        return next((outcome for outcome in outcomes if outcome != ACCEPTED), ACCEPTED)


def everywhere(show: Show) -> bool:
    """The scope of every group."""
    return True


@dataclass(frozen=True, slots=True)
class Points:
    """A submission's points as a viewer sees them: those shown, those still
    pending and when they are shown.
    """

    shown: Fraction
    pending: Fraction
    pending_until: datetime | None


def points_seen(scored: Scored, seen: Seen, reveal_at: datetime | None) -> Points | None:
    """The submission's points shown and pending at a moment, none on a task
    that gives no points.
    """
    shown = scored.points(everywhere, seen)
    pending = scored.pending(seen)
    if shown is None or pending is None:
        return None
    return Points(shown, pending, reveal_at if pending else None)


def group_of(test: str) -> str:
    return test.split("/", 1)[0]


def name_in_group(test: str) -> str:
    return test.split("/", 1)[1] if "/" in test else test


def score(
    result: Mapping[str, Any],
    tests: Sequence[str],
    groups: Mapping[str, Group],
    credit: str | Relative | None,
    measures: Mapping[str, Measure],
    *,
    worth: object,
    factor: Fraction = ONE,
    best: Mapping[str, Fraction] | None = None,
) -> Scored:
    """`result` scored under `groups` and `credit`, `tests` being its plan's
    tests in order, `worth` what the contest says the task is worth, none
    when it gives no points, and `best` each test's `B` under relative
    credit.
    """
    rows = {str(row["test"]): row for row in result.get("tests") or ()}
    total = sum((rule_weight(group) for group in groups.values()), ZERO)
    worth_of = exact(worth) or ZERO
    gives = total > 0 and exact(worth) is not None
    of_group: dict[str, list[str]] = {}
    for test in tests:
        of_group.setdefault(group_of(test), []).append(test)
    relative = isinstance(credit, Relative)
    found: list[GroupScore] = []
    for name, group in groups.items():
        ids = of_group.get(name, [])
        scores = tuple(
            _test(test, rows.get(test), credit, measures, (best or {}).get(test), relative)
            for test in ids
        )
        ran = any(test in rows for test in ids)
        earned = _earned(group, scores)
        weight = rule_weight(group)
        found.append(
            GroupScore(
                group=name,
                show=group.shown,
                ran=ran,
                outcome=_outcome(scores) if ran else None,
                earned=earned,
                weight=weight,
                points=worth_of * factor * earned / total if gives else None,
                max=worth_of * weight / total if gives else None,
                tests=scores,
            )
        )
    return Scored(tuple(found), factor, measures, gives)


def _test(
    test: str,
    row: Mapping[str, Any] | None,
    credit: str | Relative | None,
    measures: Mapping[str, Measure],
    best: Fraction | None,
    relative: bool,
) -> TestScore:
    if row is None:
        return TestScore(test, None, ZERO, {})
    outcome = str(row["outcome"])
    values = row.get("values") or {}
    return TestScore(
        test,
        outcome,
        credit_of(outcome, values, credit, measures, best),
        values,
        best if relative else None,
    )


def _earned(group: Group, tests: Sequence[TestScore]) -> Fraction:
    """`E_g` over the group's tests."""
    if not tests:
        return ZERO
    weights = group.test_weights or {}
    weighted = [(exact(weights.get(name_in_group(test.test), 1)) or ONE, test) for test in tests]
    total = sum((weight for weight, _ in weighted), ZERO)
    mean = sum((weight * test.credit for weight, test in weighted), ZERO) / total
    worst = min(test.credit for test in tests)
    passing = sum((weight for weight, test in weighted if test.credit == ONE), ZERO) / total
    pass_at = exact(group.pass_at) if group.pass_at is not None else ONE
    passed = ONE if pass_at is not None and passing >= pass_at else ZERO
    return (
        (exact(group.each) or ZERO) * mean
        + (exact(group.worst) or ZERO) * worst
        + (exact(group.pass_) or ZERO) * passed
    )


def _outcome(tests: Sequence[TestScore]) -> str:
    return next(
        (test.outcome for test in tests if test.outcome is not None and test.outcome != ACCEPTED),
        ACCEPTED,
    )


def best_of(
    results: Sequence[Mapping[str, Any]], name: str, better: Better | None
) -> dict[str, Fraction]:
    """`B` for each test: the best value of `name` over `results`, every
    candidate of every contestant row graded on the same test content,
    among the tests each accepted and reported it on.
    """
    if better is None:
        return {}
    found: dict[str, Fraction] = {}
    for result in results:
        for row in result.get("tests") or ():
            if row.get("outcome") != ACCEPTED:
                continue
            value = exact((row.get("values") or {}).get(name))
            if value is None:
                continue
            test = str(row["test"])
            held = found.get(test)
            if (
                held is None
                or (better is Better.HIGHER and value > held)
                or (better is Better.LOWER and value < held)
            ):
                found[test] = value
    return found
