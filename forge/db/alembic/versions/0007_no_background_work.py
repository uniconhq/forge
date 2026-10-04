"""What the platform keeps once every piece of work is done by the request
that asks for it: no `provisioning` table, since an org, a contest and a task
are made before the request answers and a contestant's workspace when it is
first needed; no workspace on a contestant's row, since its id comes from
the contest and the person; a grading row without the dispatcher's
bookkeeping, since its run is started by the request that made it; an org
account that records when it last signed in at the CI; and no tables for
teams, invites or notebook servers, which no feature uses yet.

A grading that was `dispatching` or `failed` becomes `system_error`, and so
does one still `queued` or `dispatched`: nothing will start the first, and
the run of the second was given secrets numbered by its run, which are no
longer made. An `expired` upload becomes `consumed`, which is what it was.

Going back puts the tables and columns back, empty.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-03 18:00:00
"""

from collections.abc import Sequence
from datetime import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRADING_STATUSES_BEFORE = (
    "status in ('queued', 'dispatching', 'dispatched', 'running', 'done', 'failed', "
    "'cancelled', 'system_error')"
)
GRADING_STATUSES_AFTER = (
    "status in ('queued', 'dispatched', 'running', 'done', 'cancelled', 'system_error')"
)
UPLOAD_STATUSES_BEFORE = (
    "status in ('presigned', 'uploaded', 'verified', 'consumed', 'rejected', 'expired')"
)
UPLOAD_STATUSES_AFTER = "status in ('presigned', 'verified', 'consumed', 'rejected')"
UNFINISHED_BEFORE = "status in ('queued', 'dispatching', 'dispatched', 'running')"
DROPPED_GRADING_COLUMNS = (
    "wait_reason",
    "start_failures",
    "retry_at",
    "requeues",
    "compute_id",
    "selected_at",
)


def upgrade() -> None:
    op.drop_table("provisioning")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("invites")
    op.drop_table("jupyter_sessions")
    op.drop_column("contestants", "workspace_id")

    op.alter_column("org_accounts", "last_kept_alive_at", new_column_name="ci_signed_in_at")
    op.drop_column("org_accounts", "keepalive_error")

    op.drop_index("ix_gradings_status_unfinished", table_name="gradings")
    op.drop_constraint(op.f("ck_gradings_status"), "gradings", type_="check")
    op.execute(
        "UPDATE gradings SET status = 'system_error', finished_at = coalesce(finished_at, now()), "
        "error = coalesce(error, 'Its run was lost when the platform was upgraded.') "
        "WHERE status IN ('queued', 'dispatching', 'dispatched', 'failed')"
    )
    op.create_check_constraint(op.f("ck_gradings_status"), "gradings", GRADING_STATUSES_AFTER)
    for column in DROPPED_GRADING_COLUMNS:
        op.drop_column("gradings", column)

    op.drop_constraint(op.f("ck_uploads_status"), "uploads", type_="check")
    op.execute("UPDATE uploads SET status = 'consumed' WHERE status = 'expired'")
    op.create_check_constraint(op.f("ck_uploads_status"), "uploads", UPLOAD_STATUSES_AFTER)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_uploads_status"), "uploads", type_="check")
    op.create_check_constraint(op.f("ck_uploads_status"), "uploads", UPLOAD_STATUSES_BEFORE)

    op.add_column("gradings", sa.Column("wait_reason", sa.Text(), nullable=True))
    op.add_column(
        "gradings",
        sa.Column("start_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("gradings", sa.Column("retry_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "gradings",
        sa.Column("requeues", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("gradings", sa.Column("compute_id", sa.Uuid(), nullable=True))
    op.add_column("gradings", sa.Column("selected_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_gradings_status"), "gradings", type_="check")
    op.create_check_constraint(op.f("ck_gradings_status"), "gradings", GRADING_STATUSES_BEFORE)
    op.create_index(
        "ix_gradings_status_unfinished",
        "gradings",
        ["status"],
        unique=False,
        postgresql_where=sa.text(UNFINISHED_BEFORE),
    )

    op.add_column("org_accounts", sa.Column("keepalive_error", sa.Text(), nullable=True))
    op.alter_column("org_accounts", "ci_signed_in_at", new_column_name="last_kept_alive_at")

    op.add_column("contestants", sa.Column("workspace_id", sa.Text(), nullable=True))
    _create_jupyter_sessions()
    _create_invites()
    _create_teams()
    _create_provisioning()


def _timestamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _create_provisioning() -> None:
    op.create_table(
        "provisioning",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("last_step", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("ready_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_step", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind in ('org', 'contest', 'task', 'workspace', 'submission_place', 'activation')",
            name=op.f("ck_provisioning_kind"),
        ),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'ready', 'failed')",
            name=op.f("ck_provisioning_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisioning")),
        sa.UniqueConstraint("kind", "target_id", name=op.f("uq_provisioning_kind_target_id")),
    )
    op.create_index(
        "ix_provisioning_status_waiting",
        "provisioning",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status in ('pending', 'failed')"),
    )


def _create_teams() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("leader_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("contest_id", "slug", name=op.f("uq_teams_contest_id_slug")),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("workspace_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('requested', 'active', 'left', 'removed')",
            name=op.f("ck_team_members_status"),
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], name=op.f("fk_team_members_team_id")),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_team_members")),
    )
    op.create_index(
        "uq_team_members_contest_id_user_id_active",
        "team_members",
        ["contest_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def _create_invites() -> None:
    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("target_email", sa.Text(), nullable=True),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("grants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("invited_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "scope_kind in ('org', 'contest', 'task', 'team')", name=op.f("ck_invites_scope_kind")
        ),
        sa.CheckConstraint(
            "status in ('pending', 'accepted', 'declined', 'revoked', 'expired')",
            name=op.f("ck_invites_status"),
        ),
        sa.CheckConstraint(
            "target_email is not null or target_user_id is not null",
            name=op.f("ck_invites_has_target"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
    )
    op.create_index(
        "ix_invites_scope_kind_scope_id_status",
        "invites",
        ["scope_kind", "scope_id", "status"],
        unique=False,
    )
    op.create_index("ix_invites_target_email", "invites", ["target_email"], unique=False)
    op.create_index("ix_invites_target_user_id", "invites", ["target_user_id"], unique=False)


def _create_jupyter_sessions() -> None:
    op.create_table(
        "jupyter_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("input_id", sa.Text(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=True),
        sa.Column("server_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("spawned_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('spawning', 'running', 'stopped', 'failed')",
            name=op.f("ck_jupyter_sessions_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jupyter_sessions")),
        sa.UniqueConstraint(
            "user_id",
            "task_id",
            "input_id",
            name=op.f("uq_jupyter_sessions_user_id_task_id_input_id"),
        ),
    )
