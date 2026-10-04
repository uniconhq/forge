"""What the plugin gives a dependant beyond its fixtures: a contestant row
written in one call, and the settings type for an override.
"""

from datetime import timedelta

from sqlalchemy import select

import forge.testing
from forge.db.tables import Contestant
from forge.domain.ids import ContestId
from forge.runtime.setup import Setup
from forge.settings import Settings
from forge.testing import register_contestant


def test_the_settings_type_is_the_packages() -> None:
    assert forge.testing.Settings is Settings


async def test_a_contestant_is_registered_with_a_status_and_an_extension(setup: Setup) -> None:
    await register_contestant(setup, ContestId("acme/spring"), 8)
    await register_contestant(
        setup, ContestId("acme/autumn"), 8, status="pending", time_extension=timedelta(minutes=5)
    )

    async with setup.unit_of_work() as ctx:
        rows = (await ctx.db.execute(select(Contestant).order_by(Contestant.contest_id))).scalars()
        found = [(row.contest_id, row.status, row.time_extension_seconds) for row in rows]
    assert found == [("acme/autumn", "pending", 300), ("acme/spring", "approved", 0)]
