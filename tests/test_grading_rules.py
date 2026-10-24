"""The rules a grading run keeps to, without a database: the package's copy of
the runner's contract files is the runner's own and checks what it should,
and the run's clock agrees with the CI's; and which delayed submissions an
entrant still has room for under a task's `submissions.max`, the earliest
first.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.contracts import CONTRACTS, schema_text, violation
from forge.domain.grading import (
    BASE_WALL,
    REPORT_ALLOWANCE,
    RUN_TIMEOUT,
    STEP_OVERHEAD,
    WALL_CEILING,
    Place,
    log_key,
    room,
    run_deadline,
    wall_of,
)
from tests.conftest import sibling

GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")


@pytest.mark.parametrize("contract", CONTRACTS)
def test_the_contract_files_are_the_runners_own(contract: str) -> None:
    published = sibling("runner", "schemas", f"{contract}.schema.json")
    assert schema_text(contract) == published.read_text(encoding="utf-8")


@pytest.mark.parametrize("contract", ["result", "plan", "submission"])
def test_the_runners_examples_keep_the_contracts(contract: str) -> None:
    example = json.loads(sibling("runner", "examples", f"{contract}-v6.json").read_text("utf-8"))
    assert violation(example, contract) is None


def test_the_runners_run_task_result_keeps_its_contract() -> None:
    example = json.loads(sibling("runner", "examples", "result-v6.json").read_text("utf-8"))
    assert violation(example, "result") is None
    assert violation(example, "result", "result_v6") is None


@pytest.mark.parametrize(
    ("name", "kept"),
    [("log", True), ("judge.feedback", True), ("judge.x.y", False), ("Judge.x", False)],
)
def test_a_text_is_named_by_its_output_or_a_programs(name: str, kept: bool) -> None:
    example = json.loads(sibling("runner", "examples", "result-v6.json").read_text("utf-8"))
    example["once"]["texts"]["log"]["name"] = name
    assert (violation(example, "result", "result_v6") is None) is kept


def test_a_document_that_breaks_a_contract_says_where() -> None:
    assert violation({"schema_version": 6}, "result") == (
        "at the top: 'runs' is a required property"
    )
    with pytest.raises(ValueError):
        schema_text("verdict")


def test_the_wall_clock_is_the_steps_limits_with_a_margin_for_each() -> None:
    assert wall_of([60_000, 20_000]) == 60 + 20 + 60 + 2 * 15
    assert wall_of([1]) == 1 + 60 + 15
    assert (timedelta(seconds=60), timedelta(seconds=15)) == (BASE_WALL, STEP_OVERHEAD)
    assert timedelta(minutes=25) == WALL_CEILING
    assert WALL_CEILING + REPORT_ALLOWANCE < RUN_TIMEOUT


def test_a_runs_deadline_is_its_wall_clock_and_the_time_kept_for_reporting() -> None:
    fetched = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert run_deadline(fetched, 100) == fetched + timedelta(seconds=100) + REPORT_ALLOWANCE


def test_a_log_is_kept_by_grading_and_attempt() -> None:
    assert log_key(GRADING, 2) == f"logs/{GRADING}/2.log"


HOLDS, DELAYED, NONE = Place.HOLDS, Place.DELAYED, Place.NONE


def test_a_delayed_submission_is_graded_again_while_the_places_held_leave_room() -> None:
    places = {1: HOLDS, 2: DELAYED, 3: NONE}

    assert room(places, 2, [2]) == {2}
    assert room(places, 1, [2]) == set()
    # Over the limit, it is taken again once there is room.
    assert room({1: HOLDS, 2: NONE}, 2, [2]) == {2}


def test_the_earliest_delayed_submissions_take_the_room_left() -> None:
    places = {1: HOLDS, 2: DELAYED, 3: DELAYED, 4: DELAYED}

    # One place left: the later one asked for alone is refused, its place
    # kept for the earlier delayed one's own retry.
    assert room(places, 2, [3]) == set()
    assert room(places, 2, [2]) == {2}
    # Two left, asked for together: the two made first.
    assert room(places, 3, [2, 3, 4]) == {2, 3}
    # One over the limit made before a delayed one comes first too.
    assert room({1: NONE, 2: DELAYED}, 1, [1, 2]) == {1}


def test_a_submission_waiting_for_its_result_holds_its_place() -> None:
    assert room({1: HOLDS, 2: HOLDS, 3: DELAYED}, 2, [3]) == set()
