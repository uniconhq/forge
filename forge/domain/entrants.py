"""An entrant: the one kind of entry a contest has, one or more people who
enter it together. This module holds a member's statuses, the rules an
entrant's name and size are checked against, when its roster locks, and when
it is complete and approved on its own.

A person is a `member` of an entrant once in it, and `left` once they leave,
are taken out or are moved to another; a person is a member of at most one
entrant in a contest, and their rows of the ones they left are kept.

An entrant of a contest whose entrants may be more than one person has a
name, trimmed, from one to `NAME_MAX` characters, and no two entrants of a
contest share one, ignoring case. An entrant of a contest whose entrants are
one person each has none and is shown under its member's username.

An entrant's roster locks at the contest's start, or at the entrant's first
submission when the contest says `entrants.locks: first_submission`, and in
any case once the contest has ended or is archived; from then on only
organisers change who is in it. An entrant is complete once it has
`entrants.fewest` members, and is approved straight away once it is complete
and every member came in by a way that approves straight away.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from forge.domain.definitions import Approval, ContestDefinition, Locks, State
from forge.domain.errors import EntrantFull, EntrantIncomplete, InvalidEntrantName, RosterLocked

NAME_MAX = 60
DELETED_USER = "Deleted user"
"""What a member whose account is gone is shown as, in place of a username."""


class MemberStatus(StrEnum):
    MEMBER = "member"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class EntrantRef:
    """An entrant by its id and the name it is shown under: its own, or its
    member's username when it has none.
    """

    id: uuid.UUID
    name: str


def checked_name(name: str) -> str:
    """An entrant's name, trimmed. `InvalidEntrantName` when it is empty, too
    long, or holds a character that is not printable.
    """
    trimmed = name.strip()
    if not trimmed:
        raise InvalidEntrantName("Give the team a name.")
    if len(trimmed) > NAME_MAX:
        raise InvalidEntrantName(
            f"A team's name can be at most {NAME_MAX} characters. Choose a shorter one."
        )
    if not trimmed.isprintable():
        raise InvalidEntrantName(
            "A team's name can hold only characters that can be printed. Remove the others."
        )
    return trimmed


def refuse_full(taken: int, most: int) -> None:
    """`EntrantFull`, naming the limit, when an entrant whose members and
    pending invites come to `taken` takes no one more under the contest's
    `most`.
    """
    if taken >= most:
        raise EntrantFull(
            f"A team in this contest can have at most {most} people, counting the invites "
            "still waiting, and this one is full.",
            limit=most,
        )


def locked(settings: ContestDefinition, now: datetime, *, submitted: bool) -> bool:
    """Whether an entrant's roster is locked now: the contest has ended or is
    archived, or it has passed the point its `entrants.locks` names, the
    start or the entrant's first submission.
    """
    if settings.state is State.ARCHIVED or now >= settings.end:
        return True
    if settings.entrants.locks is Locks.START:
        return now >= settings.start
    return submitted


def refuse_locked(settings: ContestDefinition, now: datetime, *, submitted: bool) -> None:
    """`RosterLocked` once the entrant's roster has locked."""
    if locked(settings, now, submitted=submitted):
        raise RosterLocked(
            "Who is in this team is fixed now. Only the contest's organisers can change it."
        )


def complete(settings: ContestDefinition, members: int) -> bool:
    """Whether an entrant of `members` people has as many as the contest's
    `entrants.fewest` asks.
    """
    return members >= settings.entrants.fewest


def refuse_incomplete(settings: ContestDefinition, members: int) -> None:
    """`EntrantIncomplete`, naming how many it needs, for an entrant short
    of the contest's `entrants.fewest`.
    """
    fewest = settings.entrants.fewest
    if members < fewest:
        raise EntrantIncomplete(
            f"A team in this contest needs at least {fewest} "
            f"{'member' if fewest == 1 else 'members'}, and this one has {members}. Invite "
            "more people before it can take part.",
            fewest=fewest,
            members=members,
        )


def approved_on_its_own(settings: ContestDefinition, approvals: Iterable[Approval]) -> bool:
    """Whether an entrant whose members came in by ways of `approvals` is
    approved straight away: it is complete, and every one of them approves
    straight away.
    """
    found = list(approvals)
    return complete(settings, len(found)) and all(approval is Approval.AUTO for approval in found)
