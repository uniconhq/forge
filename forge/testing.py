"""What a test needs to run the package: a Postgres of its own, migrated, the
in-memory forge with two users, a clock a test can move, and a setup and a
context over them. Load it as a pytest plugin, with `-p forge.testing` in the
pytest configuration or `pytest_plugins = ["forge.testing"]` in a conftest.
`UNICON_TEST_DATABASE_URL` names a server the tests may create databases on;
without it every test that needs one is skipped.

A test calls an action with `ctx` to run it inside the test's unit of work,
or with `setup` to have it open and commit one of its own. A dependant that
calls actions with neither, as the backend does, asks for `held_setup`, which
makes `setup` the one forge holds for the test and lets go of it afterwards.

A dependant imports `forge.api` and this module and nothing else of the
package, so this module also re-exports what a dependant's test needs to
arrange the fake: `FakeForge`, `Settings` for a settings override, and the
domain types a test builds, `OrgName`, `AsUser` and `Visibility`, and `Setup`
to type the setup a fixture hands over. `logged` reads back the records a
test caused, in the shape they are written. `tick` runs one tick of a poller
or a timed pass by name, so a test moves provisioning along the way the setup
would; `register_contestant` writes the row that makes someone a contestant,
for a test of what registration decides; and `seed_classic` puts the
built-in workflow `unicon/classic@v1` at the fake the way deploy's bootstrap
puts it at a real forge, from `CLASSIC`, a copy of that file,
`deploy/workflows/classic/workflow.yaml`. The package's own tests check the
two agree when the deploy repo is checked out beside this one.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from forge.db.migrations import upgrade_to_head
from forge.db.tables import Contestant
from forge.domain.clock import FakeClock
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, OrgName
from forge.domain.workflows import Visibility
from forge.forges.fake import FakeForge
from forge.log import JsonFormatter
from forge.runtime.context import Context
from forge.runtime.held import hold, release
from forge.runtime.setup import Setup
from forge.settings import Settings

__all__ = [
    "APP_URL",
    "CALLBACK",
    "CALLBACK_PATH",
    "CLASSIC",
    "FORGE_URL",
    "AsUser",
    "FakeClock",
    "FakeForge",
    "OrgName",
    "Settings",
    "Setup",
    "Visibility",
    "logged",
    "register_contestant",
    "seed_classic",
    "tick",
]

SERVER_URL_VARIABLE = "UNICON_TEST_DATABASE_URL"
APP_URL = "http://app.test"
FORGE_URL = "http://forge.test"
CALLBACK_PATH = "/api/v1/auth/callback"
CALLBACK = f"{APP_URL}{CALLBACK_PATH}"

_FORMATTER = JsonFormatter()

CLASSIC = b"""\
# unicon/classic@v1, the built-in workflow for a task judged by comparing
# output with an answer.
name: unicon/classic
version: v1

inputs:
  - id: submission
    type: code
  - id: testcases
    type: file[]
  - id: time_limit
    type: number
  - id: memory_limit
    type: number

steps:
  - id: compile
    use: unicon/compile@v1
    with:
      source: ${{ inputs.submission }}

  - id: run
    use: unicon/sandbox-run@v1
    foreach: ${{ inputs.testcases }}
    with:
      binary: ${{ steps.compile.output }}
      input: ${{ item.input }}
      time_limit: ${{ inputs.time_limit }}
      memory_limit: ${{ inputs.memory_limit }}

  - id: check
    use: unicon/diff-check@v1
    foreach: ${{ inputs.testcases }}
    with:
      actual: ${{ steps.run.stdout }}
      expected: ${{ item.answer }}

outputs:
  outcome: ${{ steps.check.outcome }}
  metrics:
    points: ${{ steps.check.points }}
"""


async def seed_classic(fake: FakeForge) -> None:
    """`unicon/classic@v1` public at the fake, as bootstrap makes it at a real
    forge, so a task's first save finds a workflow its organiser can read.
    """
    workflow = await fake.workflows.create_workflow(
        PLATFORM, "unicon", "classic", {"workflow.yaml": CLASSIC}, Visibility.PUBLIC
    )
    await fake.workflows.create_workflow_version(PLATFORM, workflow, "v1")


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """Every record named `event` that `caplog` holds, as the JSON object the
    package writes it as.
    """
    return [
        json.loads(_FORMATTER.format(record))
        for record in caplog.records
        if record.getMessage() == event
    ]


async def tick(setup: Setup, name: str) -> None:
    """One tick of the setup's poller or timed pass named `name`, such as
    `provisioning` or `drift.nightly`, on a unit of work of its own.
    `ValueError` naming the loops there are for any other name.
    """
    await setup.tick(name)


async def register_contestant(
    setup: Setup,
    contest: ContestId,
    user_id: int,
    *,
    status: str = "approved",
    time_extension: timedelta = timedelta(0),
) -> None:
    """Make the user a contestant of the contest, with `status` and their own
    time extension, committed at once.
    """
    async with setup.unit_of_work() as ctx:
        ctx.db.add(
            Contestant(
                contest_id=contest,
                user_id=user_id,
                status=status,
                registered_at=ctx.now,
                time_extension_seconds=int(time_extension.total_seconds()),
            )
        )


def _server_url() -> str:
    url = os.environ.get(SERVER_URL_VARIABLE)
    if not url:
        pytest.skip(f"{SERVER_URL_VARIABLE} is not set")
    return url


def _connect(url: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True)


@pytest.fixture
def database_url() -> Iterator[str]:
    """An empty database of its own, dropped when the test ends."""
    server_url = _server_url()
    name = f"unicon_test_{uuid.uuid4().hex[:12]}"
    with _connect(server_url) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    try:
        yield server_url.rsplit("/", 1)[0] + "/" + name
    finally:
        with _connect(server_url) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def migrated_database_url(database_url: str) -> str:
    upgrade_to_head(database_url)
    return database_url


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(
        database_url=migrated_database_url, public_url=APP_URL, forge_public_url=FORGE_URL
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock: FakeClock) -> FakeForge:
    """The in-memory forge with two users, `ada` (7) signed in and `bob` (8)."""
    forge = FakeForge(public_url=FORGE_URL, sign_in_redirect_uri=CALLBACK, clock=clock)
    forge.add_user(7, "ada", name="Ada Lovelace", email="ada@example.test")
    forge.add_user(8, "bob")
    forge.signed_in_user_id = 7
    return forge


@pytest.fixture
async def setup(settings: Settings, fake: FakeForge, clock: FakeClock) -> AsyncIterator[Setup]:
    built = Setup.build(settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock)
    try:
        yield built
    finally:
        await built.stop()


@pytest.fixture
def held_setup(setup: Setup) -> Iterator[Setup]:
    """`setup`, held as the process's own for the test, so an action called
    with no setup uses it. Let go when the test ends, passed or failed.
    """
    hold(setup)
    try:
        yield setup
    finally:
        release()


@pytest.fixture
async def ctx(setup: Setup) -> AsyncIterator[Context]:
    """One unit of work, committed when the test ends."""
    async with setup.unit_of_work() as context:
        yield context
