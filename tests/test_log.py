"""Every line the package logs is one JSON record, a secret comes out masked,
and `setup` sends every logger in the process through the one handler at the
level the environment names.
"""

import json
import logging
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from forge.log import MASK, QUIET, JsonFormatter, get_logger, setup
from forge.testing import logged


def test_a_record_is_one_json_object_with_named_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    get_logger("forge.test").info("workspace.opened", contestant="c-1", attempt=2)

    (record,) = logged(caplog, "workspace.opened")
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

    (record,) = logged(caplog, "token.seen")
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

    (record,) = logged(caplog, "pass.failed")
    assert record["name"] == "drift"
    assert "ValueError: boom" in record["exception"]


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """The root logger, put back as it was after `setup` has replaced its
    handlers.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_setup_sends_every_logger_through_one_json_handler_at_the_named_level(
    root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_LOG_LEVEL", "warning")

    setup()

    (handler,) = root_logger.handlers
    assert isinstance(handler.formatter, JsonFormatter)
    assert root_logger.level == logging.WARNING
    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, "Started", None, None)
    assert json.loads(handler.format(record))["logger"] == "uvicorn.error"


def test_setup_keeps_the_http_clients_request_lines_out_at_info(
    root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_LOG_LEVEL", "info")
    quiet = [logging.getLogger(name) for name in QUIET]
    levels = [logger.level for logger in quiet]
    try:
        setup()

        assert [logger.getEffectiveLevel() for logger in quiet] == [logging.WARNING] * len(quiet)
        assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.INFO
    finally:
        for logger, level in zip(quiet, levels, strict=True):
            logger.setLevel(level)
