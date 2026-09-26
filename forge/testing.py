"""What a test needs to run the package: a Postgres of its own, migrated, the
in-memory forge with two users, a clock a test can move, and a runtime and
context over them. Load it as a pytest plugin, with `-p forge.testing` in the
pytest configuration or `pytest_plugins = ["forge.testing"]` in a conftest.
`UNICON_TEST_DATABASE_URL` names a server the tests may create databases on;
without it every test that needs one is skipped.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forge.context import Context
from forge.db.engine import SessionFactory
from forge.forges.fake import FakeForge
from forge.runtime import Runtime, migrate
from forge.settings import Settings

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
async def runtime(settings: Settings, fake: FakeForge, clock: FakeClock) -> AsyncIterator[Runtime]:
    built = Runtime.build(settings, sign_in_redirect_uri=CALLBACK, forge=fake, clock=clock)
    try:
        yield built
    finally:
        await built.stop()


@pytest.fixture
def factory(runtime: Runtime) -> SessionFactory:
    return runtime.sessions


@pytest.fixture
async def db(runtime: Runtime) -> AsyncIterator[AsyncSession]:
    """One unit of work, committed when the test ends."""
    async with runtime.sessions() as session:
        yield session
        await session.commit()


@pytest.fixture
def ctx(runtime: Runtime, db: AsyncSession) -> Context:
    return runtime.context(db)
