"""The names people give orgs, contests and tasks, kept apart from the keys
they are filed under, and the org accounts filed by the org's id.

Every id an org, a contest or a task had before this revision was built from
names, so a database from before has rows that no key reaches: this
revision is for a database with no orgs yet, refuses one that has any,
and development stacks are reset across it. Going back drops the names and renames the column back.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM org_accounts LIMIT 1")).first():
        raise RuntimeError(
            "This database has orgs from before names and keys (revision 0006), which no "
            "key would reach after it. Reset the database and bootstrap again."
        )
    op.create_table(
        "names",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("parent", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_names")),
        sa.UniqueConstraint("kind", "parent", "name", name=op.f("uq_names_kind_parent_name")),
    )
    op.alter_column("org_accounts", "org_name", new_column_name="org_id")
    op.execute(
        "ALTER TABLE org_accounts RENAME CONSTRAINT uq_org_accounts_org_name "
        "TO uq_org_accounts_org_id"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE org_accounts RENAME CONSTRAINT uq_org_accounts_org_id "
        "TO uq_org_accounts_org_name"
    )
    op.alter_column("org_accounts", "org_id", new_column_name="org_name")
    op.drop_table("names")
