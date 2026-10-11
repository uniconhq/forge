"""What the plugin gives a dependant beyond its fixtures: an entrant and its
members written in one call, and the settings type for an override.
"""

from datetime import timedelta

from sqlalchemy import select

import forge.testing
from forge.db.tables import Entrant, EntrantMember
from forge.domain.ids import ContestId
from forge.runtime.setup import Setup
from forge.settings import Settings
from forge.testing import register_entrant


def test_the_settings_type_is_the_packages() -> None:
    assert forge.testing.Settings is Settings


async def test_an_entrant_is_registered_with_a_status_an_extension_and_its_members(
    setup: Setup,
) -> None:
    alone = await register_entrant(setup, ContestId("acme/spring"), 8)
    together = await register_entrant(
        setup,
        ContestId("acme/autumn"),
        8,
        9,
        name="Adders",
        status="pending",
        time_extension=timedelta(minutes=5),
    )

    async with setup.unit_of_work() as ctx:
        rows = await ctx.db.scalars(select(Entrant).order_by(Entrant.contest_id))
        found = [(row.id, row.name, row.status, row.time_extension_seconds) for row in rows]
        members = await ctx.db.scalars(
            select(EntrantMember.user_id).where(EntrantMember.entrant_id == together)
        )
        held = sorted(members)
    assert found == [(together, "Adders", "pending", 300), (alone, None, "approved", 0)]
    assert held == [8, 9]
