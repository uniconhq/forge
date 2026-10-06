"""What a contestant is shown of a result before and after the task's reveal:
each group by its `show`, the outcome over the groups whose verdict is
shown, what stopped the run unless a sealed step may have, and the values
of steps that are not sealed.
"""

from datetime import UTC, datetime
from typing import Any

from forge.domain.definitions import Group, Show
from forge.domain.showing import NOTHING_SEALED, GroupShown, Sealed, group_of, shown

REVEAL = datetime(2026, 6, 1, 14, tzinfo=UTC)

GROUPS = {
    "samples": Group(),
    "small": Group.model_validate({"pass": 30, "show": "verdict"}),
    "large": Group.model_validate({"pass": 70, "show": "after_close"}),
}


def row(test: str, outcome: str = "accepted", **values: Any) -> dict[str, Any]:
    return {"test": test, "outcome": outcome, "values": values}


RESULT: dict[str, Any] = {
    "schema_version": 5,
    "stopped": None,
    "tests": [
        row("large/1", "time_limit", time_ms=2000),
        row("samples/1", time_ms=12),
        row("samples/2", time_ms=11),
        row("small/1", time_ms=30),
        row("small/2", "wrong_answer", time_ms=31),
    ],
    "values": {"log": ""},
    "run_log": None,
    "error": None,
}


def test_before_the_reveal_each_group_shows_what_its_show_allows() -> None:
    seen = shown(RESULT, GROUPS, NOTHING_SEALED, revealed=False, reveal_at=REVEAL)

    samples, small, large = seen.groups
    assert samples == GroupShown(
        group="samples",
        show=Show.ALWAYS,
        outcome="accepted",
        tests=(
            {"test": "samples/1", "outcome": "accepted", "values": {"time_ms": 12}},
            {"test": "samples/2", "outcome": "accepted", "values": {"time_ms": 11}},
        ),
        shown_at=None,
    )
    assert small == GroupShown("small", Show.VERDICT, "wrong_answer", None, REVEAL)
    assert large == GroupShown("large", Show.AFTER_CLOSE, None, None, REVEAL)
    assert seen.values == {"log": ""}
    assert seen.stopped is None


def test_the_outcome_is_over_the_groups_whose_verdict_is_shown_in_group_order() -> None:
    before = shown(RESULT, GROUPS, NOTHING_SEALED, revealed=False, reveal_at=REVEAL)
    assert before.outcome == "wrong_answer"

    hidden_failure = {
        **RESULT,
        "tests": [row("large/1", "time_limit"), row("samples/1"), row("small/1")],
    }
    passing = shown(hidden_failure, GROUPS, NOTHING_SEALED, revealed=False, reveal_at=REVEAL)
    assert passing.outcome == "accepted"

    after = shown(hidden_failure, GROUPS, NOTHING_SEALED, revealed=True, reveal_at=REVEAL)
    assert after.outcome == "time_limit"


def test_after_the_reveal_everything_is_shown() -> None:
    seen = shown(RESULT, GROUPS, NOTHING_SEALED, revealed=True, reveal_at=REVEAL)

    assert [(group.outcome, group.shown_at) for group in seen.groups] == [
        ("accepted", None),
        ("wrong_answer", None),
        ("time_limit", None),
    ]
    assert seen.groups[2].tests == (
        {"test": "large/1", "outcome": "time_limit", "values": {"time_ms": 2000}},
    )
    assert seen.outcome == "wrong_answer"


def test_a_task_showing_nothing_yet_has_no_outcome() -> None:
    hidden = {"large": GROUPS["large"]}

    seen = shown(RESULT, hidden, NOTHING_SEALED, revealed=False, reveal_at=REVEAL)

    assert seen.outcome is None
    assert seen.groups == (GroupShown("large", Show.AFTER_CLOSE, None, None, REVEAL),)


def test_a_stop_by_a_step_that_is_not_sealed_is_shown_at_once() -> None:
    stopped = {
        **RESULT,
        "stopped": "compile_error",
        "tests": [row(test, "skipped") for test in ("large/1", "samples/1", "small/1")],
        "values": {"log": "main.cpp:3: error"},
    }

    seen = shown(stopped, GROUPS, NOTHING_SEALED, revealed=False, reveal_at=REVEAL)

    assert (seen.stopped, seen.outcome) == ("compile_error", "compile_error")
    assert seen.values == {"log": "main.cpp:3: error"}


AFTER_CLOSE = {
    "main": Group.model_validate({"each": 1, "show": "after_close"}),
}
SEALED = Sealed(stop=True, values=frozenset({"accuracy", "fraction"}))


def test_a_sealed_steps_stop_and_values_wait_for_the_reveal() -> None:
    result = {
        **RESULT,
        "stopped": "runtime_error",
        "tests": [row("main/1", "skipped")],
        "values": {"accuracy": 0.9, "log": "notebook ran"},
    }

    before = shown(result, AFTER_CLOSE, SEALED, revealed=False, reveal_at=REVEAL)
    assert (before.stopped, before.outcome) == (None, None)
    assert before.values == {"log": "notebook ran"}

    after = shown(result, AFTER_CLOSE, SEALED, revealed=True, reveal_at=REVEAL)
    assert (after.stopped, after.outcome) == ("runtime_error", "runtime_error")
    assert after.values == {"accuracy": 0.9, "log": "notebook ran"}


def test_a_sealed_per_test_value_is_held_on_its_rows_until_the_reveal() -> None:
    groups = {"main": Group.model_validate({"each": 1})}
    result = {**RESULT, "tests": [row("main/1", fraction=0.5, time_ms=3)], "values": {}}

    before = shown(result, groups, SEALED, revealed=False, reveal_at=REVEAL)
    assert before.groups[0].tests == (
        {"test": "main/1", "outcome": "accepted", "values": {"time_ms": 3}},
    )

    after = shown(result, groups, SEALED, revealed=True, reveal_at=REVEAL)
    assert after.groups[0].tests == (
        {"test": "main/1", "outcome": "accepted", "values": {"fraction": 0.5, "time_ms": 3}},
    )


def test_a_test_no_group_lists_is_not_shown_and_a_group_with_no_rows_passes() -> None:
    groups = {"main": Group(), "extra": Group()}
    result = {**RESULT, "tests": [row("old/1", "wrong_answer"), row("main/1")]}

    seen = shown(result, groups, NOTHING_SEALED, revealed=True, reveal_at=None)

    assert [group.group for group in seen.groups] == ["main", "extra"]
    assert seen.groups[1].tests == ()
    assert seen.outcome == "accepted"
    assert group_of("samples/12") == "samples"
