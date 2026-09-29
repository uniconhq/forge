"""The names people give things, and the rules they follow. A name is lower
case letters, digits, hyphens and underscores, starts with a letter or a
digit, and is at most 24 characters for a contest or task, 30 for an org and
40 for anything else. It is a handle, not a title: it names a repository and
sits in a URL, and the dot that joins repository segments cannot be in it.
An org's name is shorter because its service account's, `unicon-ci-<org>`,
is a name too and must keep within 40.
"""

import re
import uuid
from dataclasses import dataclass

from forge.domain.errors import InvalidName

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CONTEST_OR_TASK_MAX = 24
OTHER_MAX = 40

TEAM_PREFIX = "team."
SERVICE_ACCOUNT_PREFIX = "unicon-ci-"
ORG_MAX = OTHER_MAX - len(SERVICE_ACCOUNT_PREFIX)


def validate_name(value: str, *, max_length: int = OTHER_MAX) -> str:
    if not NAME.match(value):
        raise InvalidName(f"{value!r} must be lower case letters, digits, hyphens and underscores")
    if len(value) > max_length:
        raise InvalidName(f"{value!r} is longer than {max_length} characters")
    return value


def validate_contest_or_task_name(value: str) -> str:
    return validate_name(value, max_length=CONTEST_OR_TASK_MAX)


def validate_org_name(value: str) -> str:
    return validate_name(value, max_length=ORG_MAX)


def service_account_name(org: str) -> str:
    """The username of the org's own service account at the forge, the one
    that registers and grades for it: `unicon-ci-<org>`.
    """
    return f"{SERVICE_ACCOUNT_PREFIX}{org}"


def is_service_account(username: str) -> bool:
    """Whether a username has the shape reserved for org service accounts, so
    the operator's command refuses to make a person's account under it. It
    decides no permission: anyone may sign up at the forge under such a name,
    and the package knows a service account by its id in `org_accounts`.
    """
    return username.lower().startswith(SERVICE_ACCOUNT_PREFIX)


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
