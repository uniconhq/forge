"""What the organiser-path tests share: the org acme provisioned through the
poller with ada as its admin and its service account made, the built-in
workflow `unicon/classic@v1` public at the fake as bootstrap makes it, a
contest and a task made the way an organiser makes them, and a checked
`Organiser` for anyone.
"""

from dataclasses import dataclass

import pytest

from forge.domain.ids import ContestId, OrgName, TaskId
from forge.domain.roles import Role, Scope
from forge.forges.fake import FakeForge
from forge.runtime.setup import Setup
from forge.services import access, contests, orgs, sessions, tasks
from forge.services.access import Organiser
from forge.testing import seed_classic, tick

ACME = OrgName("acme")
SPRING = ContestId("acme/spring")
SUM = TaskId("acme/spring/sum")


async def organiser(
    setup: Setup, fake: FakeForge, user_id: int, scope: Scope, role: Role = Role.OBSERVER
) -> Organiser:
    """The user signed in and checked, as the host's guard does, for `role`
    at `scope`.
    """
    async with setup.unit_of_work() as ctx:
        session = await sessions.create(
            ctx, user=fake.users[user_id], credential=fake.mint(user_id), ip=None, user_agent=None
        )
    return await access.organiser(setup, session, scope, role)


@dataclass(frozen=True, slots=True)
class Acme:
    """The provisioned org, with ada (7) its admin as an `Organiser`."""

    fake: FakeForge
    ada: Organiser


@pytest.fixture
async def acme(setup: Setup, fake: FakeForge) -> Acme:
    record = await orgs.create_by_operator(setup, ACME, description="Acme", admin_username="ada")
    assert record.status == "ready"
    await seed_classic(fake)
    ada = await organiser(setup, fake, 7, Scope("acme"), Role.MANAGER)
    fake.reset_calls()
    return Acme(fake, ada)


@pytest.fixture
async def spring(setup: Setup, acme: Acme) -> ContestId:
    """The contest acme/spring, made through the poller."""
    await contests.create(setup, acme.ada, ACME, "spring", title="Spring 2026")
    await tick(setup, "provisioning")
    acme.fake.reset_calls()
    return SPRING


@pytest.fixture
async def sum_task(setup: Setup, acme: Acme, spring: ContestId) -> TaskId:
    """The task acme/spring/sum, made through the poller and not yet saved."""
    await tasks.create(setup, acme.ada, spring, "sum", title="Sum of Two")
    await tick(setup, "provisioning")
    acme.fake.reset_calls()
    return SUM
