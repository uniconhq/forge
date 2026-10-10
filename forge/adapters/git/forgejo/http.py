"""The HTTP client Forgejo is called through: the shared retrying client,
for calls made as one of the port's identities, signed the way Forgejo reads
each.
"""

from forge.adapters import http
from forge.adapters.http import (
    MAX_PAGES,
    PAGE_SIZE,
    file_path,
    json_of,
    list_of,
    new_client,
    refusal,
    segment,
)
from forge.domain.errors import Forbidden
from forge.domain.identity import AsOrgAccount, AsUser, Identity, Platform

__all__ = [
    "MAX_PAGES",
    "PAGE_SIZE",
    "ForgejoAuth",
    "Http",
    "file_path",
    "json_of",
    "list_of",
    "new_client",
    "refusal",
    "segment",
]

Http = http.Http[Identity]


class ForgejoAuth:
    def __init__(self, admin_token: str) -> None:
        self._admin_token = admin_token

    async def header(self, as_: Identity) -> str:
        match as_:
            case Platform():
                return f"token {self._admin_token}"
            case AsUser(credential=credential):
                return f"Bearer {credential.access}"
            case AsOrgAccount(forge_token=token):
                return f"token {token}"
        raise Forbidden("unknown identity")
