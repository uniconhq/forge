"""A small in-process cache around the port, chosen per read. Reads go to the
forge live; only the reads named here are cached, and a write through the port
drops the matching entries in the same call. The cache is off behind a flag
except for the two reads that grow with the forge rather than with the page:
the names a leaderboard shows and the door's answer for a machine login.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from forge.domain.identity import User
from forge.domain.roles import Role, RoleGrant, Scope
from forge.port import Forge

ALWAYS_CACHED = frozenset({"find_user"})
TTL_SECONDS = 60.0


@dataclass
class Cache:
    ttl: float = TTL_SECONDS
    entries: dict[tuple[str, tuple[Any, ...]], tuple[float, Any]] = field(default_factory=dict)

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

    def drop(self, operation: str, key: tuple[Any, ...] | None = None) -> None:
        for entry in [
            entry
            for entry in self.entries
            if entry[0] == operation and (key is None or entry[1] == key)
        ]:
            del self.entries[entry]


class CachedForge:
    """The port with cached reads. Every operation not named here passes
    straight through to the inner implementation.
    """

    def __init__(self, inner: Forge, *, enabled: bool, cache: Cache | None = None) -> None:
        self._inner = inner
        self._enabled = enabled
        self._cache = cache or Cache()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    async def find_user(self, user_id: int) -> User:
        found = await self._cached("find_user", (user_id,), lambda: self._inner.find_user(user_id))
        return cast(User, found)

    async def roles_of(self, user_id: int) -> tuple[RoleGrant, ...]:
        found = await self._cached("roles_of", (user_id,), lambda: self._inner.roles_of(user_id))
        return cast(tuple[RoleGrant, ...], found)

    async def holders_of(self, scope: Scope, role: Role) -> tuple[User, ...]:
        found = await self._cached(
            "holders_of", (scope, role), lambda: self._inner.holders_of(scope, role)
        )
        return cast(tuple[User, ...], found)

    async def deactivate_user(self, user_id: int) -> None:
        await self._inner.deactivate_user(user_id)
        self._cache.drop("find_user", (user_id,))

    async def delete_user(self, user_id: int) -> None:
        await self._inner.delete_user(user_id)
        self._cache.drop("find_user", (user_id,))
        self._cache.drop("roles_of", (user_id,))
        self._cache.drop("holders_of")

    async def grant_role(self, user_id: int, scope: Scope, role: Role) -> None:
        await self._inner.grant_role(user_id, scope, role)
        self._cache.drop("roles_of", (user_id,))
        self._cache.drop("holders_of", (scope, role))

    async def revoke_role(self, user_id: int, scope: Scope, role: Role) -> None:
        await self._inner.revoke_role(user_id, scope, role)
        self._cache.drop("roles_of", (user_id,))
        self._cache.drop("holders_of", (scope, role))

    async def _cached(
        self, operation: str, key: tuple[Any, ...], read: Callable[[], Awaitable[Any]]
    ) -> Any:
        if not self._enabled and operation not in ALWAYS_CACHED:
            return await read()
        hit, value = self._cache.get(operation, key)
        if hit:
            return value
        value = await read()
        self._cache.put(operation, key, value)
        return value
