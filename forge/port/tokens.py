"""Where an implementation gets the credentials of the org accounts, the
service accounts that register and grade for one org each. The package holds
them; the implementation only asks.
"""

from typing import Protocol


class TokenSource(Protocol):
    async def forge_token(self, org: str) -> str:
        """The org account's credential at the git host. `NotFound` when the
        org has no account yet.
        """
        ...

    async def ci_token(self, org: str) -> str:
        """The org account's credential at the CI. `NotFound` when the org has
        no account yet.
        """
        ...
