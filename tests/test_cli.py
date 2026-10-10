"""`unicon-forge migrate` reads the database URL itself and brings an empty
database to the latest migration, and `unicon-forge reconcile` runs the
reconcile pass once over the settings it reads and logs what it did.
"""

import json
import logging

import pytest
from sqlalchemy import create_engine, inspect

from forge.cli import main
from forge.settings import TEST_KEY


def test_migrate_brings_an_empty_database_to_the_latest_migration(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_DATABASE_URL", database_url)

    assert main(["migrate"]) == 0

    engine = create_engine(database_url)
    names = set(inspect(engine).get_table_names())
    engine.dispose()
    assert {"alembic_version", "sessions"} <= names


def test_reconcile_runs_the_pass_once_and_logs_what_it_did(
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in {
        "UNICON_PUBLIC_URL": "http://app.test",
        "UNICON_DATABASE_URL": migrated_database_url,
        "UNICON_TOKEN_ENCRYPTION_KEY": TEST_KEY,
        "UNICON_SESSION_SIGNING_KEY": TEST_KEY,
        "UNICON_FORGE": "fake",
        "UNICON_FORGE_PUBLIC_URL": "http://forge.test",
    }.items():
        monkeypatch.setenv(name, value)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert main(["reconcile"]) == 0
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)

    [done] = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if '"reconcile.done"' in line
    ]
    assert (done["contests"], done["submissions"], done["inserted"], done["activated"]) == (
        0,
        0,
        0,
        0,
    )
