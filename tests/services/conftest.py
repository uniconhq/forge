"""What the organiser-path tests share: the org acme made with ada as its
admin and its service account made, the built-in
workflow `unicon/classic@v3` public at the fake as bootstrap makes it, a
contest and a task made the way an organiser makes them, a checked
`Organiser` for anyone, and what the fake holds, to compare before and after
a create that failed. For the contestant's side: a session for anyone,
the contest's settings written as a test needs them, a task made and one
published by a save of its starter, its save's runs checked, a contestant
entered alone in a running contest, and a file uploaded as a browser
uploads one. A test that moves
the clock on by more than a credential lives and then calls an action as
someone passes the guard first, `identity.current`, as every request does.
"""

import copy
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import update

from forge.adapters.fakes import FakeForge
from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgId, TaskId
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role, Scope, task_id_of
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import (
    access,
    contests,
    entrants,
    identity,
    orgs,
    pipelines,
    sessions,
    task_save,
    tasks,
    uploads,
)
from forge.services.access import Organiser
from forge.services.task_save import Published
from forge.testing import seed_classic

ACME = OrgId("acme")
SPRING = ContestId("acme/spring")
SUM = TaskId("acme/spring/sum")


RUNNING = """name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: {visibility}
tasks:
  - id: sum
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


def forge_state(fake: FakeForge) -> dict[str, Any]:
    """Everything a create could make or change at the fake forge and CI:
    each org with its roles, labels, service account's place and event push,
    the users, the files of every place, the accounts' credentials at the
    forge and the CI, the CI's users and the tasks it has activated.
    """
    state = fake.state
    return copy.deepcopy(
        {
            "orgs": {
                name: (org.roles, org.labels, org.account_members, org.event_push)
                for name, org in state.orgs.items()
            },
            "users": state.users,
            "places": {key: repo.files for key, repo in state.repos.items()},
            "tokens": state.tokens,
            "ci_users": fake.ci.ci_users,
            "ci_tokens": fake.ci.ci_tokens,
            "activated": fake.ci.activated,
        }
    )


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
    """Another task of acme/spring, made and not yet saved."""
    await tasks.create(setup, acme.ada, SPRING, name, title=name.title())
    return task_id_of(Scope("acme", "spring", name))


async def publish(setup: Setup, acme: Acme, task: TaskId, extra: bytes = b"") -> Published:
    """The task published by ada's save of its `task.yaml` with `extra` after
    it, confirmed, since a save asks first once the contest has started, and
    the runs its save grades checked.
    """
    head = await acme.fake.content.list_files(PLATFORM, task)
    current = await acme.fake.content.read_file(PLATFORM, task, "task.yaml")
    result = await task_save.save(
        setup,
        acme.ada,
        task,
        {"task.yaml": Edit(current.content + extra, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(result, Published), result
    await checked(setup, task)
    return result


async def checked(setup: Setup, task: TaskId) -> None:
    """Each save row of `task` ended `done` with every run accepted, as its
    save pipeline would, and the publications it checks taken over, a
    change to how the task grades grading its submissions again."""
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.task_id == task, Grading.pipeline == "save")
            .values(status=GradingStatus.DONE, result={"schema_version": 6, "runs": []})
        )
    async with setup.unit_of_work() as ctx:
        await pipelines.take_over(ctx, task)
    await setup.settle()


@dataclass(frozen=True, slots=True)
class Acme:
    """The org, with ada (7) its admin as an `Organiser`."""

    fake: FakeForge
    ada: Organiser


@pytest.fixture
async def acme(setup: Setup, fake: FakeForge) -> Acme:
    await orgs.create_by_operator(setup, ACME, description="Acme", admin_username="ada")
    await seed_classic(fake)
    ada = await organiser(setup, fake, 7, Scope("acme"), Role.MANAGER)
    fake.reset_calls()
    return Acme(fake, ada)


@pytest.fixture
async def spring(setup: Setup, acme: Acme) -> ContestId:
    """The contest acme/spring."""
    await contests.create(setup, acme.ada, ACME, "spring", title="Spring 2026")
    acme.fake.reset_calls()
    return SPRING


@pytest.fixture
async def sum_task(setup: Setup, acme: Acme, spring: ContestId) -> TaskId:
    """The task acme/spring/sum, made and not yet saved."""
    await tasks.create(setup, acme.ada, spring, "sum", title="Sum of Two")
    acme.fake.reset_calls()
    return SUM


@dataclass(frozen=True, slots=True)
class Entered:
    """bob (8), alone in an approved entrant of acme/spring, running and for
    everyone, with the task sum published. Its place to submit it is made at
    his first submit.
    """

    session: Session
    task: TaskId
    entrant: uuid.UUID


@pytest.fixture
async def entered(setup: Setup, acme: Acme, sum_task: TaskId) -> Entered:
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    await publish(setup, acme, sum_task)
    bob = await signed_in(setup, acme.fake, 8)
    entrant = await entrants.register(setup, bob, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await entrants.approve(setup, manager, SPRING, entrant.id)
    acme.fake.reset_calls()
    return Entered(bob, sum_task, entrant.id)


async def upload(
    setup: Setup,
    fake: FakeForge,
    session: Session,
    task: TaskId,
    content: bytes,
    *,
    input: str = "code",
    filename: str = "main.py",
) -> uploads.Upload:
    """A file uploaded the way a browser does: a slot, the bytes sent
    through the door, and the upload completed, each request passing the
    host's guard first, which keeps the session's credential fresh.
    """
    await identity.current(session.id, setup=setup)
    slot = await uploads.slot(
        setup,
        session,
        task,
        input=input,
        filename=filename,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    if not slot.ready:
        door = await uploads.door(setup, session, slot.id, length=len(content))
        fake.uploads.send(door.path, door.authorization, content)
    return await uploads.complete(setup, session, task, slot.id)


def segment(entrant: uuid.UUID) -> str:
    """How an entrant's workspace is named at the forge, between a contest
    and the word that ends a repository's name.
    """
    return EntrantOwner(entrant).segment
