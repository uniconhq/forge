"""The in-memory forge's primitives behave as a real forge does: a
primitive's declaration is read as the organiser at its version.
"""

import pytest

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import PrimitiveId
from forge.forges.fake import FakeForge
from forge.testing import PRIMITIVES, seed_primitives


async def test_a_declaration_is_read_as_the_organiser_at_its_version(fake: FakeForge) -> None:
    await seed_primitives(fake)
    fake.primitives.add("scorer", {"v1": b"one", "v2": b"two"})
    ada = AsUser(7, fake.mint(7))

    assert (
        await fake.primitives.read_declaration(ada, PrimitiveId("compile"), "v1")
        == (PRIMITIVES["compile"])
    )
    assert await fake.primitives.read_declaration(ada, PrimitiveId("scorer"), "v1") == b"one"
    assert fake.calls_to("read_declaration")[0].identity == ada
    with pytest.raises(NotFound):
        await fake.primitives.read_declaration(PLATFORM, PrimitiveId("compile"), "v9")
