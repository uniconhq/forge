import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from forge.db.tables import Entrant as EntrantRow
from forge.domain.definitions import Locks
from forge.domain.entrants import EntrantRef
from forge.domain.identity import User
from forge.domain.ids import ContestId
from forge.domain.names import EntrantOwner
from forge.domain.registration import Status, WayKind

PLACES_LOCK = 0x504C4143
"""The first key of every lock on a contest's places, the second being the
contest's id hashed by Postgres."""
PEOPLE_LOCK = 0x50504C45
"""The first key of every lock on one person in one contest, the second being
the contest and the user id hashed by Postgres."""
NO_SUCH_ENTRANT = "There is no such entrant in this contest."
NOT_IN_ONE = "You have not entered this contest."
ENDED = "Entrants cannot change once the contest has ended. Ask the organisers."
ADDRESSES_UNREAD = (
    "Unicon could not check your email addresses just now, so it cannot tell whether you may "
    "register. Try again in a moment."
)
MEMBER_DAILY_MAX = 10
"""The most invites an entrant's members send in a day, together: a team
finds its few people, and no contestant mails strangers through the
platform."""


@dataclass(frozen=True, slots=True)
class Member:
    """Someone in an entrant: who they are at the forge, or none once their
    account is gone, the way they came in by, its kind and its name, and
    when they joined.
    """

    user_id: int
    user: User | None
    way_kind: WayKind
    way_name: str | None
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class Invited:
    """An invite into an entrant still waiting: whom it names, whether an
    organiser or a member sent it, who, and when it lapses.
    """

    invite: uuid.UUID
    username: str | None
    email: str | None
    invited_by: int
    by_organiser: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Entrant:
    """An entrant: its id, its own name, if it has one, and the name it is
    shown under; where its registration stands, when it was made and the last
    decision on it with, for a rejection, why; its members, oldest first, and
    the invites into it still waiting; its extension, how long and on which
    tasks, every task when it names none; whether it has submitted; whether
    it has as many members as the contest asks; and whether its roster has
    locked.
    """

    id: uuid.UUID
    contest: ContestId
    name: str | None
    display_name: str
    status: Status
    registered_at: datetime
    decided_at: datetime | None
    reason: str | None
    members: tuple[Member, ...]
    invites: tuple[Invited, ...]
    time_extension: timedelta
    extension_tasks: tuple[str, ...] | None
    submitted: bool
    complete: bool
    locked: bool


@dataclass(frozen=True, slots=True)
class Invitation:
    """One of the person's own invites into an entrant of the contest, still
    waiting: the invite, the entrant, whether an organiser sent it, and when
    it lapses.
    """

    invite: uuid.UUID
    entrant: EntrantRef
    by_organiser: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the person cannot register now: the code `register` would refuse
    with, and its sentence.
    """

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Mine:
    """What the person's registration panel shows: their entrant, if any,
    and their own invites into the contest's entrants; whether the window is
    open; whether they can register, and why not when they cannot; whether
    to ask them for a code, only when a code would let them in; and the
    contest's size rules and when a roster locks.
    """

    entrant: Entrant | None
    invitations: tuple[Invitation, ...]
    registration_open: bool
    can_register: bool
    asks_code: bool
    refused: Refusal | None
    most: int
    fewest: int
    locks: Locks


@dataclass(frozen=True, slots=True)
class Entered:
    """What accepting a place in a contest did: the entrant the person is in
    now, its status, and whether accepting `made` it, `joined` it, or found
    them in it `already`.
    """

    entrant: EntrantRef
    status: Status
    how: str


@dataclass(frozen=True, slots=True)
class Standing:
    """The entrant a person is in: its row, the workspace it owns, every
    member, and when the person joined it, by the package's clock and then
    their member row's id, which is made in order, which tells a first
    upload whether its place was made with them on it.
    """

    entrant: EntrantRow
    owner: EntrantOwner
    members: tuple[int, ...]
    since: tuple[datetime, uuid.UUID]
