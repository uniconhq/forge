"""Which attempt of a submission counts, the one rule for its row, the
boards and `submissions.max` (`counted`); what a task graded test by test
is shown with (`GradedOf`); and a run task's values, worked out on every
read from both its rows (`run_scored`).

A grading graded test by test is shown with the publication it ran under as
`showing.under` says: its own tests and sealed facts, and the latest
`test_groups` and value meanings while no grading change was published
since. A past publication's `task.yaml` and plan are read once per process,
since a publication never changes. Nothing of it is scored: since stage 5
only a run task has values (stage 5 section 6, item 123).

One rule says which of a submission's attempts counts, on a board, to its
row and against `submissions.max`: the latest that finished with a result,
until a newer attempt finishes with one, so a regrade under way, a system
error and a staff cancel never take a result away (`counted`). A submission
none of whose attempts finished with a result is still grading while its
latest attempt is queued, running or a system error, and nothing once that
attempt is cancelled or ended over the limit (`forge.domain.grading.room`):
it then counts on no board.
"""

import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from pydantic import ValidationError
from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.gradings import LATE
from forge.domain.definitions import TASK_FILE, ContestDefinition, parse_task
from forge.domain.errors import NotFound
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import PublicationId
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.plans import Plan
from forge.domain.release import Extension, due_of
from forge.domain.run_results import (
    RunGrading,
    Worked,
    facts_of,
    run_grading,
    worked_out,
)
from forge.domain.showing import Graded, under
from forge.domain.step_data import unreported
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entries, gradings, published
from forge.services.published import PublishedTask, RunsHeld

log = get_logger(__name__)

FINISHED_WITH_RESULT = (GradingStatus.DONE, GradingStatus.SYSTEM_ERROR)
"""Where a run task's row stands when its result is read: done, or failed
with the runs it gave, its faulted ones waiting for staff."""
GRADED_KEPT = published.FORM_KEPT
"""How long this process keeps what a past publication shows its gradings
with: a publication never changes, so the time only bounds what is held."""


class State(StrEnum):
    """Where one submission stands by the attempt of it that counts: still
    `grading` to its row, pending (a delayed one, in `system_error`, among
    them); `void`, no attempt, cancelled or ended over the limit; or a
    `candidate`, an attempt that finished with a result.
    """

    GRADING = "grading"
    VOID = "void"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class Usable:
    """The attempt of a submission a board reads, with where it stands."""

    state: State
    row: Grading | None


def counted(rows: Sequence[Grading]) -> Grading:
    """The attempt of one submission that counts, the one its row reads: the
    latest that finished with a result, or, while none has, the latest.
    """
    return gradings.latest_good(rows) or max(rows, key=lambda row: row.attempt)


def usable(ctx: Context, rows: Sequence[Grading], lost: Collection[uuid.UUID]) -> Usable:
    """Which of one submission's attempts a board reads, and where the
    submission stands by it: the latest attempt that finished with a
    result, a candidate; with none, still grading while the latest attempt
    is queued, running or a system error, and nothing once it is cancelled
    or ended over the limit.
    """
    row = counted(rows)
    if gradings.good(row):
        return Usable(State.CANDIDATE, row)
    if gradings.status_of(ctx, row, lost) in (GradingStatus.CANCELLED, GradingStatus.OVER_LIMIT):
        return Usable(State.VOID, None)
    return Usable(State.GRADING, None)


class GradedOf:
    """What one task's gradings graded test by test are shown with, for one
    read (`showing.Graded`). Given `stood`, the publication that was the
    task's latest at a moment, it reads as things stood then, that
    publication standing for the task's latest.
    """

    def __init__(
        self, ctx: Context, current: PublishedTask, stood: PublicationId | None = None
    ) -> None:
        self.ctx = ctx
        self.current = current
        self.stood = stood
        self._latest: Graded | None = None

    async def latest(self) -> Graded:
        if self._latest is None:
            current = self.current
            form = await published.form(self.ctx, current)
            self._latest = Graded(
                form.tests,
                current.definition.test_groups,
                current.publication.sealed,
                current.generation(current.publication),
                current.publication.meanings,
            )
        return self._latest

    async def graded(self, publication: PublicationId) -> Graded | None:
        """What a grading made under `publication` is shown with, or none
        when that publication does not read. Read as things stood at a
        moment, the publication then the latest stands for the latest.
        """
        stood = self.current.publication.id if self.stood is None else self.stood
        latest = await self.latest() if stood == self.current.publication.id else None
        if latest is None:
            latest = await self._own(stood)
        if publication == stood and latest is not None:
            return latest
        own = await self._own(publication)
        if own is None:
            return None
        return under(own, latest) if latest is not None else own

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
                plan.tests, definition.test_groups, found.sealed, generation, found.meanings
            )

        key = f"scores.graded.{current.id}.{publication}"
        return await ctx.memo.remembered(key, GRADED_KEPT, read)


@dataclass(frozen=True, slots=True)
class RunScored:
    """A run task's grading worked out on read: the live publication's runs
    and plan, the grading from both its rows, every value, as organisers
    read it, and from its early row alone, which `run_showing.shown`
    serves an entrant from before the reveal, worked out only when called
    (stage 5 section 6, item 129), the values reading a column the
    entrant could set, and the runs the early row's plan ran late, which a
    value shown from the early row alone may not read (item 134).
    """

    held: RunsHeld
    whole: Worked
    early: Callable[[], Worked]
    self_reported: frozenset[str]
    late: frozenset[str] = frozenset()


async def late_of(ctx: Context, early: Grading) -> Grading | None:
    """The attempt of the late row that follows the early row `early` that
    counts, as `counted` takes an early row's: the latest that finished
    with a result, so a staff retry still to run takes nothing away, or,
    while none has, the latest; none while there is none."""
    attempts = (
        await ctx.db.scalars(
            select(Grading).where(
                Grading.submission_id == early.submission_id,
                Grading.pipeline == LATE,
                Grading.early_id == early.id,
            )
        )
    ).all()
    return counted(attempts) if attempts else None


async def lates_of(ctx: Context, earlies: Iterable[Grading]) -> dict[uuid.UUID, Grading]:
    """`late_of` for each of `earlies` at once, by the early row's id; one
    with no late row is left out."""
    ids = sorted({early.id for early in earlies})
    if not ids:
        return {}
    attempts: dict[uuid.UUID, list[Grading]] = {}
    for row in await ctx.db.scalars(
        select(Grading).where(Grading.pipeline == LATE, Grading.early_id.in_(ids))
    ):
        assert row.early_id is not None
        attempts.setdefault(row.early_id, []).append(row)
    return {early: counted(rows) for early, rows in attempts.items()}


async def run_scored(
    ctx: Context,
    current: PublishedTask,
    settings: ContestDefinition,
    extension: Extension,
    early: Grading,
    late: Grading | None,
    marks: Mapping[str, Fraction] | None = None,
) -> RunScored:
    """The values of a run task's submission (SIMPLER-CORE.md 9.2): the live
    publication's `scoring` over the runs of its early row, done or failed
    with a result, and of its late row's attempt that counts, `late`, once
    that is done or failed with a result, matched by run id, with the facts
    of the row's due, its extension included, and the contest's start; and
    the same from the early row alone, which is all an entrant reads until
    the reveal (`run_showing`). A run neither row holds, or a value's
    program neither row gives, waits only when its
    row's own plan put it in a pipeline still to give it: the early row's
    while that row failed, the late row's while that row is queued, running
    or failed, or not made since the early row failed. Any other, such as
    one a later save added, never ran (`run_results`). The marks entered
    for the submission's `entered` values are read with them (`entries`),
    unless the caller read them already, as `marks`. Every value is worked
    out, one reading a mark among them: an entrant's page gives no marks
    while what the boards' cells counted holds them back (`submissions`).
    A program waits while its row is to give it, and for staff once its
    row ended with a fault and no number for it; `compiled` waits while the
    row whose plan holds its compile is to give it. Worked out on every
    read, never stored (stage 5 section 6, item 1).
    """
    scoring = current.definition.scoring
    assert scoring is not None, "a run task's definition always has scoring"
    held = await published.runs_held(ctx, current)
    early_failed = early.status != GradingStatus.DONE
    late_result = None
    late_waits = early_failed
    planned, programs, compiles = await _planned(ctx, current, early, held)
    late_planned, late_programs, late_compiles = planned, programs, compiles
    if late is not None:
        status = gradings.status_of(ctx, late)
        if late.result is not None and status in FINISHED_WITH_RESULT:
            late_result = late.result
        late_waits = status not in (
            GradingStatus.DONE,
            GradingStatus.CANCELLED,
            GradingStatus.OVER_LIMIT,
        )
        late_planned, late_programs, late_compiles = await _planned(ctx, current, late, held)
    waits_early = {run for run, first in planned.items() if first} if early_failed else set()
    waits_late = {run for run, first in late_planned.items() if not first} if late_waits else set()
    # A program waits while its row is still to give it, and, once its row
    # ended with a fault and no number for it, for staff.
    early_named = {name for name, first in programs.items() if first}
    late_named = {name for name, first in late_programs.items() if not first}
    programs_early = early_named if early_failed else set(unreported(early.result, early_named))
    programs_late = late_named if late_waits else set(unreported(late_result, late_named))
    # So does a compile `compiled` reads, while its row is still to give it.
    early_compiling = {step for step, first in compiles.items() if first}
    late_compiling = {step for step, first in late_compiles.items() if not first}
    compiles_early = early_compiling if early_failed else set()
    compiles_late = late_compiling if late_waits else set()
    entry = settings.entry(current.name)
    due = due_of(settings, entry, extension) if entry is not None else None
    if marks is None:
        marks = await entries.entered_of(ctx, current.id, early.submission_id)

    def work(grading: RunGrading) -> Worked:
        facts = facts_of(due, settings.start, early.submitted_at, grading.compiled)
        outcomes = worked_out(scoring, held.runs, grading, facts, held.columns, marks)
        return Worked(grading, outcomes, facts)

    whole = work(
        run_grading(
            held.runs,
            early.result,
            late_result,
            waiting=waits_early | waits_late,
            compiling=held.compiling,
            programs_waiting=programs_early | programs_late,
            compiling_waiting=compiles_early | compiles_late,
        )
    )
    ran_late = frozenset(run for run, first in planned.items() if not first)
    if late_result is None and not waits_late and not programs_late and not compiles_late:
        # The early row alone is all there is, so it works out the same.
        return RunScored(held, whole, lambda: whole, held.self_reported, ran_late)
    kept: list[Worked] = []

    def alone() -> Worked:
        if not kept:
            kept.append(
                work(
                    run_grading(
                        held.runs,
                        early.result,
                        None,
                        waiting=waits_early,
                        compiling=held.compiling,
                        programs_waiting=programs_early,
                        compiling_waiting=compiles_early,
                    )
                )
            )
        return kept[0]

    return RunScored(held, whole, alone, held.self_reported, ran_late)


async def _planned(
    ctx: Context, current: PublishedTask, row: Grading, held: RunsHeld
) -> tuple[dict[str, bool], dict[str, bool], dict[str, bool]]:
    """Each run of the publication `row` grades against, by id, with whether
    its plan runs it early in the split `row` was made with, each value
    whose program it runs, and each step it runs that `compiled` reads,
    each with whether early; the live publication's runs when that one is
    gone, and no program or step."""
    found = next((each for each in current.publications if each.id == row.publication_id), None)
    if found is None:
        log.warning("scores.publication_gone", task=current.id, publication=row.publication_id)
        return {run.id: run.early for run in held.runs}, {}, {}
    plan = (await published.plan_at(ctx, current.id, found.version)).for_split(row.released_split)
    programs: dict[str, bool] = {}
    compiles: dict[str, bool] = {}
    for kind in ("early", "late"):
        for each in plan.pipeline(kind):
            value = plan.steps[each.step].value
            if value is not None and value.report is not None:
                programs[value.name] = kind == "early"
            if each.step in held.compiling:
                compiles[each.step] = kind == "early"
    return {run.id: run.early for run in plan.runs}, programs, compiles
