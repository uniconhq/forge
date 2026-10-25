import json
import re
from enum import StrEnum
from typing import Annotated

import regex
from pydantic import Field, PlainValidator

from forge.domain.errors import InvalidName
from forge.domain.names import validate_contest_or_task_name
from forge.domain.yaml_models import name_rule

CONTEST_FILE = "contest.yaml"
TASK_FILE = "task.yaml"
STATEMENT_FILE = "statement.md"

Flag = Annotated[bool, Field(strict=True)]


class State(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ContestVisibility(StrEnum):
    """Who sees the contest at all: anyone, guests included; anyone signed
    in; or only its contestants and organisers.
    """

    EVERYONE = "everyone"
    SIGNED_IN = "signed-in"
    HIDDEN = "hidden"


class Approval(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


def _pattern(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise ValueError("Give a pattern on one line, written as a regular expression.")
    try:
        regex.compile(value)
    except regex.error as error:
        raise ValueError(f"This is not a regular expression Unicon can read: {error}.") from None
    return value


def _task_handle(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("This must be text.")
    try:
        return validate_contest_or_task_name(value)
    except InvalidName:
        raise ValueError(name_rule(value, 24)) from None


TaskHandle = Annotated[str, PlainValidator(_task_handle)]
Count = Annotated[int, Field(strict=True, ge=1)]
Seconds = Annotated[int, Field(strict=True, ge=1)]
VALUE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
"""A domain a sign-up takes addresses at: labels of lower-case letters,
digits and hyphens, two or more, joined by dots, no label starting or ending
with a hyphen."""
UNDER = "*."
"""What a sign-up's domain starts with to take the domain after it and every
domain under it: `*.nus.edu.sg` takes `nus.edu.sg` and `comp.nus.edu.sg`
(#439). It stands only as the whole first label; nothing else is wild."""


def _domain(value: object) -> str:
    if not isinstance(value, str) or not DOMAIN.fullmatch(value.removeprefix(UNDER)):
        raise ValueError(
            "Give a domain such as u.nus.edu, or *.nus.edu.sg to take nus.edu.sg and every "
            "domain under it: lower case letters, digits, hyphens and dots, with at least one "
            "dot, no dot or hyphen at either end, and * only as the whole first part."
        )
    return value


Domain = Annotated[str, PlainValidator(_domain)]


def _yaml_text(value: str) -> str:
    """`value` as a YAML scalar in double quotes; a JSON string is one."""
    return json.dumps(" ".join(value.split()), ensure_ascii=False)


STARTER_STATEMENT = b"Write the statement entrants read here.\n"
"""The placeholder statement a new task is created with: the one copy of it,
which `statement_written` compares against."""
