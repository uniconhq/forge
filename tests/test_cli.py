"""`unicon-forge migrate` reads the database URL itself and brings an empty
database to the latest migration.
"""

import pytest
from sqlalchemy import create_engine, inspect

from forge.cli import main


def test_migrate_brings_an_empty_database_to_the_latest_migration(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_DATABASE_URL", database_url)

    assert main(["migrate"]) == 0

    engine = create_engine(database_url)
    names = set(inspect(engine).get_table_names())
    engine.dispose()
    assert {"alembic_version", "sessions"} <= names
