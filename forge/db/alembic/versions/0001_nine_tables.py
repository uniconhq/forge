"""The nine tables the platform owns.

Revision ID: 0001
Revises:
Create Date: 2026-09-26 12:00:00
"""

from collections.abc import Sequence
from datetime import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("credential", sa.LargeBinary(), nullable=False),
        sa.Column("credential_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    op.create_table(
        "contestants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contest_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "eligibility",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "time_extension_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected', 'withdrawn', 'removed')",
            name=op.f("ck_contestants_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contestants")),
        sa.UniqueConstraint(
            "contest_id", "user_id", name=op.f("uq_contestants_contest_id_user_id")
        ),
    )
    op.create_index(
        "ix_contestants_contest_id_status", "contestants", ["contest_id", "status"], unique=False
    )

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
        sa.CheckConstraint(
            "kind in ('org', 'contest', 'task', 'workspace')", name=op.f("ck_provisioning_kind")
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

    op.create_table(
        "gradings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("submission_id", sa.Text(), nullable=False),
        sa.Column("publication_id", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("wait_reason", sa.Text(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("log_key", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("compute_id", sa.Uuid(), nullable=True),
        sa.Column("callback_token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("selected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('queued', 'dispatching', 'dispatched', 'running', 'done', 'failed', 'cancelled')",
            name=op.f("ck_gradings_status"),
        ),
        sa.CheckConstraint(
            "wait_reason is null or wait_reason in ('waiting_for_compute', 'ci_unavailable')",
            name=op.f("ck_gradings_wait_reason"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gradings")),
        sa.UniqueConstraint(
            "submission_id",
            "stage",
            "attempt",
            name=op.f("uq_gradings_submission_id_stage_attempt"),
        ),
    )
    op.create_index(
        "ix_gradings_status_unfinished",
        "gradings",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status in ('queued', 'dispatching', 'dispatched', 'running')"),
    )
    op.create_index("ix_gradings_workspace_id", "gradings", ["workspace_id"], unique=False)

    op.create_table(
        "uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("input_id", sa.Text(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("actual_size", sa.BigInteger(), nullable=True),
        sa.Column("digest", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("multipart_upload_id", sa.Text(), nullable=True),
        sa.Column("consumed_by", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("purpose in ('submission', 'asset')", name=op.f("ck_uploads_purpose")),
        sa.CheckConstraint(
            "status in ('presigned', 'uploaded', 'verified', 'consumed', 'rejected', 'expired')",
            name=op.f("ck_uploads_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
        sa.UniqueConstraint("object_key", name=op.f("uq_uploads_object_key")),
    )
    op.create_index("ix_uploads_expires_at", "uploads", ["expires_at"], unique=False)
    op.create_index(
        "ix_uploads_owner_user_id_status", "uploads", ["owner_user_id", "status"], unique=False
    )

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


def downgrade() -> None:
    op.drop_table("jupyter_sessions")
    op.drop_index("ix_uploads_owner_user_id_status", table_name="uploads")
    op.drop_index("ix_uploads_expires_at", table_name="uploads")
    op.drop_table("uploads")
    op.drop_index("ix_gradings_workspace_id", table_name="gradings")
    op.drop_index(
        "ix_gradings_status_unfinished",
        table_name="gradings",
        postgresql_where=sa.text("status in ('queued', 'dispatching', 'dispatched', 'running')"),
    )
    op.drop_table("gradings")
    op.drop_index(
        "ix_provisioning_status_waiting",
        table_name="provisioning",
        postgresql_where=sa.text("status in ('pending', 'failed')"),
    )
    op.drop_table("provisioning")
    op.drop_index("ix_invites_target_user_id", table_name="invites")
    op.drop_index("ix_invites_target_email", table_name="invites")
    op.drop_index("ix_invites_scope_kind_scope_id_status", table_name="invites")
    op.drop_table("invites")
    op.drop_index(
        "uq_team_members_contest_id_user_id_active",
        table_name="team_members",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_index("ix_contestants_contest_id_status", table_name="contestants")
    op.drop_table("contestants")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_table("sessions")
