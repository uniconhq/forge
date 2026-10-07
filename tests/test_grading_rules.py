"""The rules a grading run keeps to, without a database: the package's copy of
the runner's contract files is the runner's own and checks what it should,
a report reads as one or is refused, a result is kept only when it matches
its schema, its numbers read exactly, and the run's clock agrees with the
CI's.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from forge.domain.contracts import CONTRACTS, schema_text, violation
from forge.domain.errors import InvalidCallback
from forge.domain.grading import (
    BASE_WALL,
    REPORT_ALLOWANCE,
    RUN_TIMEOUT,
    STEP_OVERHEAD,
    WALL_CEILING,
    log_key,
    run_deadline,
    wall_seconds,
)
from forge.domain.plans import Plan
from forge.domain.reports import RESULT_MAX, Event, read_report, result_problem
from tests.conftest import sibling

GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
IMAGE = "ghcr.io/uniconhq/primitive-compile@sha256:" + "0" * 64


@pytest.mark.parametrize("contract", CONTRACTS)
def test_the_contract_files_are_the_runners_own(contract: str) -> None:
    published = sibling("runner", "schemas", f"{contract}.schema.json")
    assert schema_text(contract) == published.read_text(encoding="utf-8")


@pytest.mark.parametrize("contract", ["envelope", "result", "plan", "submission"])
def test_the_runners_examples_keep_the_contracts(contract: str) -> None:
    example = json.loads(sibling("runner", "examples", f"{contract}.json").read_text("utf-8"))
    assert violation(example, contract) is None


def test_a_document_that_breaks_a_contract_says_where() -> None:
    assert violation({"schema_version": 5}, "result") == (
        "at the top: 'stopped' is a required property"
    )
    with pytest.raises(ValueError):
        schema_text("verdict")


def result(**changes: Any) -> dict[str, Any]:
    found: dict[str, Any] = {
        "schema_version": 5,
        "stopped": None,
        "stopped_by": None,
        "tests": [
            {"test": "main/1", "outcome": "accepted", "values": {"time_ms": 120}},
            {"test": "samples/1", "outcome": "wrong_answer", "values": {"time_ms": 15}},
        ],
        "values": {"log": ""},
        "run_log": None,
        "error": None,
    }
    found.update(changes)
    return found


def test_a_result_that_keeps_its_schema_is_kept() -> None:
    assert result_problem(result()) is None
    stopped = result(
        stopped="compile_error",
        stopped_by="compile",
        tests=[{"test": "main/1", "outcome": "skipped", "values": {}}],
        values={"log": "main.cpp:3: error"},
    )
    assert result_problem(stopped) is None
    assert result_problem(result(stopped="system_error", error="The checker crashed.")) is None


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"schema_version": 4}, "result.schema.json"),
        ({"stopped": "accepted"}, "result.schema.json"),
        ({"stopped": "system_error"}, "result.schema.json"),
        ({"stopped": "compile_error"}, "result.schema.json"),
        ({"stopped_by": "compile"}, "result.schema.json"),
        ({"stopped": "system_error", "error": "x", "stopped_by": "run"}, "result.schema.json"),
        ({"error": "a sentence with no stop"}, "result.schema.json"),
        ({"tests": [{"test": "main/1", "outcome": "great", "values": {}}]}, "result.schema.json"),
        ({"tests": [{"id": "1", "outcome": "accepted", "values": {}}]}, "result.schema.json"),
        ({"outcome": "accepted"}, "result.schema.json"),
        ({"values": {"log": "x" * RESULT_MAX}}, "more than the"),
        ({"values": {"score": float("nan")}}, "not a JSON document"),
    ],
)
def test_a_result_that_is_not_one_to_keep_says_why(changes: dict[str, Any], problem: str) -> None:
    found = result_problem(result(**changes))
    assert found is not None and problem in found


def test_a_result_that_is_not_an_object_is_not_kept() -> None:
    assert result_problem("accepted") is not None


def test_the_three_reports_read() -> None:
    assert read_report(b'{"event": "started"}').event is Event.STARTED
    progress = read_report(b'{"event": "progress", "step": "run", "done": 3, "total": 10}')
    assert progress.progress == {"step": "run", "done": 3, "total": 10}
    finished = read_report(json.dumps({"event": "finished", "result": {"a": 1}}).encode())
    assert (finished.event, finished.result) == (Event.FINISHED, {"a": 1})


def test_a_reports_numbers_read_exactly_as_written() -> None:
    body = b'{"event": "finished", "result": {"values": {"fraction": 1.0000000000000000001}}}'

    fraction = read_report(body).result["values"]["fraction"]

    assert fraction == Decimal("1.0000000000000000001")
    assert fraction > 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"event": "exploded"}',
        b'{"event": "progress", "step": "run", "done": -1, "total": 3}',
        b'{"event": "progress", "step": "run", "done": true, "total": 3}',
        b'{"event": "progress", "step": "run", "done": 4, "total": 3}',
        b'{"event": "progress", "step": "", "done": 1, "total": 3}',
        b'{"event": "progress", "done": 1, "total": 3}',
        b'{"event": "finished"}',
        b'{"event": "finished", "result": {"x": NaN}}',
        b"\xff\xfe",
    ],
)
def test_what_is_not_a_report_is_refused(body: bytes) -> None:
    with pytest.raises(InvalidCallback):
        read_report(body)


def _plan(*limits: int) -> Plan:
    return Plan.model_validate(
        {
            "harness_image": "ghcr.io/uniconhq/harness@sha256:" + "1" * 64,
            "tests": ["main/1"],
            "steps": [
                {
                    "id": f"s{index}",
                    "primitive": "unicon/compile@v2",
                    "image": IMAGE,
                    "network": False,
                    "limits": {
                        "time_ms": time_ms,
                        "cpu_ms": time_ms,
                        "memory_mb": 64,
                        "pids": 8,
                        "output_mb": 1,
                        "gpus": 0,
                    },
                    "outputs": {"outcome": "outcome"},
                    "inputs": {},
                }
                for index, time_ms in enumerate(limits)
            ],
        }
    )


def test_the_wall_clock_is_the_steps_limits_with_a_margin_for_each() -> None:
    assert wall_seconds(_plan(60_000, 20_000)) == 60 + 20 + 60 + 2 * 15
    assert wall_seconds(_plan(1)) == 1 + 60 + 15
    assert (timedelta(seconds=60), timedelta(seconds=15)) == (BASE_WALL, STEP_OVERHEAD)
    assert timedelta(minutes=25) == WALL_CEILING
    assert WALL_CEILING + REPORT_ALLOWANCE < RUN_TIMEOUT


def test_a_runs_deadline_is_its_wall_clock_and_the_time_kept_for_reporting() -> None:
    fetched = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert run_deadline(fetched, 100) == fetched + timedelta(seconds=100) + REPORT_ALLOWANCE


def test_a_log_is_kept_by_grading_and_attempt() -> None:
    assert log_key(GRADING, 2) == f"logs/{GRADING}/2.log"
