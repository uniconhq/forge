"""What the organiser-path tests share: the org acme provisioned through the
poller with ada as its admin and its service account made, the built-in
workflow `unicon/classic@v1` public at the fake as bootstrap makes it, a
contest and a task made the way an organiser makes them, and a checked
`Organiser` for anyone. For the contestant's side: a session for anyone,
the contest's settings written as a test needs them, a task made and one
published by a save of its starter.
"""

from dataclasses import dataclass

import pytest

from forge.domain.content import Edit
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgName, TaskId
from forge.domain.roles import Role, Scope, task_id_of
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.runtime.setup import Setup
from forge.services import access, contests, orgs, publications, sessions, tasks
from forge.services.access import Organiser
from forge.services.publications import Published
from forge.testing import seed_classic, tick

ACME = OrgName("acme")
SPRING = ContestId("acme/spring")
SUM = TaskId("acme/spring/sum")


RUNNING = """name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: {visibility}
"""


async def signed_in(setup: Setup, fake: FakeForge, user_id: int) -> Session:
    """A session of the user's own, as a sign-in makes one."""
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx, user=fake.users[user_id], credential=fake.mint(user_id), ip=None, user_agent=None
        )


async def organiser(
    setup: Setup, fake: FakeForge, user_id: int, scope: Scope, role: Role = Role.OBSERVER
) -> Organiser:
    """The user signed in and checked, as the host's guard does, for `role`
    at `scope`.
    """
    return await access.organiser(setup, await signed_in(setup, fake, user_id), scope, role)


async def write_contest(fake: FakeForge, content: str, contest: ContestId = SPRING) -> None:
    """The contest's `contest.yaml` replaced by `content`, as an admin's save
    would leave it; acme/spring's unless another is named.
    """
    current = await fake.content.read_file(PLATFORM, contest, "contest.yaml")
    await fake.content.write_file(
        PLATFORM,
        contest,
        "contest.yaml",
        content.encode(),
        message="Settings",
        expected=current.token,
    )


async def make_task(setup: Setup, acme: Acme, name: str) -> TaskId:
    """Another task of acme/spring, made through the poller and not yet saved."""
    await tasks.create(setup, acme.ada, SPRING, name, title=name.title())
    await tick(setup, "provisioning")
    return task_id_of(Scope("acme", "spring", name))


async def publish(setup: Setup, acme: Acme, task: TaskId, extra: bytes = b"") -> Published:
    """The task published by ada's save of its `task.yaml` with `extra` after
    it.
    """
    head = await acme.fake.content.list_files(PLATFORM, task)
    current = await acme.fake.content.read_file(PLATFORM, task, "task.yaml")
    result = await publications.save(
        setup,
        acme.ada,
        task,
        {"task.yaml": Edit(current.content + extra, head.tokens["task.yaml"])},
    )
    assert isinstance(result, Published), result
    return result


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
