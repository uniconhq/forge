"""The runs a draft's `repeat` makes for the task's cards: every combination
of the axes' tuples, the first axis outermost, with their titles and the
values the task gives, a tuple still being written read as far as it goes.
"""

from forge.domain.run_task.draft_runs import DraftRun, draft_runs
from forge.domain.run_task.records import MOST_RUNS


def test_the_runs_cross_the_axes_the_first_outermost() -> None:
    found = draft_runs(
        [
            {
                "inputs": ["input", "answer"],
                "tuples": [["a.in", "a.ans"], {"title": "B", "input": "b.in", "answer": "b.ans"}],
            },
            {"inputs": ["seed"], "tuples": [1, 2]},
        ]
    )

    assert [run.cells for run in found.runs] == [
        {"input": "a.in", "answer": "a.ans", "seed": 1},
        {"input": "a.in", "answer": "a.ans", "seed": 2},
        {"input": "b.in", "answer": "b.ans", "seed": 1},
        {"input": "b.in", "answer": "b.ans", "seed": 2},
    ]
    assert [run.titles for run in found.runs] == [(), (), ("B",), ("B",)]
    assert found.count == 4
    assert [len(axis) for axis in found.axes] == [2, 2]


def test_no_repeated_input_is_one_run_and_an_entrant_value_is_left_out() -> None:
    assert draft_runs([]).runs == (DraftRun(),)
    found = draft_runs([{"inputs": ["code", "n"], "tuples": [["entrant", None], ["entrant"]]}])
    assert [run.cells for run in found.runs] == [{}, {}]


def test_more_runs_than_a_task_may_have_are_counted_not_listed() -> None:
    found = draft_runs(
        [
            {"inputs": ["a"], "tuples": list(range(5 * MOST_RUNS))},
            {"inputs": ["b"], "tuples": [1, 2]},
        ]
    )

    assert found.runs == ()
    assert found.count == 10 * MOST_RUNS
