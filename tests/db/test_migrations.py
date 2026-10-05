"""The migration brings an empty database to the schema the tables declare,
with exactly the tables the package owns and rolls back to nothing, an
activation job and a grading row made by a submit included, a grading row
round-trips with its verdict, and the rows of states that are gone are moved
to the ones that stand for them.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from forge.db.migrations import alembic_config, downgrade_to_base, upgrade_to_head
from forge.db.tables import Grading, OrgAccount, metadata
from forge.runtime.setup import Setup

TABLES = {
    "sessions",
    "contestants",
    "gradings",
    "uploads",
    "org_accounts",
    "names",
    "invites",
    "teams",
    "team_members",
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
    command.downgrade(alembic_config(migrated_database_url), "0006")
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
            workspace_id="acme/spring/@u7",
            submission_id="acme/spring/@u7/sum#1",
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
            callback_token_hash=b"\x00" * 32,
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


async def test_an_org_account_carries_its_ciphertext(setup: Setup) -> None:
    async with setup.unit_of_work() as ctx:
        ctx.db.add(
            OrgAccount(
                org_id="acme",
                username="unicon-ci-acme",
                forge_token=b"\x01",
                ci_token=b"",
                event_secret=b"\x02",
            )
        )
    async with setup.unit_of_work() as ctx:
        account = (await ctx.db.execute(select(OrgAccount))).scalar_one()
    assert account.forge_token == b"\x01"
    assert (account.forge_user_id, account.ci_user_id, account.ci_signed_in_at) == (
        None,
        None,
        None,
    )
    assert account.created_at is not None


async def test_a_time_reads_back_in_utc_whatever_zone_the_server_is_in(setup: Setup) -> None:
    async with setup.unit_of_work() as ctx:
        zone: str = (await ctx.db.execute(text("SHOW TimeZone"))).scalar_one()
        ctx.db.add(
            OrgAccount(
                org_id="acme",
                username="unicon-ci-acme",
                forge_token=b"",
                ci_token=b"",
                event_secret=b"",
            )
        )
    async with setup.unit_of_work() as ctx:
        account = (await ctx.db.execute(select(OrgAccount))).scalar_one()

    assert zone == "UTC"
    assert account.created_at.utcoffset() == timedelta(0)


def _grading(
    connection: Any, status: str, wait_reason: str | None, key: str | None, number: int = 0
) -> None:
    """A grading row, with a `wait_reason` only on a schema from before
    revision 0007, which still has one.
    """
    columns = ", wait_reason" if wait_reason is not None else ""
    values = ", :reason" if wait_reason is not None else ""
    connection.execute(
        text(
            "INSERT INTO gradings (id, task_id, workspace_id, submission_id, submission_number, "
            f"submission_version, submitted_at, publication_id, stage, status{columns}, "
            "idempotency_key) VALUES (gen_random_uuid(), 'acme/spring/sum', 'acme/spring/@u7', "
            "'acme/spring/@u7/sum#' || :number, :number, 'c', now(), 'acme/spring/sum#1', "
            f"'default', :status{values}, :key)"
        ),
        {
            "number": number or (1 if key else 2),
            "status": status,
            "reason": wait_reason,
            "key": key,
        },
    )


def test_a_name_is_of_one_of_the_three_kinds(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO names (id, kind, parent, name) VALUES ('k1', 'org', '', 'acme')")
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("INSERT INTO names (id, kind, parent, name) VALUES ('k2', 'contests', '', 'x')")
        )
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM names WHERE id = 'k1'"))
    engine.dispose()


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
                "'acme/spring/@u7', 'acme/spring/@u7/sum#9', 9, 'c', now(), "
                "'acme/spring/sum#1', 'default', 'queued', 'key-12345678')"
            )
        )
    engine.dispose()


def test_going_back_from_submissions_keeps_the_rows_within_the_old_checks(
    migrated_database_url: str,
) -> None:
    command.downgrade(alembic_config(migrated_database_url), "0006")
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
        ("acme/spring/@u7/sum#1", "failed", None),
        ("acme/spring/@u7/sum#2", "queued", "ci_unavailable"),
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

    command.upgrade(alembic_config(migrated_database_url), "0005")
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


def test_names_and_keys_refuse_a_database_that_already_has_orgs(
    migrated_database_url: str,
) -> None:
    command.downgrade(alembic_config(migrated_database_url), "0005")
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO org_accounts (id, org_name, username, forge_token, ci_token, "
                "event_secret) VALUES (gen_random_uuid(), 'acme', 'unicon-ci-acme', "
                "'x', 'x', 'x')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Reset the database"):
        command.upgrade(alembic_config(migrated_database_url), "0006")

    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM org_accounts"))
    engine.dispose()
    upgrade_to_head(migrated_database_url)


def test_the_states_that_are_gone_become_the_ones_that_stand_for_them(
    migrated_database_url: str,
) -> None:
    command.downgrade(alembic_config(migrated_database_url), "0006")
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "dispatching", "The CI did not answer.", "k-1234567", 1)
        _grading(connection, "failed", "The CI did not answer.", "k-7654321", 2)
        _grading(connection, "queued", "The CI did not answer.", "k-0000000", 3)
        connection.execute(
            text(
                "INSERT INTO uploads (id, owner_user_id, purpose, object_key, filename, "
                "declared_size, actual_size, digest, status, consumed_by, expires_at) "
                "VALUES (gen_random_uuid(), 8, 'submission', 'uploads/1', 'main.py', 3, 3, "
                "sha256('abc'), 'expired', NULL, now()), "
                "(gen_random_uuid(), 8, 'submission', 'uploads/2', 'used.py', 3, 3, "
                "sha256('def'), 'consumed', 'acme/spring/@u8/sum#1', now())"
            )
        )
    engine.dispose()

    upgrade_to_head(migrated_database_url)
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        gradings = connection.execute(
            text("SELECT status, finished_at IS NOT NULL FROM gradings ORDER BY submission_number")
        ).all()
        uploads: list[str] = list(connection.execute(text("SELECT status FROM uploads")).scalars())
    engine.dispose()
    assert [tuple(row) for row in gradings] == [("system_error", True)] * 3
    # The one a submission took is the record of what was submitted and
    # stays; the one that only lapsed named bytes in a bucket nothing reads
    # after 0009, and 'consumed' is the word 0007 gave it, so it goes.
    assert uploads == ["consumed"]
