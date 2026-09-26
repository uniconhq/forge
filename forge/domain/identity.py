"""Who a person is at the forge, the credential the platform holds to act as
them, and the identity a call through the port is made under.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class User:
    """A person as the forge describes them. `id` is the forge's own numeric id
    and is the only key the platform stores; the username is display data and
    can change.
    """

    id: int
    username: str
    name: str | None = None
    email: str | None = None
    avatar_url: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class Credential:
    """What the platform presents to act at the forge as one user. It expires
    and is refreshed from `refresh` before it does.
    """

    access: str
    refresh: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Platform:
    """The platform's own service account, which provisions and creates
    protected versions.
    """


@dataclass(frozen=True, slots=True)
class AsUser:
    """A call made as one person with their own credential, so the forge records
    the change as theirs and enforces their permissions.
    """

    user_id: int
    credential: Credential


@dataclass(frozen=True, slots=True)
class AsOrgAccount:
    """The service account that registers and grades for one org."""

    org: str


@dataclass(frozen=True, slots=True)
class CiAdmin:
    """The CI's own administrator, which creates users and agents at the CI
    and reads nothing at the forge.
    """


Identity = Platform | AsUser | AsOrgAccount | CiAdmin

PLATFORM = Platform()
CI_ADMIN = CiAdmin()
