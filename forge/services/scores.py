"""Scoring a task's gradings on read (`forge.domain.scoring`), the same way
for a contestant's own submissions and for the boards.

A grading is scored with the publication it ran under as `showing.under`
says: its own tests and sealed facts, and the latest `test_groups`,
`credit` and value meanings while no grading change was published since. A
past publication's `task.yaml` and plan are read once per process, since a
publication never changes.

Under relative credit a test's `B` is the best value any candidate reached
on it among the task's gradings of the same generation, the publications
that grade the same test content: over the contest's rows, each
submission's attempt a board reads (`usable`), a candidate by it, its run
not stopped, accepted on the test.

One rule says which of a submission's attempts counts, on a board, for
relative credit, to its row and against `submissions.max`: the latest that
finished with a result, until a newer attempt finishes with one, so a
regrade under way, a system error and a staff cancel never take a result
away (`counted`). Finished, that attempt is a candidate when its run was
not stopped, an attempt when a sealed step stopped it, and nothing
otherwise. A submission none of whose attempts finished with a result is
still grading while its latest attempt is queued, running or a system
error, and nothing once that attempt is cancelled or ended over the limit
(`forge.domain.grading.room`): it then counts on no board.
"""

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from pydantic import ValidationError
from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.boards import State
from forge.domain.definitions import (
    TASK_FILE,
    ContestDefinition,
    Relative,
    parse_task,
)
from forge.domain.errors import NotFound
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import PublicationId
from forge.domain.plans import PLAN_PATH, Plan
from forge.domain.release import Extension, due_of, late_days
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.scoring import ONE, Better, Scored, best_of, late_factor, rule_weight, score
from forge.domain.showing import Graded, under
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import gradings, published, timelines
from forge.services.published import PublishedTask

log = get_logger(__name__)

GRADED_KEPT = published.FORM_KEPT
"""How long this process keeps what a past publication scores its gradings
with: a publication never changes, so the time only bounds what is held."""


def factor(
    settings: ContestDefinition, current: PublishedTask, extension: Extension, at: datetime
) -> Fraction:
    """The late factor of a submission taken `at` by a row with `extension`."""
    entry = current.entry
    if entry is None:
        return ONE
    days = late_days(due_of(settings, entry, extension), at)
    return late_factor(days, settings.late_per_day_of(entry))


@dataclass(frozen=True, slots=True)
class Usable:
    """The attempt of a submission a score reads, with where it stands."""

    state: State
    row: Grading | None


def counted(rows: Sequence[Grading]) -> Grading:
    """The attempt of one submission that counts, the one its row reads: the
    latest that finished with a result, or, while none has, the latest.
    """
    return gradings.latest_good(rows) or max(rows, key=lambda row: row.attempt)


def usable(ctx: Context, rows: Sequence[Grading], lost: Collection[uuid.UUID]) -> Usable:
    """Which of one submission's attempts a board reads, and where the
    submission stands by it, before its run's stop is read: the latest
    attempt that finished with a result, a candidate; with none, still
    grading while the latest attempt is queued, running or a system error,
    and nothing once it is cancelled or ended over the limit.
    """
    row = counted(rows)
    if gradings.good(row):
        return Usable(State.CANDIDATE, row)
    if gradings.status_of(ctx, row, lost) in (GradingStatus.CANCELLED, GradingStatus.OVER_LIMIT):
        return Usable(State.VOID, None)
    return Usable(State.GRADING, None)


def stands(row: Grading, graded: Graded) -> State:
    """A finished attempt's place on a board by what stopped its run."""
    result = row.result or {}
    if result.get("stopped") is None:
        return State.CANDIDATE
    if result.get("stopped_by") in graded.sealed.steps:
        return State.ATTEMPT
    return State.VOID


@dataclass(frozen=True, slots=True)
class Before:
    """A moment a result is scored as it stood: `publication` was then the
    task's latest, only attempts queued by `at` counted, and none of the
    attempts of `regrade` when one is given: just before that regrade began.
    """

    publication: PublicationId
    at: datetime
    regrade: uuid.UUID | None = None


class Scorer:
    """Scores one task's gradings for one read, `B` worked out once for
    each generation, value and direction it is asked for (two publications
    of one generation may name different values in `relative`), by the
    attempt of each submission a board reads (`usable`). Given `before`, it
    scores as things stood then: its publication standing for the task's
    latest, and `B` read from the attempts that counted then.
    """

    def __init__(self, ctx: Context, current: PublishedTask, before: Before | None = None) -> None:
        self.ctx = ctx
        self.current = current
        self.before = before
        self._latest: Graded | None = None
        self._bests: dict[tuple[int, str, Better | None], Mapping[str, Fraction]] = {}

    async def latest(self) -> Graded:
        if self._latest is None:
            current = self.current
            form = await published.form(self.ctx, current)
            self._latest = Graded(
                form.tests,
                current.definition.test_groups,
                current.publication.sealed,
                current.definition.credit,
                current.publication.measures,
                current.generation(current.publication),
                current.publication.meanings,
            )
        return self._latest

    async def graded(self, publication: PublicationId) -> Graded | None:
        """What a grading made under `publication` is scored with, or none
        when that publication does not read. Scoring as things stood before a
        regrade, the publication then the latest stands for the latest.
        """
        stood = self.current.publication.id if self.before is None else self.before.publication
        latest = await self.latest() if stood == self.current.publication.id else None
        if latest is None:
            latest = await self._own(stood)
        if publication == stood and latest is not None:
            return latest
        own = await self._own(publication)
        if own is None:
            return None
        return under(own, latest) if latest is not None else own

    async def scored(self, row: Grading, graded: Graded, late: Fraction) -> Scored:
        """The finished grading `row` scored under `graded` with its late
        factor.
        """
        best = None
        if isinstance(graded.credit, Relative):
            best = await self._best(graded, graded.credit.relative)
        worth: object = self.current.worth
        entry = self.current.entry
        if worth is not None and entry is not None and entry.worth is None:
            # Worth its groups' total by default: the groups it is scored
            # with, an earlier generation's included, so each group's
            # numbers stay its points.
            worth = sum((rule_weight(group) for group in graded.groups.values()), Fraction(0))
        return score(
            row.result or {},
            graded.tests,
            graded.groups,
            graded.credit,
            graded.measures,
            worth=worth,
            factor=late,
            best=best,
        )

    async def _best(self, graded: Graded, name: str) -> Mapping[str, Fraction]:
        measure = graded.measures.get(name)
        better = measure.better if measure is not None else None
        key = (graded.generation, name, better)
        if key in self._bests:
            return self._bests[key]
        generations = {each.id: self.current.generation(each) for each in self.current.publications}
        results = [
            row.result
            for row in await self._candidates()
            if row.result is not None
            and row.result.get("stopped") is None
            and generations.get(PublicationId(row.publication_id)) == graded.generation
        ]
        found = best_of(results, name, better)
        self._bests[key] = found
        return found

    async def _candidates(self) -> list[Grading]:
        """The attempt a board reads of every submission of the contest's
        rows to the task, for each that is a candidate by it; scoring as
        things stood at a moment, of the attempts that counted then.
        """
        before = self.before
        ctx = self.ctx
        contest = contest_id_of(task_scope(self.current.id))
        workspaces = sorted(
            ctx.forge.workspaces.workspace_of(contest, owner)
            for owner in await timelines.rows(ctx, contest)
        )
        if not workspaces:
            return []
        grouped: dict[tuple[str, int], list[Grading]] = {}
        for row in await ctx.db.scalars(
            select(Grading).where(
                Grading.task_id == self.current.id, Grading.workspace_id.in_(workspaces)
            )
        ):
            if before is not None and (
                (before.regrade is not None and row.regrade_id == before.regrade)
                or row.queued_at > before.at
            ):
                continue
            grouped.setdefault((row.workspace_id, row.submission_number), []).append(row)
        found: list[Grading] = []
        for attempts in grouped.values():
            # A submission is a candidate by its latest attempt that finished
            # with a result, whatever its later ones read as.
            good = gradings.latest_good(attempts)
            if good is not None:
                found.append(good)
        return found

    async def _own(self, publication: PublicationId) -> Graded | None:
        current = self.current
        found = next((each for each in current.publications if each.id == publication), None)
        if found is None:
            log.warning("scores.publication_gone", task=current.id, publication=publication)
            return None
        generation = current.generation(found)
        ctx = self.ctx

        async def read() -> Graded | None:
            await ctx.let_go()
            try:
                task_file = await ctx.forge.content.read_file(
                    PLATFORM, current.id, TASK_FILE, at=found.version
                )
                plan_file = await ctx.forge.content.read_file(
                    PLATFORM, current.id, PLAN_PATH, at=found.version
                )
                definition = parse_task(task_file.content)
                plan = Plan.from_bytes(plan_file.content)
            except NotFound, InvalidDefinition, ValidationError:
                log.warning(
                    "scores.publication_unreadable", task=current.id, publication=publication
                )
                return None
            return Graded(
                plan.tests,
                definition.test_groups,
                found.sealed,
                definition.credit,
                found.measures,
                generation,
                found.meanings,
            )

        key = f"scores.graded.{current.id}.{publication}"
        return await ctx.memo.remembered(key, GRADED_KEPT, read)
