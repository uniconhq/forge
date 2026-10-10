"""The migration brings an empty database to the schema the tables declare,
with exactly the tables the package owns, and every revision goes down and
up again one step at a time, back to nothing and an activation job and a
grading row made by a submit included. A grading row round-trips with its
result, its numbers exactly as written; the rows of states that are gone are
moved to the ones that stand for them; and the task format's revision keeps
one grading per submission and attempt, its result emptied, and gives
extensions their tasks; and a submission staff cancelled before cancels
carried a sentence is given a stock one.
"""

import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from forge.adapters.ci.woodpecker.ci_state import read_state
from forge.adapters.ci.woodpecker.http import WoodpeckerAuth
from forge.adapters.fakes import FakeForge
from forge.db.migrations import alembic_config, downgrade_to_base, upgrade_to_head
from forge.db.tables import Grading, OrgAccount, metadata
from forge.domain.ids import OrgId
from forge.runtime.setup import Setup
from forge.services import org_accounts
from forge.settings import TEST_KEY, decode_key
from forge.testing import FakeClock

STOCK_REASON = "The organisers cancelled this grading."
"""The sentence revision 0014 gives a submission staff cancelled before
revision 0013."""

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
    "marks",
}


@pytest.fixture
def migrated_database_url(private_migrated_database_url: str) -> str:
    """These tests move the schema up and down, so each has a database of
    its own rather than the one the process's other tests share.
    """
    return private_migrated_database_url


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


async def test_a_grading_row_round_trips_with_its_result_exactly(setup: Setup) -> None:
    result = {
        "schema_version": 5,
        "stopped": None,
        "tests": [
            {
                "test": "main/1",
                "outcome": "accepted",
                "values": {"score": Decimal("1.0000000000000000001"), "time_ms": 12},
            }
        ],
        "values": {"fraction": Decimal("0.1"), "log": ""},
        "run_log": None,
        "error": None,
    }
    async with setup.unit_of_work() as ctx:
        row = Grading(
            task_id="acme/spring/sum",
            workspace_id="acme/spring/@u7",
            submission_id="acme/spring/@u7/sum#1",
            submission_number=1,
            submission_version="c" * 40,
            submitted_at=datetime.now(UTC),
            publication_id="acme/spring/sum#1",
            idempotency_key="key-12345678",
            status="done",
            result=result,
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
        stored: str = (
            await ctx.db.execute(text("SELECT result #>> '{tests,0,values,score}' FROM gradings"))
        ).scalar_one()
    assert found is not None and found.result is not None
    assert found.result == result
    score = found.result["tests"][0]["values"]["score"]
    assert isinstance(score, Decimal) and score > 1
    assert str(found.result["values"]["fraction"]) == "0.1"
    assert stored == "1.0000000000000000001"
    assert found.run_id == "12/3"


async def test_an_org_account_carries_its_ciphertext(setup: Setup) -> None:
    async with setup.unit_of_work() as ctx:
        ctx.db.add(
            OrgAccount(
                org_id="acme",
                username="unicon-ci-acme",
                forge_token=b"\x01",
                ci_state=b"\x03",
                event_secret=b"\x02",
            )
        )
    async with setup.unit_of_work() as ctx:
        account = (await ctx.db.execute(select(OrgAccount))).scalar_one()
    assert (account.forge_token, account.ci_state) == (b"\x01", b"\x03")
    assert account.forge_user_id is None
    assert account.created_at is not None


async def test_a_time_reads_back_in_utc_whatever_zone_the_server_is_in(setup: Setup) -> None:
    async with setup.unit_of_work() as ctx:
        zone: str = (await ctx.db.execute(text("SHOW TimeZone"))).scalar_one()
        ctx.db.add(
            OrgAccount(
                org_id="acme",
                username="unicon-ci-acme",
                forge_token=b"",
                ci_state=b"",
                event_secret=b"",
            )
        )
    async with setup.unit_of_work() as ctx:
        account = (await ctx.db.execute(select(OrgAccount))).scalar_one()

    assert zone == "UTC"
    assert account.created_at.utcoffset() == timedelta(0)


def _grading(
    connection: Any,
    status: str,
    wait_reason: str | None,
    key: str | None,
    number: int = 0,
    *,
    stage: str | None = "default",
) -> None:
    """A grading row, with a `wait_reason` only on a schema from before
    revision 0007, which still has one, and a `stage` only on a schema from
    before revision 0012.
    """
    columns = ", wait_reason" if wait_reason is not None else ""
    values = ", :reason" if wait_reason is not None else ""
    if stage is not None:
        columns += ", stage"
        values += ", :stage"
    connection.execute(
        text(
            "INSERT INTO gradings (id, task_id, workspace_id, submission_id, submission_number, "
            f"submission_version, submitted_at, publication_id, status{columns}, "
            "idempotency_key) VALUES (gen_random_uuid(), 'acme/spring/sum', 'acme/spring/@u7', "
            "'acme/spring/@u7/sum#' || :number, :number, 'c', now(), 'acme/spring/sum#1', "
            f":status{values}, :key)"
        ),
        {
            "number": number or (1 if key else 2),
            "status": status,
            "reason": wait_reason,
            "key": key,
            "stage": stage,
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


def test_a_key_is_unique_for_a_workspace_and_task(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "queued", None, "key-12345678", 1, stage=None)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _grading(connection, "queued", None, "key-12345678", 9, stage=None)
    engine.dispose()


def test_a_submission_has_one_grading_per_attempt(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "done", None, None, 1, stage=None)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _grading(connection, "queued", None, None, 1, stage=None)
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


def _revisions(database_url: str) -> list[str]:
    """Every revision, oldest first."""
    script = ScriptDirectory.from_config(alembic_config(database_url))
    return [revision.revision for revision in reversed(list(script.walk_revisions()))]


def test_every_revision_goes_down_and_up_again_one_step_at_a_time(
    migrated_database_url: str,
) -> None:
    config = alembic_config(migrated_database_url)
    revisions = _revisions(migrated_database_url)
    assert revisions[:12] == [f"{number:04}" for number in range(1, 13)]

    for revision in reversed(["base", *revisions[:-1]]):
        command.downgrade(config, revision)
    for revision in revisions:
        command.upgrade(config, revision)

    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        differences = compare_metadata(context, metadata)
    engine.dispose()
    assert differences == []


def _columns(database_url: str, table: str) -> set[str]:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        found = {column["name"] for column in inspect(connection).get_columns(table)}
    engine.dispose()
    return found


def test_the_task_format_keeps_one_grading_per_attempt_and_empties_its_result(
    migrated_database_url: str,
) -> None:
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0011")
    assert "stage" in _columns(migrated_database_url, "gradings")
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "done", None, "key-12345678", 1)
        _grading(connection, "done", None, None, 1, stage="final")
        connection.execute(text("""UPDATE gradings SET verdict = '{"outcome": "accepted"}'"""))
    engine.dispose()

    command.upgrade(config, "0012")

    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT submission_number, idempotency_key, status, result FROM gradings")
        ).all()
    engine.dispose()
    assert [tuple(row) for row in rows] == [(1, "key-12345678", "done", None)]
    assert not {"stage", "verdict"} & _columns(migrated_database_url, "gradings")
    assert "extension_tasks" in _columns(migrated_database_url, "contestants")
    assert {"time_extension_seconds", "extension_tasks"} <= _columns(migrated_database_url, "teams")

    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        connection.execute(text("""UPDATE gradings SET result = '{"schema_version": 5}'"""))
    engine.dispose()
    command.downgrade(config, "0011")

    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        back = connection.execute(text("SELECT stage, verdict FROM gradings")).all()
    engine.dispose()
    assert [tuple(row) for row in back] == [("default", None)]
    assert "result" not in _columns(migrated_database_url, "gradings")
    assert "extension_tasks" not in _columns(migrated_database_url, "contestants")
    assert not {"time_extension_seconds", "extension_tasks"} & _columns(
        migrated_database_url, "teams"
    )
    # The stage is back in the key, so another stage of the same attempt goes in.
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        _grading(connection, "queued", None, None, 1, stage="final")
    engine.dispose()
    upgrade_to_head(migrated_database_url)


def test_a_submission_staff_cancelled_before_reasons_is_given_the_stock_one(
    migrated_database_url: str,
) -> None:
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0013")
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        for number, attempt, status, reason in (
            (1, 1, "cancelled", None),
            (1, 2, "done", None),
            (2, 1, "cancelled", None),
            (3, 1, "cancelled", "The checker broke on this one."),
        ):
            connection.execute(
                text(
                    "INSERT INTO gradings (id, task_id, workspace_id, submission_id, "
                    "submission_number, submission_version, submitted_at, publication_id, "
                    "attempt, status, cancel_reason) VALUES (gen_random_uuid(), "
                    "'acme/spring/sum', 'acme/spring/@u7', 'acme/spring/@u7/sum#' || :number, "
                    ":number, 'c', now(), 'acme/spring/sum#1', :attempt, :status, :reason)"
                ),
                {"number": number, "attempt": attempt, "status": status, "reason": reason},
            )
    engine.dispose()

    def reasons() -> list[tuple[Any, ...]]:
        engine = create_engine(migrated_database_url)
        with engine.connect() as connection:
            found = connection.execute(
                text(
                    "SELECT submission_number, attempt, cancel_reason FROM gradings "
                    "ORDER BY submission_number, attempt"
                )
            ).all()
        engine.dispose()
        return [tuple(row) for row in found]

    command.upgrade(config, "0014")

    # The latest attempts staff cancelled have a sentence; one a later attempt replaced has none.
    assert reasons() == [
        (1, 1, None),
        (1, 2, None),
        (2, 1, STOCK_REASON),
        (3, 1, "The checker broke on this one."),
    ]
    command.downgrade(config, "0013")
    assert reasons() == [
        (1, 1, None),
        (1, 2, None),
        (2, 1, None),
        (3, 1, "The checker broke on this one."),
    ]
    upgrade_to_head(migrated_database_url)


SIGNED_IN_AT = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)


def _sealed(plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(decode_key(SecretStr(TEST_KEY))).encrypt(nonce, plaintext, None)


def _opened(blob: bytes) -> bytes:
    return AESGCM(decode_key(SecretStr(TEST_KEY))).decrypt(blob[:12], blob[12:], None)


def _accounts_before_the_state(database_url: str, token: bytes) -> None:
    """Two org accounts as revision 0016 holds them: one signed in at the CI
    with `token` sealed under the test key, one not signed in yet.
    """
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO org_accounts (id, org_id, forge_user_id, username, forge_token, "
                "ci_token, ci_user_id, event_secret, ci_signed_in_at) VALUES "
                "(gen_random_uuid(), 'acme', 9, 'unicon-ci-acme', :forge, :ci, 4, :secret, :at), "
                "(gen_random_uuid(), 'beta', NULL, 'unicon-ci-beta', '', '', NULL, :secret, NULL)"
            ),
            {
                "forge": _sealed(b"forge-token-acme"),
                "ci": _sealed(token),
                "secret": _sealed(b"secret"),
                "at": SIGNED_IN_AT,
            },
        )
    engine.dispose()


def _rows(database_url: str, columns: str) -> dict[str, Any]:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        found = connection.execute(
            text(f"SELECT org_id, {columns} FROM org_accounts ORDER BY org_id")
        ).mappings()
        rows = {row["org_id"]: dict(row) for row in found}
    engine.dispose()
    return rows


def test_the_ci_credentials_move_into_one_state_and_back_with_the_same_token(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0016")
    _accounts_before_the_state(migrated_database_url, b"ci-token-acme")

    command.upgrade(config, "head")

    moved = _rows(migrated_database_url, "ci_state")
    assert json.loads(_opened(moved["acme"]["ci_state"])) == {
        "user_id": 4,
        "token": "ci-token-acme",
        "signed_in_at": SIGNED_IN_AT.isoformat(),
        "account_id": 9,
    }
    assert moved["beta"]["ci_state"] == b""
    assert {"ci_token", "ci_user_id", "ci_signed_in_at"} & _columns(
        migrated_database_url, "org_accounts"
    ) == set()

    command.downgrade(config, "0016")

    back = _rows(migrated_database_url, "ci_token, ci_user_id, ci_signed_in_at")
    assert _opened(back["acme"]["ci_token"]) == b"ci-token-acme"
    assert (back["acme"]["ci_user_id"], back["acme"]["ci_signed_in_at"]) == (4, SIGNED_IN_AT)
    assert (back["beta"]["ci_token"], back["beta"]["ci_user_id"]) == (b"", None)
    assert back["beta"]["ci_signed_in_at"] is None
    assert "ci_state" not in _columns(migrated_database_url, "org_accounts")
    command.upgrade(config, "head")


def test_without_the_key_a_token_is_not_moved_and_nothing_changes(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UNICON_TOKEN_ENCRYPTION_KEY", raising=False)
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0016")
    _accounts_before_the_state(migrated_database_url, b"ci-token-acme")

    with pytest.raises(RuntimeError, match="UNICON_TOKEN_ENCRYPTION_KEY"):
        command.upgrade(config, "head")

    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == "0016"
    engine.dispose()
    kept = _rows(migrated_database_url, "ci_token")
    assert _opened(kept["acme"]["ci_token"]) == b"ci-token-acme"
    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    command.upgrade(config, "head")


async def test_an_orgs_ci_token_still_works_after_the_state_is_made(
    migrated_database_url: str,
    setup: Setup,
    fake: FakeForge,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The org signed in a day before the migration grades on with the token
    it had: nothing is refreshed, and the CI is shown the same token.
    """
    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0016")
    _accounts_before_the_state(migrated_database_url, b"ci-token-acme")
    command.upgrade(config, "head")
    clock.set(SIGNED_IN_AT + timedelta(days=1))

    async with setup.unit_of_work() as ctx:
        account = await org_accounts.identity(ctx, OrgId("acme"))

    assert account.forge_token == "forge-token-acme"
    assert read_state(account.ci_state).token == "ci-token-acme"
    assert await WoodpeckerAuth("ci-admin").header(account) == "Bearer ci-token-acme"
    assert fake.calls_to("refresh") == []


async def test_an_orgs_state_made_by_the_migration_is_refreshed_once_stale(
    migrated_database_url: str,
    setup: Setup,
    fake: FakeForge,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state the migration made names the account by its forge id, so
    once it is stale the org signs in again as that account.
    """
    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0016")
    _accounts_before_the_state(migrated_database_url, b"ci-token-acme")
    command.upgrade(config, "head")
    fake.add_user(9, "unicon-ci-acme")
    fake.state.ci_users["unicon-ci-acme"] = 4
    clock.set(SIGNED_IN_AT + timedelta(days=21))

    async with setup.unit_of_work() as ctx:
        account = await org_accounts.identity(ctx, OrgId("acme"))

    state = read_state(account.ci_state)
    assert (state.user_id, state.account_id) == (4, 9)
    assert state.token not in ("", "ci-token-acme")
    assert state.signed_in_at == clock.now()
    assert len(fake.calls_to("refresh")) == 1


OTHER_KEY = base64.urlsafe_b64encode(b"\x01" * 32).decode().rstrip("=")


def _revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        found = MigrationContext.configure(connection).get_current_revision()
    engine.dispose()
    return found


def test_a_wrong_key_moves_nothing_in_either_direction(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0016")
    _accounts_before_the_state(migrated_database_url, b"ci-token-acme")

    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", OTHER_KEY)
    with pytest.raises(RuntimeError, match="does not open"):
        command.upgrade(config, "head")
    assert _revision(migrated_database_url) == "0016"

    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    command.upgrade(config, "head")
    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", OTHER_KEY)
    with pytest.raises(RuntimeError, match="does not open"):
        command.downgrade(config, "0016")
    assert _revision(migrated_database_url) == "0017"


def test_going_back_without_the_key_moves_nothing(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "0016")
    _accounts_before_the_state(migrated_database_url, b"ci-token-acme")
    monkeypatch.setenv("UNICON_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    command.upgrade(config, "head")

    monkeypatch.delenv("UNICON_TOKEN_ENCRYPTION_KEY")
    with pytest.raises(RuntimeError, match="UNICON_TOKEN_ENCRYPTION_KEY"):
        command.downgrade(config, "0016")

    assert _revision(migrated_database_url) == "0017"
    assert _rows(migrated_database_url, "ci_state")["acme"]["ci_state"] != b""
