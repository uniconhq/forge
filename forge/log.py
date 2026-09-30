"""One structured logger for the whole package. Every event is one JSON record
with named fields, so logs can be searched and filtered rather than parsed out
of sentences. Every module logs through `get_logger` and nothing prints.

A `SecretStr` or `SecretBytes` given as a field is written masked, so a token
passed to the logger by mistake never reaches a line. The hosting process
calls `setup` once, before anything logs, and writes its own records through
`get_logger`, so every line the process prints has the same shape.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretBytes, SecretStr

from forge.settings import load_log_settings

MASK = "**********"
QUIET = ("httpx", "httpcore", "boto3", "botocore", "s3transfer", "urllib3")
"""The HTTP clients' loggers: the forge's, which writes every request's whole
URL at info, and the object store's, which writes every request with its
signed headers at debug. A URL of the sign-in dance carries a one-time code
and its state, so these say only what goes wrong; the package logs what it
asked the forge and the store itself."""

Fields = dict[str, Any]


class Logger:
    """`log.info("workspace.opened", contestant=id)`: an event name and fields,
    never a formatted sentence.
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        """An error record carrying the exception being handled."""
        self._log(logging.ERROR, event, fields, exc_info=True)

    def _log(self, level: int, event: str, fields: Fields, exc_info: bool = False) -> None:
        self._logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)


def get_logger(name: str) -> Logger:
    return Logger(name)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, event, then the fields."""

    def format(self, record: logging.LogRecord) -> str:
        document: Fields = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            document.update({name: plain(value) for name, value in fields.items()})
        if record.exc_info and record.exc_info[0] is not None:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document, default=plain, ensure_ascii=False)


def plain(value: Any) -> Any:
    """A JSON-safe copy of a field value. Secrets come out masked."""
    if isinstance(value, SecretStr | SecretBytes):
        return MASK
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(name): plain(item) for name, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(item) for item in value]
    return str(value)


def setup() -> None:
    """Send every record from every logger in the process, the web server's
    included, through the one JSON handler on stderr, at the level
    `UNICON_LOG_LEVEL` names, except the HTTP clients', which log from a
    warning up whatever the level. A level that is not one stops the process
    with the variable named.
    """
    level = load_log_settings().log_level
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in QUIET:
        logging.getLogger(name).setLevel(max(root.level, logging.WARNING))
