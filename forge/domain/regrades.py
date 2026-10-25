"""A regrade of many and what it changed (TASK-FORMAT.md section 3.5).

A rejudge of a task and the regrade every submission to it gets when a save
publishes a change to how it grades are each one regrade, by what started
it (`RegradeCause`). Each submission's latest attempt of it is compared with
the result that counted for that submission when the regrade began, the
latest earlier attempt that finished with one, both as organisers see
results: every group, nothing held back, the late factor applied
(`Side`). The earlier one is scored as it counted just before the regrade
began: with the scoring of the publication then the task's latest
(`counted_under`), and under relative credit against the best among the
results that counted then. What it changed is one `ChangeKind`:

- `up` and `down`, its points went up or down; on a task that gives no
  points, a larger or smaller share of its tests accepted, or with the same
  share, its values with a declared direction: down when any got worse, up
  when none did and one got better, so a regrade that makes anything worse
  is never read as an improvement; a value one side has and the other has
  not is the worst where it is missing, as a board reads it;
- `stopped`, it now stops before its tests where the result it replaced did
  not: in the classic workflow, it now fails to compile;
- `unchanged`, all of that the same;
- `new`, it had no result to compare with, or one of the two could not be
  scored, its publication no longer reading;
- `running`, still being graded; `failed`, no result yet, its latest
  attempt a system error or cancelled; and `over_limit`, ended without
  being graded since its entrant had no room left under `submissions.max`.

A regrade's summary counts its submissions by change, `graded` being those
that finished with a result.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction

from forge.domain.blocks.numbers import Better
from forge.domain.ids import PublicationId
from forge.domain.publications import Publication


class RegradeCause(StrEnum):
    """What started a regrade of many: staff's `rejudge` of the task, or the
    `save` that published a change to how it grades.
    """

    REJUDGE = "rejudge"
    SAVE = "save"


class ChangeKind(StrEnum):
    """What a regrade did to one submission, as the module says."""

    UP = "up"
    DOWN = "down"
    STOPPED = "stopped"
    UNCHANGED = "unchanged"
    NEW = "new"
    RUNNING = "running"
    FAILED = "failed"
    OVER_LIMIT = "over_limit"


GRADED = (ChangeKind.UP, ChangeKind.DOWN, ChangeKind.STOPPED, ChangeKind.UNCHANGED, ChangeKind.NEW)
"""The changes of a submission whose regrade finished with a result."""


@dataclass(frozen=True, slots=True)
class Side:
    """One result a regrade compares, as organisers see it: its attempt,
    its points, none on a task that gives no points, how many of its tests
    were accepted of how many it has, the outcome that stopped its run
    before its tests, none when nothing did, each value with a fold and a
    declared direction, folded over every test, and that direction.
    """

    attempt: int
    points: Fraction | None
    accepted: int
    tests: int
    stopped: str | None = None
    values: Mapping[str, Fraction] = field(default_factory=dict)
    better: Mapping[str, Better] = field(default_factory=dict)

    @property
    def share(self) -> Fraction | None:
        """The part of its tests accepted, none with no tests."""
        return Fraction(self.accepted, self.tests) if self.tests else None


@dataclass(frozen=True, slots=True)
class Compared:
    """What a regrade did to one submission: the change, the result it
    replaced, none when there was none or it could not be scored, and its
    own, none while it has none or it could not be scored.
    """

    change: ChangeKind
    earlier: Side | None
    now: Side | None


def counted_under(
    cause: RegradeCause, against: PublicationId, publications: Sequence[Publication]
) -> PublicationId:
    """The publication that was the task's latest just before a regrade
    against `against` began, of the task's `publications`: `against` itself
    for a rejudge, which publishes nothing; for a save's regrade, the latest
    publication before the one the save made, or `against` when there is
    none. A result that counted then is scored as that publication scored it
    (`showing.under`).
    """
    if cause is RegradeCause.REJUDGE:
        return against
    own = next((each for each in publications if each.id == against), None)
    before = [each for each in publications if own is not None and each.number < own.number]
    return max(before, key=lambda each: each.number).id if before else against


def change_of(earlier: Side | None, now: Side | None) -> ChangeKind:
    """What a regrade's result `now` changed against `earlier`, the result it
    replaced; `new` when there is no earlier one, or either could not be
    scored (none). Only a finished result is asked about here.
    """
    if earlier is None or now is None:
        return ChangeKind.NEW
    if now.stopped is not None and earlier.stopped is None:
        return ChangeKind.STOPPED
    if now.points is not None and earlier.points is not None:
        return _by(now.points - earlier.points)
    if now.share is not None and earlier.share is not None and now.share != earlier.share:
        return _by(now.share - earlier.share)
    worse = better = False
    for name in now.values.keys() | earlier.values.keys():
        before, value = earlier.values.get(name), now.values.get(name)
        direction = now.better.get(name) or earlier.better.get(name)
        if direction is None or before == value:
            continue
        if before is None or value is None:
            # A missing value is the worst, as a board reads it.
            improved = before is None
        else:
            improved = value > before if direction is Better.HIGHER else value < before
        better |= improved
        worse |= not improved
    if worse:
        return ChangeKind.DOWN
    return ChangeKind.UP if better else ChangeKind.UNCHANGED


def _by(difference: Fraction) -> ChangeKind:
    if difference > 0:
        return ChangeKind.UP
    if difference < 0:
        return ChangeKind.DOWN
    return ChangeKind.UNCHANGED


def counted(changes: Iterable[ChangeKind]) -> dict[ChangeKind, int]:
    """How many submissions each change counts, every change named, 0 where
    none.
    """
    found = Counter(changes)
    return {change: found[change] for change in ChangeKind}
