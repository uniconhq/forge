"""Every line the package logs is one JSON record, and a secret comes out
masked.
"""

import json
import logging
from typing import Any

import pytest
from pydantic import SecretStr

from forge.log import MASK, JsonFormatter, get_logger

FORMATTER = JsonFormatter()


def _records(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [
        json.loads(FORMATTER.format(record))
        for record in caplog.records
        if record.getMessage() == event
    ]


def test_a_record_is_one_json_object_with_named_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    get_logger("forge.test").info("workspace.opened", contestant="c-1", attempt=2)

    (record,) = _records(caplog, "workspace.opened")
    assert record["level"] == "INFO"
    assert record["logger"] == "forge.test"
    assert record["contestant"] == "c-1"
    assert record["attempt"] == 2
    assert record["time"].endswith("+00:00")


def test_a_secret_logged_on_purpose_comes_out_masked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    get_logger("forge.test").warning(
        "token.seen", token=SecretStr("gho_live_token"), nested={"key": SecretStr("k")}
    )

    (record,) = _records(caplog, "token.seen")
    line = json.dumps(record)
    assert "gho_live_token" not in line
    assert record["token"] == MASK
    assert record["nested"]["key"] == MASK


def test_an_exception_record_carries_the_traceback(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("forge.test").exception("pass.failed", name="drift")

    (record,) = _records(caplog, "pass.failed")
    assert record["name"] == "drift"
    assert "ValueError: boom" in record["exception"]
