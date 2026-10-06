"""The rules a grading run keeps to, without a database: the package's copy of
the runner's contract files is the runner's own and checks what it should,
a report reads as one or is refused, a verdict is kept only when it matches
its schema, and the run's clock agrees with the CI's.
"""

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forge.domain.contracts import CONTRACTS, schema_text, violation
from forge.domain.errors import InvalidCallback
from forge.domain.grading import (
    REPORT_ALLOWANCE,
    RUN_TIMEOUT,
    WALL_CEILING,
    log_key,
    run_deadline,
    wall_seconds,
)
from forge.domain.plans import Plan
from forge.domain.reports import VERDICT_MAX, Event, read_report, verdict_problem
from tests.conftest import sibling

GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
IMAGE = "ghcr.io/uniconhq/primitive-compile@sha256:" + "0" * 64


@pytest.mark.parametrize("contract", CONTRACTS)
def test_the_contract_files_are_the_runners_own(contract: str) -> None:
    published = sibling("runner", "schemas", f"{contract}.schema.json")
    assert schema_text(contract) == published.read_text(encoding="utf-8")


@pytest.mark.parametrize("contract", ["envelope", "verdict", "plan", "submission"])
def test_the_runners_examples_keep_the_contracts(contract: str) -> None:
    example = json.loads(sibling("runner", "examples", f"{contract}.json").read_text("utf-8"))
    assert violation(example, contract) is None


def test_a_document_that_breaks_a_contract_says_where() -> None:
    assert violation({"schema_version": 4}, "verdict") == (
        "at the top: 'outcome' is a required property"
    )


def _verdict(**changes: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "schema_version": 4,
        "outcome": "wrong_answer",
        "metrics": {"points": 0},
        "tests": [
            {"id": "1", "outcome": "wrong_answer", "time_ms": 3, "memory_kb": None, "metrics": {}}
        ],
        "summary": "0 of 1 tests accepted.",
        "log": None,
    }
    verdict.update(changes)
    return verdict


def test_a_verdict_that_keeps_its_schema_is_kept() -> None:
    assert verdict_problem(_verdict()) is None


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"outcome": "great"}, "verdict.schema.json"),
        ({"metrics": {"Points": 1}}, "verdict.schema.json"),
        ({"outcome": "system_error"}, "verdict.schema.json"),
        ({"grading_id": "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"}, "verdict.schema.json"),
        ({"summary": "x" * VERDICT_MAX}, "more than the"),
        ({"metrics": {"points": math.nan}}, "not a JSON document"),
    ],
)
def test_a_verdict_that_is_not_one_to_keep_says_why(changes: dict[str, Any], problem: str) -> None:
    found = verdict_problem(_verdict(**changes))
    assert found is not None and problem in found


def test_a_verdict_that_is_not_an_object_is_not_kept() -> None:
    assert verdict_problem("accepted") is not None


def test_the_three_reports_read() -> None:
    assert read_report(b'{"event": "started"}').event is Event.STARTED
    progress = read_report(b'{"event": "progress", "step": "run", "done": 3, "total": 10}')
    assert progress.progress == {"step": "run", "done": 3, "total": 10}
    finished = read_report(json.dumps({"event": "finished", "verdict": {"a": 1}}).encode())
    assert (finished.event, finished.verdict) == (Event.FINISHED, {"a": 1})


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"event": "exploded"}',
        b'{"event": "progress", "step": "run", "done": -1, "total": 3}',
        b'{"event": "progress", "step": "run", "done": true, "total": 3}',
        b'{"event": "progress", "step": "", "done": 1, "total": 3}',
        b'{"event": "progress", "done": 1, "total": 3}',
        b'{"event": "finished"}',
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
            "stage": "default",
            "steps": [
                {
                    "id": f"s{index}",
                    "primitive": "compile@v1",
                    "image": IMAGE,
                    "limits": {
                        "time_ms": time_ms,
                        "cpu_ms": time_ms,
                        "memory_mb": 64,
                        "pids": 8,
                        "output_mb": 1,
                    },
                    "inputs": {},
                }
                for index, time_ms in enumerate(limits)
            ],
            "verdict": {"outcome": {"step": "s0", "output": "outcome"}},
        }
    )


def test_the_wall_clock_is_the_steps_limits_with_a_margin_and_never_past_the_ceiling() -> None:
    assert wall_seconds(_plan(60_000, 20_000)) == 60 + 20 + 60 + 2 * 15
    assert wall_seconds(_plan(10_000_000)) == WALL_CEILING.total_seconds()
    assert WALL_CEILING + REPORT_ALLOWANCE < RUN_TIMEOUT


def test_a_runs_deadline_is_its_wall_clock_and_the_time_kept_for_reporting() -> None:
    fetched = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert run_deadline(fetched, 100) == fetched + timedelta(seconds=100) + REPORT_ALLOWANCE


def test_a_log_is_kept_by_grading_and_attempt() -> None:
    assert log_key(GRADING, 2) == f"logs/{GRADING}/2.log"
