"""A run task's grading worked out (SIMPLER-CORE.md 9.2, 9.3 and 11.3):
its runs, read from both its rows, its facts, and its values.

A submission's grading is its early row and, when its plan has late runs,
the late row that follows it. Its runs are the task's live publication's,
each read by its id from whichever row ran it, the early row first. A run
neither row holds **waits** (`SYSTEM_ERROR`) only when it is one of
`waiting`: the runs a row's own plan put in a pipeline still to give them
(`scores.run_scored`), so a run a later save added, which no row's plan
holds, **never ran** (`NOT_RUN`) whatever the late row is doing: its
reports are missing and it never passed, until the regrade that save
started gives it. No value shows a number built on runs that have not
run, and none waits on a row whose plan never held the run.

A run that faulted keeps its `system_error` verdict, and waits too. Each
run reads the reports of the steps done once beside its own: an early
run the early row's, a late run both rows', since a late run goes through
the steps done once early. A run a row holds that the live publication
dropped is kept apart (`dropped`), for organisers.

The facts (`facts_of`): `seconds_late` and `seconds`
(`forge.domain.release`), and `compiled`, 0 when a step done once that is
not tainted returned `compile_error`, which the plan's steps and each
row's `once` outcomes say, else 1. Nothing here is stored: a value is
worked out on every read.

What reaches an entrant, or anything an entrant reads such as a board, is
`for_entrants`: every value reading an `entered` mark, directly or through
others, left out, until the grading queue decides when an entry may reach
its entrant (phase 5.8, stage 5 section 6 item 45).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

from forge.domain.blocks.catalogue import ALL, ENTERED
from forge.domain.blocks.evaluate import (
    SYSTEM_ERROR,
    Column,
    Outcome,
    RunResult,
    evaluate,
    using,
)
from forge.domain.blocks.numbers import exact
from forge.domain.definitions import Scoring
from forge.domain.release import seconds, seconds_late
from forge.domain.run_plan import COMPILE_ERROR, RunPlan
from forge.domain.run_task import Run, holds

NOT_RUN = "not_run"
"""The verdict a run reads with when the grading never ran it."""


@dataclass(frozen=True, slots=True)
class RunGrading:
    """A grading's runs as its values and its readers read them: each run
    of the live publication a row holds, as that row's result has it, by
    id; the ids the early row holds; every run as a fold reads it; what the
    steps done once reported and returned, the early row's and the late
    row's; the `compiled` fact; each run a row holds that the live
    publication no longer has, by id; the number each value's program
    reported, by the value's name; and the values whose program waits.
    """

    results: Mapping[str, Mapping[str, Any]]
    early: frozenset[str]
    read: Mapping[str, RunResult]
    once_early: Mapping[str, Any]
    once_late: Mapping[str, Any]
    compiled: int
    dropped: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    programs: Mapping[str, Fraction] = field(default_factory=dict)
    programs_waiting: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Worked:
    """A grading and what it works out to: each value's outcome and the
    facts it read."""

    grading: RunGrading
    outcomes: Mapping[str, Outcome]
    facts: Mapping[str, Fraction]


def for_entrants(values: Mapping[str, object], worked: Worked) -> Worked:
    """`worked` as an entrant, or anything an entrant reads, may be given
    it: each value reading an `entered` mark of `values`, a task's values,
    directly or through others, left out (stage 5 section 6 item 45).
    """
    withheld = using(values, ENTERED)
    return Worked(
        worked.grading,
        {name: outcome for name, outcome in worked.outcomes.items() if name not in withheld},
        worked.facts,
    )


def compilers(plan: RunPlan) -> frozenset[str]:
    """The steps done once that are not tainted, whose `compile_error` makes
    `compiled` 0 (stage 4 open 79): a workflow's, never a value's
    program's."""
    return frozenset(
        name
        for name, step in plan.steps.items()
        if step.once and step.tainted is None and step.value is None
    )


def run_grading(
    runs: Sequence[Run],
    early: Mapping[str, Any] | None,
    late: Mapping[str, Any] | None,
    *,
    waiting: Collection[str],
    compiling: Collection[str],
    programs_waiting: Collection[str] = (),
) -> RunGrading:
    """The grading of the live publication's `runs` from its early row's
    result and its late row's, none for a row with none; `waiting` the runs
    that wait when neither row holds them; `compiling` the steps `compiled`
    reads; `programs_waiting` the values whose program neither row gives,
    while a row whose plan runs it is still to give it.
    """
    live = {run.id for run in runs}
    early_runs = {str(run["id"]): run for run in (early or {}).get("runs", ())}
    late_runs = {str(run["id"]): run for run in (late or {}).get("runs", ())}
    held = late_runs | early_runs
    once_early = (early or {}).get("once") or {}
    once_late = (late or {}).get("once") or {}
    reports_early = once_early.get("reports") or {}
    reports_late = {**reports_early, **(once_late.get("reports") or {})}
    read: dict[str, RunResult] = {}
    for run in runs:
        entry = held.get(run.id)
        if entry is None:
            verdict = SYSTEM_ERROR if run.id in waiting else NOT_RUN
            read[run.id] = RunResult(run.id, verdict, run.key)
            continue
        once = reports_early if run.id in early_runs else reports_late
        reports = {**once, **(entry.get("reports") or {})}
        read[run.id] = RunResult(run.id, str(entry["verdict"]), run.key, reports)
    programs: dict[str, Fraction] = {}
    for result in (early, late):
        for name, number in ((result or {}).get("values") or {}).items():
            found = exact(number)
            if found is not None:
                programs[str(name)] = found
    stopped = any(
        (once.get("outcomes") or {}).get(step) == COMPILE_ERROR
        for once in (once_early, once_late)
        for step in compiling
    )
    return RunGrading(
        {name: entry for name, entry in held.items() if name in live},
        frozenset(name for name in early_runs if name in live),
        read,
        once_early,
        once_late,
        0 if stopped else 1,
        {name: entry for name, entry in held.items() if name not in live},
        programs,
        frozenset(name for name in programs_waiting if name not in programs),
    )


def facts_of(
    due: datetime | None, start: datetime, at: datetime, compiled: int
) -> dict[str, Fraction]:
    """The facts of a submission made `at`, against the row's `due` and the
    contest's `start`, with `compiled`."""
    return {
        "seconds_late": seconds_late(due, at),
        "seconds": seconds(start, at),
        "compiled": Fraction(compiled),
    }


def worked_out(
    scoring: Scoring,
    runs: Sequence[Run],
    grading: RunGrading,
    facts: Mapping[str, Fraction],
    columns: Mapping[str, Column],
    entered: Mapping[str, Fraction] | None = None,
) -> dict[str, Outcome]:
    """Each of the scoring's values over the grading, its groups' runs in
    the task's order, `all` among them, `columns` each report's bounds as
    the live plan gives them, and `entered` the mark entered for each value
    of the submission.
    """
    groups = {ALL: [grading.read[run.id] for run in runs]}
    for name, picked in scoring.groups.items():
        groups[name] = [grading.read[run.id] for run in runs if holds(picked, run.key)]
    return evaluate(
        scoring.values,
        groups,
        facts,
        columns,
        entered=entered,
        programs=grading.programs,
        programs_waiting=grading.programs_waiting,
    )


__all__ = [
    "NOT_RUN",
    "RunGrading",
    "Worked",
    "compilers",
    "facts_of",
    "for_entrants",
    "run_grading",
    "worked_out",
]
