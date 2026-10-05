"""A team: people in one contest who enter it together and count as one
contestant. This module holds a member's statuses, the rules a team's name
and size are checked against, and what refuses a change of membership.

A person is `invited` when a team's leader or an organiser asked them in and
they have not answered, `requested` when they asked to join and the leader
has not answered, `member` once in, and `left` once they leave, are removed
or are moved to another team. An unanswered invitation or request that is
declined, withdrawn or overtaken is removed rather than kept, since it
granted nothing. A person is a member of at most one team in a contest.

A team's name is how people find it, trimmed, from one to `NAME_MAX`
characters, and no two teams of a contest share one, ignoring case.
"""

from enum import StrEnum

from forge.domain.errors import InvalidTeamName, TeamFull

NAME_MAX = 60


class MemberStatus(StrEnum):
    INVITED = "invited"
    REQUESTED = "requested"
    MEMBER = "member"
    LEFT = "left"


PENDING = (MemberStatus.INVITED, MemberStatus.REQUESTED)
"""The statuses of someone asked in or asking, who holds nothing yet."""


def checked_name(name: str) -> str:
    """A team's name, trimmed. `InvalidTeamName` when it is empty, too long,
    or holds a character that is not printable.
    """
    trimmed = name.strip()
    if not trimmed:
        raise InvalidTeamName("A team needs a name.")
    if len(trimmed) > NAME_MAX:
        raise InvalidTeamName(f"A team's name is at most {NAME_MAX} characters.")
    if not trimmed.isprintable():
        raise InvalidTeamName("A team's name holds only printable characters.")
    return trimmed


def refuse_full(members: int, max_size: int) -> None:
    """`TeamFull`, naming the limit, when a team of `members` takes no one
    more under the contest's `max_size`.
    """
    if members >= max_size:
        raise TeamFull(f"A team in this contest holds at most {max_size}.", limit=max_size)
