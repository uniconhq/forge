"""The migration brings an empty database to the schema the tables declare,
with exactly the tables the package owns, and a grading row round-trips with
its verdict.
"""

import uuid
from datetime import UTC, datetime

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from forge.db.base import Base
from forge.db.migrations import downgrade_to_base, upgrade_to_head
from forge.db.tables import Grading, register
from forge.runtime.setup import Setup

TABLES = {
    "sessions",
    "contestants",
    "teams",
    "team_members",
    "invites",
    "provisioning",
    "gradings",
    "uploads",
    "jupyter_sessions",
}


def test_the_schema_has_exactly_the_tables_the_package_owns(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    names = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert names == TABLES


def test_the_migration_matches_the_tables(migrated_database_url: str) -> None:
    register()
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()
    assert differences == []


def test_the_migration_rolls_back(migrated_database_url: str) -> None:
    downgrade_to_base(migrated_database_url)
    engine = create_engine(migrated_database_url)
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    engine.dispose()
    upgrade_to_head(migrated_database_url)


async def test_a_grading_row_round_trips_with_its_verdict(setup: Setup) -> None:
    verdict = {"outcome": "verdict", "verdict": "AC", "score": "100", "summary": [{"id": "1"}]}
    async with setup.unit_of_work() as ctx:
        row = Grading(
            workspace_id="acme/spring/@ada",
            submission_id="acme/spring/@ada/sum#1",
            publication_id="acme/spring/sum#1",
            stage="public",
            status="done",
            verdict=verdict,
            log_key="logs/1",
            run_id="12/3",
            compute_id=uuid.uuid7(),
            callback_token_hash=b"\x00" * 32,
            selected_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        ctx.db.add(row)
        await ctx.db.flush()
        row_id = row.id
    async with setup.unit_of_work() as ctx:
        found = await ctx.db.get(Grading, row_id)
    assert found is not None
    assert found.verdict == verdict
    assert found.run_id == "12/3"
