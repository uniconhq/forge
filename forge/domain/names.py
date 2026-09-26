"""The names people give things, and the rules they follow. A name is lower
case letters, digits and hyphens, starts with a letter, and is at most 24
characters for a contest or task and 40 for anything else.
"""

import re
import uuid
from dataclasses import dataclass

NAME = re.compile(r"^[a-z][a-z0-9-]*$")
CONTEST_OR_TASK_MAX = 24
OTHER_MAX = 40

TEAM_PREFIX = "team."


class InvalidName(ValueError):
    """The name breaks the character or length rule."""


def validate_name(value: str, *, max_length: int = OTHER_MAX) -> str:
    if not NAME.match(value):
        raise InvalidName(f"{value!r} must be lower case letters, digits and hyphens")
    if len(value) > max_length:
        raise InvalidName(f"{value!r} is longer than {max_length} characters")
    return value


def validate_contest_or_task_name(value: str) -> str:
    return validate_name(value, max_length=CONTEST_OR_TASK_MAX)


@dataclass(frozen=True, slots=True)
class UserOwner:
    """A workspace owned by one contestant, named by their username."""

    username: str

    @property
    def segment(self) -> str:
        return self.username.lower()


@dataclass(frozen=True, slots=True)
class TeamOwner:
    """A workspace owned by a team, named by the team's id."""

    team_id: uuid.UUID

    @property
    def segment(self) -> str:
        return f"{TEAM_PREFIX}{self.team_id}"


WorkspaceOwner = UserOwner | TeamOwner


def owner_from_segment(segment: str) -> WorkspaceOwner:
    if segment.startswith(TEAM_PREFIX):
        return TeamOwner(uuid.UUID(segment.removeprefix(TEAM_PREFIX)))
    return UserOwner(segment)
