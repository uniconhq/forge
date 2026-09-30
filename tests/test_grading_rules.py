"""The rules a grading run keeps to, without a database: the package's copy of
the runner's contract files is the runner's own and checks what it should,
and the run's clock agrees with the CI's.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.domain.contracts import CONTRACTS, schema_text, violation
from forge.domain.grading import (
    REPORT_ALLOWANCE,
    RUN_TIMEOUT,
    WALL_CEILING,
    log_key,
    run_deadline,
    start_retry_wait,
    wall_seconds,
)
from forge.domain.plans import Plan

RUNNER = Path(__file__).resolve().parents[2] / "runner"
GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
IMAGE = "ghcr.io/uniconhq/primitive-compile@sha256:" + "0" * 64


@pytest.mark.skipif(
    not (RUNNER / "schemas").exists(), reason="the runner repo is not checked out beside this one"
)
@pytest.mark.parametrize("contract", CONTRACTS)
def test_the_contract_files_are_the_runners_own(contract: str) -> None:
    published = RUNNER / "schemas" / f"{contract}.schema.json"
    assert json.loads(schema_text(contract)) == json.loads(published.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    not (RUNNER / "examples").exists(), reason="the runner repo is not checked out beside this one"
)
@pytest.mark.parametrize("contract", ["envelope", "verdict", "plan", "submission"])
def test_the_runners_examples_keep_the_contracts(contract: str) -> None:
    example = json.loads((RUNNER / "examples" / f"{contract}.json").read_text(encoding="utf-8"))
    assert violation(example, contract) is None


def test_a_document_that_breaks_a_contract_says_where() -> None:
    assert violation({"schema_version": 3}, "verdict") == (
        "at the top: 'grading_id' is a required property"
    )


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
                    "entrypoint": ["/bin/x"],
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


def test_a_failed_start_waits_longer_each_time_up_to_five_minutes() -> None:
    waits = [start_retry_wait(failures).total_seconds() for failures in range(1, 9)]
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300]
    assert start_retry_wait(10_000) == timedelta(minutes=5)


def test_a_log_is_kept_by_grading_and_attempt() -> None:
    assert log_key(GRADING, 2) == f"logs/{GRADING}/2.log"
