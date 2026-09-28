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
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from forge import actions
from forge.context import Context
from forge.db.engine import TransactionFactory
from forge.forges.fake import FakeForge
from forge.settings import Settings
from forge.setup import Setup, migrate

SERVER_URL_VARIABLE = "UNICON_TEST_DATABASE_URL"
APP_URL = "http://app.test"
FORGE_URL = "http://forge.test"
CALLBACK = f"{APP_URL}/api/v1/auth/callback"


class FakeClock:
    """A clock a test moves by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, by: timedelta) -> None:
        self._now += by

    def set(self, moment: datetime) -> None:
        self._now = moment


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
    migrate(database_url)
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
    built = Setup.build(settings, sign_in_redirect_uri=CALLBACK, forge=fake, clock=clock)
    try:
        yield built
    finally:
        await built.stop()


@pytest.fixture
def held_setup(setup: Setup) -> Iterator[Setup]:
    """`setup`, held as the process's own for the test, so an action called
    with no setup uses it. Let go when the test ends, passed or failed.
    """
    actions.hold(setup)
    try:
        yield setup
    finally:
        actions.release()


@pytest.fixture
async def ctx(setup: Setup) -> AsyncIterator[Context]:
    """One unit of work, committed when the test ends."""
    async with setup.unit_of_work() as context:
        yield context


@pytest.fixture
def factory(ctx: Context) -> TransactionFactory:
    """The factory for transactions of their own, as the background loops
    and the building blocks that write outside the caller's unit of work use
    it.
    """
    return ctx.transactions
