"""An invite: an organiser asking one person, by their username or by an email
address, to take a contestant's place in a contest or an organiser's role at
an org, a contest or a task. This module holds what an invite grants, where
it stands, what its mail did, and the rules an invite is checked against
when it is made, each of which refuses with an error of its own.

An invite is `pending` until the person accepts or declines it, or the
organisers withdraw it. It lapses at its expiry, which is never stored as a
status: a pending invite past its expiry is read as expired, and can be
neither accepted nor declined. The token the mail carries is never stored,
only its SHA-256, so a copy of the database hands out no invites.

An email address is kept as it was typed, trimmed and lowered, and matched
whole against the addresses the forge has confirmed for an account, ignoring
case, so an address nobody confirmed never matches.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from enum import StrEnum

from forge.domain.errors import InvalidInvite, InviteExpired, WrongStatus
from forge.domain.registration import EMAIL_MAX
from forge.domain.roles import Role

TOKEN_BYTES = 32
LIFETIME = timedelta(days=14)
"""How long an invite stands unless the organiser says otherwise."""
LONGEST_LIFETIME = timedelta(days=90)
SHORTEST_LIFETIME = timedelta(hours=1)
ADDRESS = re.compile(
    r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
)
"""One plain address, the dot-atom form of RFC 5322, lowered."""


class Grant(StrEnum):
    """What an invite gives once accepted: a contestant's place in a contest,
    which is eligibility to register for it, or one of the organiser roles
    at the invite's scope."""

    CONTESTANT = "contestant"
    ADMIN = "admin"
    MANAGER = "manager"
    OBSERVER = "observer"

    @property
    def role(self) -> Role | None:
        """The organiser role it grants, or none for a contestant's place."""
        return None if self is Grant.CONTESTANT else Role(self.value)


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class MailStatus(StrEnum):
    """What became of an invite's mail: `waiting` until it is handed to the
    mail server, `sent` once it took it, `failed` when it could not be sent,
    the server refusing, not answering, or there being no address to send it
    to, and `off` where the deployment has no mail server. A mail still
    `waiting` minutes on was cut short by a restart, and is sent again from
    the invite."""

    WAITING = "waiting"
    SENT = "sent"
    FAILED = "failed"
    OFF = "off"


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> bytes:
    """What is stored of a token: its SHA-256."""
    return hashlib.sha256(token.encode()).digest()


def checked_email(value: str) -> str:
    """An address as an invite keeps it: trimmed and lowered. `InvalidInvite`
    for anything but one plain address: a name of the characters an address
    may hold unquoted, one `@`, and a domain of letters, digits and hyphens
    in two labels or more, at most `EMAIL_MAX` characters in all. A list of
    addresses, a display name or anything in brackets is refused, since the
    mail goes to exactly the address kept.
    """
    address = value.strip().lower()
    if len(address) > EMAIL_MAX or not ADDRESS.fullmatch(address):
        raise InvalidInvite("That is not an email address.")
    return address


def checked_target(username: str | None, email: str | None) -> tuple[str | None, str | None]:
    """The username or the address an invite names, exactly one of them."""
    named = username.strip() if username is not None else None
    if (named is None) == (email is None) or named == "":
        raise InvalidInvite("An invite names either a username or an email address.")
    return named, checked_email(email) if email is not None else None


def checked_lifetime(lifetime: timedelta | None) -> timedelta:
    """How long an invite stands: `LIFETIME` unless given, and from an hour to
    `LONGEST_LIFETIME`."""
    if lifetime is None:
        return LIFETIME
    if lifetime < SHORTEST_LIFETIME or lifetime > LONGEST_LIFETIME:
        raise InvalidInvite(f"An invite stands from an hour to {LONGEST_LIFETIME.days} days.")
    return lifetime


def refuse_closed(status: InviteStatus, expires_at: datetime, now: datetime, doing: str) -> None:
    """`WrongStatus` for an invite that is no longer pending, naming where it
    stands, and `InviteExpired` for one past its expiry."""
    if status is not InviteStatus.PENDING:
        raise WrongStatus(f"An invite that is {status} cannot be {doing}.", current=status)
    if now >= expires_at:
        raise InviteExpired("This invite has lapsed; ask the organisers for a new one.")
