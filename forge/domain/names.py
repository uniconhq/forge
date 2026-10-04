"""The names people give things, and the rules they follow. A name is lower
case letters, digits, hyphens and underscores, starts with a letter or a
digit, and is at most 24 characters for a contest or task and 40 for
anything else, an org included. It is a handle, not a title: it sits in a URL, and the
title people read is in the thing's own settings. An org, a contest and a
task are filed under a key (`forge.domain.keys`), and their name is only a
label for it; the rules also hold for a key, so a test that files a thing
under its name gets a key the forge takes.
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


def validate_name(value: str, *, max_length: int = OTHER_MAX) -> str:
    if not NAME.match(value):
        raise InvalidName(f"{value!r} must be lower case letters, digits, hyphens and underscores")
    if len(value) > max_length:
        raise InvalidName(f"{value!r} is longer than {max_length} characters")
    return value


def validate_contest_or_task_name(value: str) -> str:
    return validate_name(value, max_length=CONTEST_OR_TASK_MAX)


def validate_org_name(value: str) -> str:
    return validate_name(value, max_length=OTHER_MAX)


def service_account_name(org: str) -> str:
    """The username of the org's own service account at the forge, the one
    that activates and grades its tasks: `unicon-ci-<org>`.
    """
    return f"{SERVICE_ACCOUNT_PREFIX}{org}"


def is_service_account(username: str) -> bool:
    """Whether a username has the shape reserved for org service accounts, so
    the operator's command refuses to make a person's account under it. It
    decides no permission: anyone may sign up at the forge under such a name,
    and the package knows a service account by its id in `org_accounts`.
    """
    return username.lower().startswith(SERVICE_ACCOUNT_PREFIX)


USER_PREFIX = "u"


@dataclass(frozen=True, slots=True)
class UserOwner:
    """A workspace owned by one contestant, named by their user id: `u<id>`.
    Not by their username, which they can change at the forge and which
    another person can take once it is free, and who would then be handed
    this workspace.
    """

    user_id: int

    @property
    def segment(self) -> str:
        return f"{USER_PREFIX}{self.user_id}"


@dataclass(frozen=True, slots=True)
class TeamOwner:
    """A workspace owned by a team, named by the team's id. No team exists
    yet: this is the owner feature 14's team workspaces use.
    """

    team_id: uuid.UUID

    @property
    def segment(self) -> str:
        return f"{TEAM_PREFIX}{self.team_id}"


WorkspaceOwner = UserOwner | TeamOwner


@dataclass(frozen=True, slots=True)
class Named:
    """An org, a contest or a task by its id, with the name people call it."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ScopeNames:
    """The names of the org, the contest and the task a scope reaches, as far
    down as it does, in the shape of an address: `acme`, `acme/spring`,
    `acme/spring/sum`.
    """

    org: str
    contest: str | None = None
    task: str | None = None

    @property
    def path(self) -> str:
        return "/".join(part for part in (self.org, self.contest, self.task) if part)
