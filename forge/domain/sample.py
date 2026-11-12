"""A made-up graded submission, which the staff preview shows as an entrant
would read it. Every run gets a result by its place: accepted and
wrong_answer in turn. Each number an output reports is made up from the
run's and the output's places, its worst bound on a run that did not pass,
and kept within its bounds. Each text output is "text". Each value's
program gives a number, and each entered mark is its highest.
"""

from collections.abc import Collection, Mapping, Sequence
from fractions import Fraction
from typing import Any

from forge.domain.blocks.catalogue import ENTERED
from forge.domain.blocks.evaluate import Column, blocks_in
from forge.domain.blocks.numbers import decimal_of, exact
from forge.domain.run_plan import RunPlan
from forge.domain.run_task import Run
from forge.domain.step_data import output_type

ACCEPTED = "accepted"
WRONG = "wrong_answer"
NUMBER = "number"
TEXT = "text"
SECONDS = Fraction(600)
"""When the made-up submission came in, in seconds after the contest started."""


def _passed(place: int) -> bool:
    return place % 2 == 0


def _number(place: int, at: int, column: Column, passed: bool) -> Fraction:
    if not passed and column.worst is not None:
        return column.worst
    number = Fraction((place + 1) * (at + 1) * 10)
    if column.at_most is not None:
        number = min(number, column.at_most)
    if column.at_least is not None:
        number = max(number, column.at_least)
    return number


def _kinds(plan: RunPlan) -> dict[str, tuple[str, bool]]:
    """Each output the plan reports, a number or a text, by name: its kind,
    and whether it is given once, the same for every run."""
    kinds: dict[str, tuple[str, bool]] = {}
    for name, report in plan.reports.items():
        step = plan.steps[report.step]
        declared = output_type(step, report.output)
        kind = NUMBER if declared is not None and declared[0] == NUMBER else TEXT
        kinds[name] = (kind, step.gives_once(report.output))
    return kinds


def samples(runs: Sequence[Run], plan: RunPlan) -> list[dict[str, Any]]:
    """Each run's made-up result by field name, in the task's order: its
    verdict, `passed`, and each output, a number exactly or "text"."""
    kinds = _kinds(plan)
    found: list[dict[str, Any]] = []
    for place, _ in enumerate(runs):
        passed = _passed(place)
        row: dict[str, Any] = {"verdict": ACCEPTED if passed else WRONG, "passed": int(passed)}
        for at, (name, (kind, once)) in enumerate(kinds.items()):
            if kind == TEXT:
                row[name] = TEXT
                continue
            # An output given once is the same for every run.
            column = plan.reports[name].column()
            row[name] = decimal_of(_number(0 if once else place, at, column, once or passed))
        found.append(row)
    return found


def sample_result(runs: Sequence[Run], plan: RunPlan, programs: Collection[str]) -> dict[str, Any]:
    """The made-up result a grading row would hold: its runs, what the steps
    done once reported, and each value's program's number."""
    numbers = {name: once for name, (kind, once) in _kinds(plan).items() if kind == NUMBER}
    rows = samples(runs, plan)
    return {
        "runs": [
            {
                "id": run.id,
                "verdict": row["verdict"],
                "passed": row["passed"],
                "reports": {name: row[name] for name, once in numbers.items() if not once},
                "texts": {},
            }
            for run, row in zip(runs, rows, strict=True)
        ],
        "once": {
            "reports": {name: rows[0][name] for name, once in numbers.items() if once}
            if rows
            else {},
            "texts": {},
            "outcomes": {},
        },
        "values": {name: decimal_of(Fraction(10 * (at + 1))) for at, name in enumerate(programs)},
    }


def sample_marks(values: Mapping[str, object]) -> dict[str, Fraction]:
    """Each value's entered mark, at its highest."""
    marks: dict[str, Fraction] = {}
    for name, tree in values.items():
        found = blocks_in(tree, ENTERED)
        if found and (most := exact(found[0]["at_most"])) is not None:
            marks[name] = most
    return marks


__all__ = ["SECONDS", "sample_marks", "sample_result", "samples"]
