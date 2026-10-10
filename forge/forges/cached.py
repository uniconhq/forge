"""A small in-process cache around chosen reads of the port. Reads go to the
forge live; only the reads named here are cached, and a write through the
same area drops the matching entries in the same call. The cache is off behind
a flag except for the reads that grow with the forge rather than with the
page, which are cached either way.
"""

import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from forge.domain.identity import AsUser, Credential, User
from forge.domain.ids import OrgId
from forge.domain.names import OrgProfile
from forge.domain.roles import Role, RoleGrant, Scope
from forge.port import Forge
from forge.port.identity import AccountVisibility, IdentityPort, SignedIn
from forge.port.orgs import OrgPort

TTL_SECONDS = 60.0
MAX_ENTRIES = 10_000

Key = tuple[str, tuple[Any, ...]]


@dataclass
class Cache:
    """Entries expire after `ttl` seconds, and the oldest go first when the
    cache is full.
    """

    ttl: float = TTL_SECONDS
    max_entries: int = MAX_ENTRIES
    entries: OrderedDict[Key, tuple[float, Any]] = field(default_factory=OrderedDict)

    def get(self, operation: str, key: tuple[Any, ...]) -> tuple[bool, Any]:
        found = self.entries.get((operation, key))
        if found is None:
            return False, None
        expires_at, value = found
        if time.monotonic() >= expires_at:
            del self.entries[(operation, key)]
            return False, None
        return True, value

    def put(self, operation: str, key: tuple[Any, ...], value: Any) -> None:
        self.entries[(operation, key)] = (time.monotonic() + self.ttl, value)
        self.entries.move_to_end((operation, key))
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)

    def drop(self, operation: str, key: tuple[Any, ...] | None = None) -> None:
        if key is not None:
            self.entries.pop((operation, key), None)
            return
        for entry in [entry for entry in self.entries if entry[0] == operation]:
            del self.entries[entry]


class CachedIdentity:
    """The identity area with `find_user` cached: the names a leaderboard
    shows grow with the forge, so this read is cached whatever the flag says.
    """

    def __init__(self, inner: IdentityPort, cache: Cache) -> None:
        self._inner = inner
        self._cache = cache

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        return self._inner.sign_in_url(state=state, code_challenge=code_challenge, nonce=nonce)

    def sign_up_url(self) -> str | None:
        return self._inner.sign_up_url()

    def public_url(self) -> str:
        return self._inner.public_url()

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        return await self._inner.complete_sign_in(code=code, verifier=verifier)

    async def refresh_credential(self, credential: Credential) -> Credential:
        return await self._inner.refresh_credential(credential)

    async def user_of(self, credential: Credential) -> User:
        return await self._inner.user_of(credential)

    async def find_user(self, user_id: int) -> User:
        hit, value = self._cache.get("find_user", (user_id,))
        if hit:
            return cast(User, value)
        found = await self._inner.find_user(user_id)
        self._cache.put("find_user", (user_id,), found)
        return found

    async def verified_emails(self, user_id: int) -> tuple[str, ...]:
        return await self._inner.verified_emails(user_id)

    async def find_user_by_username(self, username: str) -> User:
        return await self._inner.find_user_by_username(username)

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        must_change_password: bool,
        visibility: AccountVisibility = "public",
    ) -> User:
        return await self._inner.create_user(
            username,
            email,
            password,
            must_change_password=must_change_password,
            visibility=visibility,
        )

    async def mint_token(
        self, username: str, password: str, *, name: str, scopes: Sequence[str]
    ) -> str:
        return await self._inner.mint_token(username, password, name=name, scopes=scopes)

    async def deactivate_user(self, user_id: int) -> None:
        await self._inner.deactivate_user(user_id)
        self._cache.drop("find_user", (user_id,))

    async def delete_user(self, user_id: int) -> None:
        await self._inner.delete_user(user_id)
        self._cache.drop("find_user", (user_id,))
        self._cache.drop("roles_of", (user_id,))
        self._cache.drop("holders_of")


class CachedOrgs:
    """The org area with the role reads cached behind the flag."""

    def __init__(self, inner: OrgPort, cache: Cache, *, enabled: bool) -> None:
        self._inner = inner
        self._cache = cache
        self._enabled = enabled

    async def name_taken(self, name: str) -> bool:
        return await self._inner.name_taken(name)

    async def create_org(self, name: OrgId, *, description: str) -> None:
        await self._inner.create_org(name, description=description)

    async def create_roles(self, name: OrgId) -> None:
        await self._inner.create_roles(name)

    async def create_thread_labels(self, name: OrgId) -> None:
        await self._inner.create_thread_labels(name)

    async def create_event_push(self, name: OrgId, *, url: str, secret: str) -> None:
        await self._inner.create_event_push(name, url=url, secret=secret)

    async def ensure_account_membership(self, name: OrgId, user_id: int) -> bool:
        return await self._inner.ensure_account_membership(name, user_id)

    async def remove_account_membership(self, name: OrgId, user_id: int) -> None:
        await self._inner.remove_account_membership(name, user_id)

    async def delete_org(self, name: OrgId) -> None:
        """Every role read is dropped: the org's roles go with it, and the
        cache does not know who held them.
        """
        await self._inner.delete_org(name)
        self._cache.drop("roles_of")
        self._cache.drop("holders_of")

    async def update_org(
        self, name: OrgId, *, description: str, display_name: str | None = None
    ) -> None:
        await self._inner.update_org(name, description=description, display_name=display_name)

    async def read_org(self, name: OrgId) -> OrgProfile:
        return await self._inner.read_org(name)

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        await self._inner.grant_role(user_id, scope, role)
        self._cache.drop("roles_of", (user_id,))
        self._cache.drop("holders_of", (scope, role))

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        await self._inner.revoke_role(user_id, scope, role)
        self._cache.drop("roles_of", (user_id,))
        self._cache.drop("holders_of", (scope, role))

    async def roles_of(self, as_: AsUser) -> tuple[RoleGrant, ...]:
        if not self._enabled:
            return await self._inner.roles_of(as_)
        hit, value = self._cache.get("roles_of", (as_.user_id,))
        if hit:
            return cast(tuple[RoleGrant, ...], value)
        found = await self._inner.roles_of(as_)
        self._cache.put("roles_of", (as_.user_id,), found)
        return found

    async def roles_of_user(self, user_id: int) -> tuple[RoleGrant, ...]:
        """Never cached: the rules on changing roles read it just before
        they write.
        """
        return await self._inner.roles_of_user(user_id)

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        if not self._enabled:
            return await self._inner.holders_of(scope, role)
        hit, value = self._cache.get("holders_of", (scope, role))
        if hit:
            return cast(tuple[User, ...], value)
        found = await self._inner.holders_of(scope, role)
        self._cache.put("holders_of", (scope, role), found)
        return found


class CachedForge:
    """The port with the identity and org areas wrapped in the cache and every
    other area passed through untouched.
    """

    def __init__(self, inner: Forge, *, enabled: bool, cache: Cache | None = None) -> None:
        shared = cache or Cache()
        self._inner = inner
        self.identity = CachedIdentity(inner.identity, shared)
        self.orgs = CachedOrgs(inner.orgs, shared, enabled=enabled)
        self.content = inner.content
        self.workspaces = inner.workspaces
        self.threads = inner.threads
        self.workflows = inner.workflows
        self.primitives = inner.primitives
        self.grading = inner.grading
        self.computes = inner.computes
        self.objects = inner.objects
        self.uploads = inner.uploads
        self.mail = inner.mail

    @property
    def name(self) -> str:
        return self._inner.name

    async def aclose(self) -> None:
        await self._inner.aclose()
