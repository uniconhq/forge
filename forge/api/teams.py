"""Teams: a contestant making, joining, leaving and, as its leader, running
one; organisers making, deleting and mending them; and what each reads.
"""

from forge.services.teams import (
    Listed,
    Member,
    Mine,
    Team,
    approve,
    cancel,
    create,
    every,
    invite,
    leave,
    listed,
    mine,
    organise_create,
    organise_delete,
    organise_lead,
    organise_move,
    organise_remove,
    remove,
    request,
)

__all__ = [
    "Listed",
    "Member",
    "Mine",
    "Team",
    "approve",
    "cancel",
    "create",
    "every",
    "invite",
    "leave",
    "listed",
    "mine",
    "organise_create",
    "organise_delete",
    "organise_lead",
    "organise_move",
    "organise_remove",
    "remove",
    "request",
]
