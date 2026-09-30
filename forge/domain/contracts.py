"""The runner's contract files, as the package holds them: `schemas/` is a
copy of the five schemas a runner release publishes, at `schema_version` 3,
the plan, the envelope, the verdict, the primitive and the submission. The
package checks what a run sends back, and what it hands a run, against
them; its tests check the copy against the runner's own when that repo is
checked out beside this one.
"""

import json
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = 3
CONTRACTS = ("plan", "envelope", "verdict", "primitive", "submission")
MESSAGE_LIMIT = 300


def schema_text(contract: str) -> str:
    """The schema of one contract, `verdict` for `verdict.schema.json` and so
    on, as the file holds it.
    """
    if contract not in CONTRACTS:
        raise ValueError(f"there is no contract named {contract!r}")
    return (resources.files("forge.domain") / "schemas" / f"{contract}.schema.json").read_text(
        encoding="utf-8"
    )


@cache
def _validator(contract: str) -> Draft202012Validator:
    schema: dict[str, Any] = json.loads(schema_text(contract))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def violation(document: object, contract: str) -> str | None:
    """The first way `document` breaks the contract, as `at <path>: <message>`,
    or none when it keeps it. Errors are taken in the order of where they
    are, so the same document always reports the same one, and the message
    is cut short, since some repeat the whole value they failed on.
    """
    errors = sorted(
        _validator(contract).iter_errors(document),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if not errors:
        return None
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "the top"
    message = first.message
    if len(message) > MESSAGE_LIMIT:
        message = message[: MESSAGE_LIMIT - 3] + "..."
    return f"at {location}: {message}"
