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

Where a submission stands on a board is its latest attempt's: finished, it
is a candidate when its run was not stopped, an attempt when a sealed step
stopped it, and nothing otherwise; staff cancelled, nothing; in
`system_error`, still grading to its row; still running or queued, the
latest attempt that finished before it while there is one, so a regrade in
progress moves no board until it ends, and otherwise still grading. A
fallback, staff's on the latest attempt or the contest's
`on_system_error: last_result`, has a submission in `system_error` or staff
cancelled stand as its latest earlier attempt that finished with a result
(`fallen_back`), while there is one.
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
    OnSystemError,
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
from forge.domain.scoring import ONE, Scored, best_of, late_factor, score
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


def usable(
    ctx: Context,
    rows: Sequence[Grading],
    lost: Collection[uuid.UUID],
    contest: OnSystemError,
) -> Usable:
    """Which of one submission's attempts a board reads, and where the
    submission stands by it, before its run's stop is read, `contest` being
    the contest's `on_system_error`.
    """
    latest = max(rows, key=lambda row: row.attempt)
    status = gradings.status_of(ctx, latest, lost)
    if status is GradingStatus.DONE and latest.result is not None:
        return Usable(State.CANDIDATE, latest)
    good = gradings.last_good(rows, latest)
    if gradings.broken(latest, status):
        if good is not None and gradings.fallback_of(latest.falls_back, contest) is not None:
            return Usable(State.CANDIDATE, good)
        if gradings.staff_cancelled(latest):
            return Usable(State.VOID, None)
        return Usable(State.GRADING, None)
    if good is not None:
        return Usable(State.CANDIDATE, good)
    if status is GradingStatus.CANCELLED:
        return Usable(State.VOID, None)
    return Usable(State.GRADING, None)


def fallen_back(
    ctx: Context,
    rows: Sequence[Grading],
    lost: Collection[uuid.UUID],
    contest: OnSystemError,
) -> Grading | None:
    """The earlier attempt a fallback counts in place of a submission's
    broken latest attempt, or none while no fallback is in force for it.
    """
    latest = max(rows, key=lambda row: row.attempt)
    if not gradings.broken(latest, gradings.status_of(ctx, latest, lost)):
        return None
    if gradings.fallback_of(latest.falls_back, contest) is None:
        return None
    return gradings.last_good(rows, latest)


def stands(row: Grading, graded: Graded) -> State:
    """A finished attempt's place on a board by what stopped its run."""
    result = row.result or {}
    if result.get("stopped") is None:
        return State.CANDIDATE
    if result.get("stopped_by") in graded.sealed.steps:
        return State.ATTEMPT
    return State.VOID


class Scorer:
    """Scores one task's gradings for one read, `B` worked out once for
    each generation it is asked for, by the attempts the contest's
    `on_system_error` has a board read.
    """

    def __init__(self, ctx: Context, current: PublishedTask, contest: OnSystemError) -> None:
        self.ctx = ctx
        self.current = current
        self.contest = contest
        self._latest: Graded | None = None
        self._bests: dict[int, Mapping[str, Fraction]] = {}

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
            )
        return self._latest

    async def graded(self, publication: PublicationId) -> Graded | None:
        """What a grading made under `publication` is scored with, or none
        when that publication does not read.
        """
        latest = await self.latest()
        if publication == self.current.publication.id:
            return latest
        own = await self._own(publication)
        return under(own, latest) if own is not None else None

    async def scored(self, row: Grading, graded: Graded, late: Fraction) -> Scored:
        """The finished grading `row` scored under `graded` with its late
        factor.
        """
        best = None
        if isinstance(graded.credit, Relative):
            best = await self._best(graded, graded.credit.relative)
        return score(
            row.result or {},
            graded.tests,
            graded.groups,
            graded.credit,
            graded.measures,
            worth=self.current.worth,
            factor=late,
            best=best,
        )

    async def _best(self, graded: Graded, name: str) -> Mapping[str, Fraction]:
        if graded.generation in self._bests:
            return self._bests[graded.generation]
        measure = graded.measures.get(name)
        better = measure.better if measure is not None else None
        generations = {each.id: self.current.generation(each) for each in self.current.publications}
        results = [
            row.result
            for row in await self._candidates()
            if row.result is not None
            and row.result.get("stopped") is None
            and generations.get(PublicationId(row.publication_id)) == graded.generation
        ]
        found = best_of(results, name, better)
        self._bests[graded.generation] = found
        return found

    async def _candidates(self) -> list[Grading]:
        """The attempt a board reads of every submission of the contest's
        rows to the task, for each that is a candidate by it.
        """
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
            grouped.setdefault((row.workspace_id, row.submission_number), []).append(row)
        lost = await gradings.lost(
            ctx, [max(attempts, key=lambda row: row.attempt) for attempts in grouped.values()]
        )
        found: list[Grading] = []
        for attempts in grouped.values():
            read = usable(ctx, attempts, lost, self.contest)
            if read.state is State.CANDIDATE and read.row is not None:
                found.append(read.row)
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
            )

        key = f"scores.graded.{current.id}.{publication}"
        return await ctx.memo.remembered(key, GRADED_KEPT, read)
