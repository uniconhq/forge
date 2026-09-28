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
arrange the fake: `FakeForge`, and the domain types a test builds, `OrgName`,
`AsUser` and `Visibility`. `logged` reads back the records a test caused, in
the shape they are written.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest

from forge.db.migrations import upgrade_to_head
from forge.domain.clock import FakeClock
from forge.domain.identity import AsUser
from forge.domain.ids import OrgName
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
    "FORGE_URL",
    "AsUser",
    "FakeClock",
    "FakeForge",
    "OrgName",
    "Visibility",
    "logged",
]

SERVER_URL_VARIABLE = "UNICON_TEST_DATABASE_URL"
APP_URL = "http://app.test"
FORGE_URL = "http://forge.test"
CALLBACK_PATH = "/api/v1/auth/callback"
CALLBACK = f"{APP_URL}{CALLBACK_PATH}"

_FORMATTER = JsonFormatter()


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """Every record named `event` that `caplog` holds, as the JSON object the
    package writes it as.
    """
    return [
        json.loads(_FORMATTER.format(record))
        for record in caplog.records
        if record.getMessage() == event
    ]


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
