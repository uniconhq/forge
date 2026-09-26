"""The cache serves a cached read from memory, drops entries on writes, and
caches only the named reads while the flag is off.
"""

from forge.domain.ids import OrgName
from forge.domain.roles import Role, Scope
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge


async def test_a_cached_read_is_served_from_memory() -> None:
    fake = FakeForge()
    fake.add_user(7, "ada")
    cached = CachedForge(fake, enabled=True)

    await cached.find_user(7)
    await cached.find_user(7)

    assert len(fake.calls_to("find_user")) == 1


async def test_a_write_through_the_port_drops_its_entry() -> None:
    fake = FakeForge()
    fake.add_user(7, "ada")
    await fake.create_org(OrgName("acme"), description="Acme")
    cached = CachedForge(fake, enabled=True)

    assert await cached.roles_of(7) == ()
    await cached.grant_role(7, Scope("acme"), Role.ADMIN)

    assert len(await cached.roles_of(7)) == 1
    assert len(fake.calls_to("roles_of")) == 2


async def test_only_the_named_reads_are_cached_while_the_flag_is_off() -> None:
    fake = FakeForge()
    fake.add_user(7, "ada")
    cached = CachedForge(fake, enabled=False)

    await cached.find_user(7)
    await cached.find_user(7)
    await cached.roles_of(7)
    await cached.roles_of(7)

    assert len(fake.calls_to("find_user")) == 1
    assert len(fake.calls_to("roles_of")) == 2
