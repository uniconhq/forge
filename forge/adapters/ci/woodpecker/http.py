"""The HTTP client Woodpecker is called through: the shared retrying
client, for calls made as the CI's own administrator or as an org account,
each signed with its token at the CI.
"""

from dataclasses import dataclass

from forge.adapters import http
from forge.adapters.ci.woodpecker.ci_state import read_state
from forge.adapters.http import json_of, new_client, segment
from forge.domain.errors import Forbidden
from forge.domain.identity import AsOrgAccount

__all__ = [
    "CI_ADMIN",
    "Caller",
    "CiAdmin",
    "Http",
    "WoodpeckerAuth",
    "json_of",
    "new_client",
    "segment",
]


@dataclass(frozen=True, slots=True)
class CiAdmin:
    """The CI's own administrator, which creates users and agents at the CI.
    Only this adapter acts as it, so it is no identity of the port's.
    """


CI_ADMIN = CiAdmin()

Caller = CiAdmin | AsOrgAccount
"""Whom a call to the CI is made as."""

Http = http.Http[Caller]


class WoodpeckerAuth:
    def __init__(self, admin_token: str) -> None:
        self._admin_token = admin_token

    async def header(self, as_: Caller) -> str:
        match as_:
            case CiAdmin():
                return f"Bearer {self._admin_token}"
            case AsOrgAccount(ci_state=state):
                return f"Bearer {read_state(state).token}"
        raise Forbidden("only the CI administrator and org accounts reach the CI")
