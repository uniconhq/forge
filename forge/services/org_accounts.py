"""The org accounts: the service accounts that register and grade for one
org each. The package holds their credentials and hands them to the
implementation on request. No org account exists until orgs are provisioned,
so every lookup is `NotFound`.
"""

from forge.domain.errors import NotFound


class OrgAccountTokens:
    async def forge_token(self, org: str) -> str:
        raise NotFound(f"org {org} has no org account yet")

    async def ci_token(self, org: str) -> str:
        raise NotFound(f"org {org} has no org account yet")
