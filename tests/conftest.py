"""Fixtures shared by every test. A real Postgres for the services and the
migrations: `UNICON_TEST_DATABASE_URL` names a server these tests may create
databases on, and without it they are skipped. The forge is the in-memory
fake.
"""

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping

import psycopg
import pytest

from forge.db.engine import SessionFactory, new_engine, new_session_factory
from forge.db.migrations import upgrade_to_head
from forge.forges.fake import FakeForge
from forge.settings import Settings

SERVER_URL_VARIABLE = "UNICON_TEST_DATABASE_URL"
APP_URL = "http://app.test"
CALLBACK = f"{APP_URL}/api/v1/auth/callback"


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """psycopg cannot run asynchronously on Windows' default proactor loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def _server_url() -> str:
    url = os.environ.get(SERVER_URL_VARIABLE)
    if not url:
        pytest.skip(f"{SERVER_URL_VARIABLE} is not set")
    return url


def _connect(url: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True)


@pytest.fixture
def database_url() -> Iterator[str]:
    server_url = _server_url()
    name = f"forge_test_{uuid.uuid4().hex[:12]}"
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
    return Settings.for_tests(database_url=migrated_database_url, public_url=APP_URL)


@pytest.fixture
async def factory(settings: Settings) -> AsyncIterator[SessionFactory]:
    engine = new_engine(str(settings.database_url))
    try:
        yield new_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def fake() -> FakeForge:
    forge = FakeForge(sign_in_redirect_uri=CALLBACK)
    forge.add_user(7, "ada", name="Ada Lovelace", email="ada@example.test")
    forge.add_user(8, "bob")
    forge.signed_in_user_id = 7
    return forge
