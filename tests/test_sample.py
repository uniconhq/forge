"""The made-up graded submission the staff preview shows: runs pass in
turn, a number stays within its bounds, a run that did not pass reports its
worst, a text output is "text", and an entered mark is its highest."""

from decimal import Decimal

from forge.domain.run_task import Run
from forge.domain.sample import sample_marks, sample_result, samples
from tests.test_run_task import MEAN, MEAN_MANIFEST, MEAN_PATHS
from tests.test_step_data import _planned


def test_runs_pass_in_turn_with_numbers_in_their_bounds() -> None:
    plan = _planned(MEAN, MEAN_PATHS, manifests={"programs/mean/program.yaml": MEAN_MANIFEST})
    runs = [Run(run.id, {}, True) for run in plan.runs]

    rows = samples(runs, plan)

    assert [(row["verdict"], row["passed"], row["score"]) for row in rows] == [
        ("accepted", 1, Decimal(1)),
        ("wrong_answer", 0, Decimal(0)),
        ("accepted", 1, Decimal(1)),
        ("wrong_answer", 0, Decimal(0)),
    ]
    assert rows[0]["log"] == "text"
    result = sample_result(runs, plan, ["judged"])
    assert result["runs"][1]["reports"] == {
        name: rows[1][name] for name in ("score", "time_ms", "memory_kb")
    }
    assert result["values"] == {"judged": Decimal(10)}


def test_an_entered_mark_is_its_highest() -> None:
    entered = {"block": "entered", "at_least": 0, "at_most": 7}
    values = {"mark": {"block": "add", "of": [entered, 1]}, "plain": 3}

    assert sample_marks(values) == {"mark": 7}
