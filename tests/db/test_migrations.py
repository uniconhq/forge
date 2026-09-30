"""The migration brings an empty database to the schema the tables declare,
with exactly the tables the package owns and rolls back to nothing, an
activation job and a grading row made by a submit included, and a grading
row round-trips with its verdict.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from forge.db.migrations import alembic_config, downgrade_to_base, upgrade_to_head
from forge.db.tables import Grading, OrgAccount, Provisioning, metadata
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
    "org_accounts",
}


def test_the_schema_has_exactly_the_tables_the_package_owns(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    names = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert names == TABLES


def test_the_migration_matches_the_tables(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        differences = compare_metadata(context, metadata)
    engine.dispose()
    assert differences == []


def test_the_migration_rolls_back(migrated_database_url: str) -> None:
    downgrade_to_base(migrated_database_url)
    engine = create_engine(migrated_database_url)
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    engine.dispose()
    upgrade_to_head(migrated_database_url)


def test_the_migration_rolls_back_over_an_activation_and_a_submission_place_job(
    migrated_database_url: str,
) -> None:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO provisioning (id, kind, target_id, last_step) "
                "VALUES (gen_random_uuid(), 'activation', 'acme/spring/sum', 'activate'), "
                "(gen_random_uuid(), 'submission_place', 'x/acme/spring/sum', NULL)"
            )
        )
    engine.dispose()

    command.downgrade(alembic_config(migrated_database_url), "0002")
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        jobs = connection.execute(text("SELECT kind, last_step FROM provisioning")).all()
    engine.dispose()
    assert [tuple(job) for job in jobs] == [("registration", "register")]
    downgrade_to_base(migrated_database_url)
    upgrade_to_head(migrated_database_url)


async def test_a_grading_row_round_trips_with_its_verdict(setup: Setup) -> None:
    verdict = {"outcome": "verdict", "verdict": "AC", "score": "100", "summary": [{"id": "1"}]}
    async with setup.unit_of_work() as ctx:
        row = Grading(
            task_id="acme/spring/sum",
            workspace_id="acme/spring/@ada",
            submission_id="acme/spring/@ada/sum#1",
            submission_number=1,
            submission_version="c" * 40,
            submitted_at=datetime.now(UTC),
            publication_id="acme/spring/sum#1",
            stage="public",
            idempotency_key="key-12345678",
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


async def test_a_provisioning_row_carries_its_payload_and_an_org_account_its_ciphertext(
    setup: Setup,
) -> None:
    async with setup.unit_of_work() as ctx:
        ctx.db.add(Provisioning(kind="org", target_id="acme", payload={"description": "Acme"}))
        ctx.db.add(
            OrgAccount(
                org_name="acme",
                username="unicon-ci-acme",
                forge_token=b"\x01",
                ci_token=b"",
                event_secret=b"\x02",
            )
        )
    async with setup.unit_of_work() as ctx:
        job = (await ctx.db.execute(select(Provisioning))).scalar_one()
        account = (await ctx.db.execute(select(OrgAccount))).scalar_one()
    assert job.payload == {"description": "Acme"}
    assert (account.forge_user_id, account.ci_user_id, account.last_kept_alive_at) == (
        None,
        None,
        None,
    )
    assert account.created_at is not None


async def test_a_time_reads_back_in_utc_whatever_zone_the_server_is_in(setup: Setup) -> None:
    async with setup.unit_of_work() as ctx:
        zone: str = (await ctx.db.execute(text("SHOW TimeZone"))).scalar_one()
        ctx.db.add(Provisioning(kind="org", target_id="acme"))
    async with setup.unit_of_work() as ctx:
        job = (await ctx.db.execute(select(Provisioning))).scalar_one()

    assert zone == "UTC"
    assert job.created_at.utcoffset() == timedelta(0)


def _grading(connection: Any, status: str, wait_reason: str | None, key: str | None) -> None:
    connection.execute(
        text(
            "INSERT INTO gradings (id, task_id, workspace_id, submission_id, submission_number, "
            "submission_version, submitted_at, publication_id, stage, status, wait_reason, "
            "idempotency_key) VALUES (gen_random_uuid(), 'acme/spring/sum', 'acme/spring/@ada', "
            "'acme/spring/@ada/sum#' || :number, :number, 'c', now(), 'acme/spring/sum#1', "
            "'default', :status, :reason, :key)"
        ),
        {"number": 1 if key else 2, "status": status, "reason": wait_reason, "key": key},
    )


def test_a_key_is_unique_for_a_workspace_task_and_stage(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "queued", None, "key-12345678")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO gradings (id, task_id, workspace_id, submission_id, "
                "submission_number, submission_version, submitted_at, publication_id, stage, "
                "status, idempotency_key) VALUES (gen_random_uuid(), 'acme/spring/sum', "
                "'acme/spring/@ada', 'acme/spring/@ada/sum#9', 9, 'c', now(), "
                "'acme/spring/sum#1', 'default', 'queued', 'key-12345678')"
            )
        )
    engine.dispose()


def test_going_back_from_submissions_keeps_the_rows_within_the_old_checks(
    migrated_database_url: str,
) -> None:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "system_error", "The CI refused the start: no machine.", "k-1234567")
        _grading(connection, "queued", "ci_unavailable", None)
    engine.dispose()

    command.downgrade(alembic_config(migrated_database_url), "0003")
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT submission_id, status, wait_reason FROM gradings ORDER BY submission_id")
        ).all()
        columns = {column["name"] for column in inspect(connection).get_columns("gradings")}
    engine.dispose()
    assert [tuple(row) for row in rows] == [
        ("acme/spring/@ada/sum#1", "failed", None),
        ("acme/spring/@ada/sum#2", "queued", "ci_unavailable"),
    ]
    assert not columns & {"task_id", "submission_number", "idempotency_key", "submitted_at"}

    upgrade_to_head(migrated_database_url)
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT task_id, submission_number, submission_version FROM gradings "
                "ORDER BY submission_number"
            )
        ).all()
    engine.dispose()
    assert [tuple(row) for row in found] == [
        ("acme/spring/sum", 1, ""),
        ("acme/spring/sum", 2, ""),
    ]


def test_a_grading_from_before_dispatch_entered_the_queue_when_it_was_made(
    migrated_database_url: str,
) -> None:
    command.downgrade(alembic_config(migrated_database_url), "0004")
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "queued", None, "k-1234567")
    engine.dispose()

    upgrade_to_head(migrated_database_url)
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT queued_at = created_at, start_failures, retry_at, requeues, progress "
                "FROM gradings"
            )
        ).one()
    engine.dispose()
    assert tuple(found) == (True, 0, None, 0, None)

    command.downgrade(alembic_config(migrated_database_url), "0004")
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("gradings")}
    engine.dispose()
    assert not columns & {"queued_at", "start_failures", "retry_at", "requeues", "progress"}
