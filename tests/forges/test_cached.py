"""The cache serves a cached read from memory, drops entries on writes, and
caches only the reads that grow with the forge while the flag is off.
"""

from forge.adapters.cached import Cache, CachedForge
from forge.adapters.git.fake import FakeForge
from forge.domain.identity import AsUser
from forge.domain.ids import OrgId
from forge.domain.roles import Role, Scope


def _ada(fake: FakeForge) -> AsUser:
    return AsUser(7, fake.mint(7))


async def test_a_cached_read_is_served_from_memory(fake: FakeForge) -> None:
    cached = CachedForge(fake, enabled=True)

    await cached.identity.find_user(7)
    await cached.identity.find_user(7)

    assert len(fake.calls_to("find_user")) == 1


async def test_a_write_through_the_area_drops_its_entry(fake: FakeForge) -> None:
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    cached = CachedForge(fake, enabled=True)

    ada = _ada(fake)
    assert await cached.orgs.roles_of(ada) == ()
    await cached.orgs.grant_role(7, Scope("acme"), Role.ADMIN)

    assert len(await cached.orgs.roles_of(ada)) == 1
    assert len(fake.calls_to("roles_of")) == 2


async def test_only_the_named_reads_are_cached_while_the_flag_is_off(fake: FakeForge) -> None:
    cached = CachedForge(fake, enabled=False)

    ada = _ada(fake)
    await cached.identity.find_user(7)
    await cached.identity.find_user(7)
    await cached.orgs.roles_of(ada)
    await cached.orgs.roles_of(ada)

    assert len(fake.calls_to("find_user")) == 1
    assert len(fake.calls_to("roles_of")) == 2


def test_the_cache_is_bounded() -> None:
    cache = Cache(max_entries=2)
    for user_id in (1, 2, 3):
        cache.put("find_user", (user_id,), user_id)
    assert cache.get("find_user", (1,)) == (False, None)
    assert cache.get("find_user", (3,)) == (True, 3)
