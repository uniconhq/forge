"""A registration: a person asking to join a contest, from the request to the
decision on it. This module holds its statuses, which of them an organiser
may decide from, and the rules in `contest.yaml` a request is checked
against, each of which refuses with an error of its own.

A registration is `pending` until an organiser approves or rejects it, or
until the contest approves it on its own. Only a pending one is decided;
`reopen` takes a rejection back, leaving it pending again, and `remove` ends
an approved one and keeps what the contestant made. `pending`
and `approved` are the two that hold a place in the contest and make the
person a contestant of it.

The rules come from the contest's `registration` block. The window is open
from `opens` and closed from `closes`, each counted from its own instant,
and always open without them. An invite-only contest needs an accepted
invite; a contest with a `code` needs that code; a contest with an
`email_pattern` needs one of the person's confirmed email addresses to match
the whole pattern, ignoring case. The pattern is an organiser's, so it is
matched under a time limit, and one that runs past it lets nobody through
rather than holding up everyone else. What let a registration through is kept
on it as its eligibility.
"""

import hmac
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import regex

from forge.domain.definitions import Registration
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
"""The statuses that hold a place and make the person a contestant."""


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


def eligibility(
    rules: Registration, *, emails: Sequence[str], invite_code: str | None, invited: bool
) -> dict[str, Any]:
    """What lets the person through, to keep on the registration: the invite,
    the code and the confirmed address that matched, each only when the
    contest asks for it. The refusal of the first rule they break otherwise.
    """
    outcome: dict[str, Any] = {}
    if rules.invite_only:
        if not invited:
            raise InviteRequired("This contest takes only the people it invites.")
        outcome["invited"] = True
    expected = rules.code
    if expected is not None:
        if invite_code is None or not hmac.compare_digest(invite_code.encode(), expected.encode()):
            raise WrongInviteCode("That is not the code this contest asks for.")
        outcome["invite_code"] = True
    pattern = rules.email_pattern
    if pattern is not None:
        matched = next((email for email in emails if matches(pattern, email)), None)
        if matched is None:
            raise DomainNotAllowed("This contest takes only some email addresses, and not yours.")
        outcome["email"] = matched
    return outcome


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
        raise WrongStatus(f"A registration that is {status} cannot be {doing}.", current=status)


def checked_reason(reason: str) -> str:
    """The reason for a rejection, trimmed. `InvalidReason` when it is empty
    or longer than the limit.
    """
    trimmed = reason.strip()
    if not trimmed:
        raise InvalidReason("Say why, so the person can read it.")
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
