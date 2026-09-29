"""The org accounts, the request that started a provisioning job, when a
failed one is next tried and the step it stopped at, and a task's
registration for grading as a kind of job. Going back removes the registration jobs, a kind the first revision
does not know, along with everything else this one added.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_BEFORE = "kind in ('org', 'contest', 'task', 'workspace')"
KINDS_AFTER = "kind in ('org', 'contest', 'task', 'workspace', 'registration')"


def upgrade() -> None:
    op.create_table(
        "org_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_name", sa.Text(), nullable=False),
        sa.Column("forge_user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("forge_token", sa.LargeBinary(), nullable=False),
        sa.Column("ci_token", sa.LargeBinary(), nullable=False),
        sa.Column("ci_user_id", sa.BigInteger(), nullable=True),
        sa.Column("event_secret", sa.LargeBinary(), nullable=False),
        sa.Column("last_kept_alive_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("keepalive_error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_accounts")),
        sa.UniqueConstraint("org_name", name=op.f("uq_org_accounts_org_name")),
    )
    op.add_column(
        "provisioning",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("provisioning", sa.Column("retry_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("provisioning", sa.Column("failed_step", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_provisioning_kind"), "provisioning", type_="check")
    op.create_check_constraint(op.f("ck_provisioning_kind"), "provisioning", KINDS_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM provisioning WHERE kind = 'registration'")
    op.drop_constraint(op.f("ck_provisioning_kind"), "provisioning", type_="check")
    op.create_check_constraint(op.f("ck_provisioning_kind"), "provisioning", KINDS_BEFORE)
    op.drop_column("provisioning", "failed_step")
    op.drop_column("provisioning", "retry_at")
    op.drop_column("provisioning", "payload")
    op.drop_table("org_accounts")
