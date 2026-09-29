"""A contestant's workspace and the place they submit each task to as kinds of
job, the workspace a contestant's row points at once it is opened, and a
task's registration for grading renamed to its activation at the CI. Going
back removes the workspace and submission place jobs, which nothing before
it makes, and the workspace column, and names the activations registrations
again.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 09:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_BEFORE = "kind in ('org', 'contest', 'task', 'workspace', 'registration')"
KINDS_AFTER = "kind in ('org', 'contest', 'task', 'workspace', 'submission_place', 'activation')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_provisioning_kind"), "provisioning", type_="check")
    _rename("registration", "register", "activation", "activate")
    op.create_check_constraint(op.f("ck_provisioning_kind"), "provisioning", KINDS_AFTER)
    op.add_column("contestants", sa.Column("workspace_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("contestants", "workspace_id")
    op.drop_constraint(op.f("ck_provisioning_kind"), "provisioning", type_="check")
    op.execute("DELETE FROM provisioning WHERE kind IN ('workspace', 'submission_place')")
    _rename("activation", "activate", "registration", "register")
    op.create_check_constraint(op.f("ck_provisioning_kind"), "provisioning", KINDS_BEFORE)


def _rename(kind: str, step: str, new_kind: str, new_step: str) -> None:
    """Give every job of `kind` the new kind, and its one step the new name
    wherever the job records it.
    """
    op.execute(
        sa.text(
            "UPDATE provisioning SET kind = :new_kind, "
            "last_step = CASE WHEN last_step = :step THEN :new_step ELSE last_step END, "
            "failed_step = CASE WHEN failed_step = :step THEN :new_step ELSE failed_step END "
            "WHERE kind = :kind"
        ).bindparams(kind=kind, step=step, new_kind=new_kind, new_step=new_step)
    )
