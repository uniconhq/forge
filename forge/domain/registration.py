"""Registration: an entrant asking to take part in a contest, from the request
to the decision on it, and the ways a person qualifies to be in one. This
module holds an entrant's statuses, which of them an organiser may decide
from, and the rules in `contest.yaml` a person is checked against, each of
which refuses with an error of its own.

An entrant is `pending` until an organiser approves or rejects it, or until
the contest approves it on its own. Only a pending one is decided; `reopen`
takes a rejection back, leaving it pending again, and `remove` ends an
approved one and keeps what its members made. `pending` and `approved` are
the two that hold a place in the contest and make its members contestants of
it.

The rules come from the contest's `registration` block. The window is open
from `opens` and closed from `closes`, each counted from its own instant,
and always open without them. A person qualifies by one of the contest's
`ways`, any one enough, at the moment they join an entrant:

- an `invite` way admits someone holding an organiser's invite they have
  accepted, and asks nothing else;
- a `signup` way admits someone who meets every condition it gives: its
  `code`, typed; and one of their confirmed addresses that is at one of its
  `domains` and matches its `email_pattern`. One with no condition admits
  anyone with an account.

Every way that admits the person is found and the most lenient wins: one
approved straight away before one an organiser approves, and among equals
the first listed. What let them through is kept on their member row with
the way: `{"invited": true}`, `{"code": true}`, `{"email": <the address>}`.

An address counts only once the forge has confirmed it is the person's, or
anyone could claim a university's. With Forgejo the platform reads them as
its administrator with `GET /api/v1/user/emails` and `sudo` set to the
person's username, keeping the entries whose `verified` is true, the primary
address first (`adapters/git/forgejo/users.py`); Forgejo marks an address
verified when its owner follows the confirmation mail, and marks every one
verified where `REGISTER_EMAIL_CONFIRM` is off, so a deployment that signs
people up itself turns it on. A domain matches an address whose part after
its last `@` is that domain, ignoring case, and nothing below it:
`u.nus.edu` does not match `comp.u.nus.edu`. A pattern must match the whole
address, ignoring case; it is an organiser's, so it is matched under a time
limit, and one that runs past it lets nobody through rather than holding up
everyone else.
"""

import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import regex

from forge.domain.definitions import Approval, Registration, SignupWay
from forge.domain.errors import (
    DomainNotAllowed,
    InvalidExtension,
    InvalidReason,
    InviteRequired,
    RegistrationClosed,
    WrongInviteCode,
    WrongStatus,
)

REASON_MAX = 1000
LONGEST_EXTENSION = timedelta(days=365)
EMAIL_MAX = 254
PATTERN_SECONDS = 0.05
"""How long one address may take to match, far more than any sensible
pattern needs."""


class Status(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    REMOVED = "removed"


REGISTERED = (Status.PENDING, Status.APPROVED)
"""The statuses that hold a place and make an entrant's members
contestants."""


class WayKind(StrEnum):
    """How a member came into their entrant: by the contest's invite way, by
    one of its sign-ups, or put there by an organiser.
    """

    INVITE = "invite"
    SIGNUP = "signup"
    ORGANISER = "organiser"


@dataclass(frozen=True, slots=True)
class Qualified:
    """The way a person qualified by: its index in `ways` then, its kind,
    its name, if it has one, whether it approves straight away, and what let
    them through.
    """

    way: int | None
    kind: WayKind
    name: str | None
    approval: Approval
    eligibility: dict[str, Any]


ORGANISER = Qualified(None, WayKind.ORGANISER, None, Approval.AUTO, {})
"""How someone an organiser put in an entrant came in: an organiser chose
them, so nothing about them waits for an approval."""


def window_open(rules: Registration, now: datetime) -> bool:
    """Whether the registration window is open now."""
    opened = rules.opens is None or now >= rules.opens
    return opened and (rules.closes is None or now < rules.closes)


def refuse_closed(rules: Registration, now: datetime) -> None:
    """`RegistrationClosed` unless the window is open now."""
    if rules.opens is not None and now < rules.opens:
        raise RegistrationClosed(f"Registration opens at {rules.opens.isoformat()}.")
    if rules.closes is not None and now >= rules.closes:
        raise RegistrationClosed(f"Registration closed at {rules.closes.isoformat()}.")


def asks_address(rules: Registration) -> bool:
    """Whether some way takes only some addresses, so a person's confirmed
    ones are worth asking the forge for.
    """
    return any(way.signup is not None and way.signup.asks_address for way in rules.ways)


def qualify(
    rules: Registration, *, emails: Sequence[str], code: str | None, invited: bool
) -> Qualified:
    """The way the person qualifies by, the most lenient of every one that
    admits them, given their confirmed `emails`, the `code` they typed and
    whether they hold an organiser's invite they accepted. When none admits
    them, the refusal that helps them most: `RegistrationClosed` for a
    contest with no ways; `WrongInviteCode` when a sign-up would take them
    with its code; `DomainNotAllowed` when the sign-ups turn their addresses
    away; `InviteRequired` when only an invite would do.
    """
    if not rules.ways:
        raise RegistrationClosed("Nobody can register for this contest.")
    admitted = [found for found in _admitting(rules, emails, code, invited) if found is not None]
    if admitted:
        auto = [found for found in admitted if found.approval is Approval.AUTO]
        return (auto or admitted)[0]
    signups = [way.signup for way in rules.ways if way.signup is not None]
    if any(signup.code is not None and _takes_address(signup, emails) for signup in signups):
        if code is None or not code.strip():
            raise WrongInviteCode("This contest asks for a code.")
        raise WrongInviteCode("That is not the code this contest asks for.")
    if signups:
        raise DomainNotAllowed("This contest takes only some email addresses, and not yours.")
    raise InviteRequired("This contest takes only the people it invites.")


def asks_code(rules: Registration, *, emails: Sequence[str], invited: bool) -> bool:
    """Whether the person should be asked for a code: a sign-up with a code
    would let them in with it, and no way lets them in without one.
    """
    if any(found is not None for found in _admitting(rules, emails, None, invited)):
        return False
    return any(
        way.signup is not None
        and way.signup.code is not None
        and _takes_address(way.signup, emails)
        for way in rules.ways
    )


def _admitting(
    rules: Registration, emails: Sequence[str], code: str | None, invited: bool
) -> list[Qualified | None]:
    """For each way in order, how it admits the person, or none."""
    found: list[Qualified | None] = []
    for index, way in enumerate(rules.ways):
        if way.invite is not None:
            found.append(
                Qualified(index, WayKind.INVITE, None, way.approval, {"invited": True})
                if invited
                else None
            )
            continue
        assert way.signup is not None
        found.append(_signed_up(index, way.signup, emails, code))
    return found


def _signed_up(
    index: int, signup: SignupWay, emails: Sequence[str], code: str | None
) -> Qualified | None:
    eligibility: dict[str, Any] = {}
    if signup.code is not None:
        if code is None or not hmac.compare_digest(code.encode(), signup.code.encode()):
            return None
        eligibility["code"] = True
    if signup.asks_address:
        address = _address(signup, emails)
        if address is None:
            return None
        eligibility["email"] = address
    return Qualified(index, WayKind.SIGNUP, signup.name, signup.approval, eligibility)


def _takes_address(signup: SignupWay, emails: Sequence[str]) -> bool:
    """Whether the sign-up's conditions on an address hold: it gives none,
    or one of `emails` meets them.
    """
    return not signup.asks_address or _address(signup, emails) is not None


def _address(signup: SignupWay, emails: Sequence[str]) -> str | None:
    """The first of `emails` the sign-up takes: at one of its domains and
    matching its pattern, each when it gives one; none when no address does.
    """
    for email in emails:
        if signup.domains is not None and not any(
            domain_matches(domain, email) for domain in signup.domains
        ):
            continue
        if signup.email_pattern is not None and not matches(signup.email_pattern, email):
            continue
        return email
    return None


def domain_matches(domain: str, email: str) -> bool:
    """Whether the address is at the domain: its part after the last `@` is
    the domain, ignoring case.
    """
    if len(email) > EMAIL_MAX or "@" not in email:
        return False
    return email.rpartition("@")[2].lower() == domain.lower()


def matches(pattern: str, email: str) -> bool:
    """Whether the whole address matches, ignoring case, within the time
    limit; an address too long to be one, or a match that runs out of time,
    does not.
    """
    if len(email) > EMAIL_MAX:
        return False
    try:
        return (
            regex.fullmatch(pattern, email, regex.IGNORECASE, timeout=PATTERN_SECONDS) is not None
        )
    except TimeoutError:
        return False


def refuse_undecidable(status: Status, allowed: tuple[Status, ...], doing: str) -> None:
    """`WrongStatus` naming the status, unless it is one `doing` is allowed
    from.
    """
    if status not in allowed:
        raise WrongStatus(f"An entrant that is {status} cannot be {doing}.", current=status)


def checked_reason(reason: str) -> str:
    """The reason for a rejection, trimmed. `InvalidReason` when it is empty
    or longer than the limit.
    """
    trimmed = reason.strip()
    if not trimmed:
        raise InvalidReason("Say why, so the entrant can read it.")
    if len(trimmed) > REASON_MAX:
        raise InvalidReason(f"A reason is at most {REASON_MAX} characters.")
    return trimmed


def checked_tasks(tasks: Sequence[str] | None, known: Sequence[str]) -> tuple[str, ...] | None:
    """The tasks an extension is for, each named once, or none for every
    task. `InvalidExtension` for an empty list or a task the contest does not
    list.
    """
    if tasks is None:
        return None
    if not tasks:
        raise InvalidExtension("Name the tasks the extension is for, or none for every task.")
    for task in tasks:
        if task not in known:
            raise InvalidExtension(f"{task} is not a task of the contest.")
    return tuple(dict.fromkeys(tasks))


def checked_extension(extension: timedelta) -> timedelta:
    """A time extension, whole seconds. `InvalidExtension` below nothing or
    above a year.
    """
    if extension < timedelta(0):
        raise InvalidExtension("An extension cannot take time away.")
    if extension > LONGEST_EXTENSION:
        raise InvalidExtension("An extension is at most a year.")
    return timedelta(seconds=int(extension.total_seconds()))
