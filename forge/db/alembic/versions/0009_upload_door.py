"""Uploads go through the forge's large-file store instead of the platform's
own bucket, so the row keeps what was declared and nothing about an object
in a bucket the platform signs for.

`object_key` and `multipart_upload_id` go with the bucket and the upload in
parts. `declared_size` becomes `size` and `actual_size` goes: the forge
refuses anything whose length is not the one its address names, so there is
no second size to keep. `digest` becomes the SHA-256 in hex, declared before
the upload rather than measured after it, and is no longer nullable.
`repo_path` arrives for a file an organiser puts into a task.

The statuses lose `rejected`, which cannot happen any more, and `presigned`
becomes `waiting`, since nothing is signed for. The purposes lose `asset`,
which nothing wrote, and gain `task_file`.

Rows written before this are of the old mechanism: their bytes are in the
platform's bucket, which no code reads after this, so only the ones a
submission took are kept, as the record of what was submitted, with their
measured size and digest carried across. A row is one of those when it names
the submission that took it; `consumed` alone does not say so, since 0007
rewrote the old `expired`, which meant an upload nobody ever submitted, to
the same word. One that was never measured goes too, since the digest is the
record and there is none to carry.

Going back restores the columns but not the bucket's objects, so an upload
in flight at either crossing is asked for again: every row the old shape has
no word for is removed before the old words are held to.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-03 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSES = "purpose in ('submission', 'task_file')"
STATUSES = "status in ('waiting', 'verified', 'consumed')"
OLD_PURPOSES = "purpose in ('submission', 'asset')"
OLD_STATUSES = "status in ('presigned', 'verified', 'consumed', 'rejected')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_uploads_purpose"), "uploads", type_="check")
    op.drop_constraint(op.f("ck_uploads_status"), "uploads", type_="check")

    # An upload no submission took named bytes in the platform's bucket,
    # which nothing reads after this. One a submission took is the record of
    # what was submitted and stays, with what was measured of it at the time.
    op.execute(sa.text("DELETE FROM uploads WHERE status <> 'consumed' OR consumed_by IS NULL"))

    op.add_column("uploads", sa.Column("repo_path", sa.Text(), nullable=True))
    op.alter_column("uploads", "declared_size", new_column_name="size")
    op.execute(sa.text("UPDATE uploads SET size = coalesce(actual_size, size)"))
    op.drop_column("uploads", "actual_size")
    op.drop_column("uploads", "object_key")
    op.drop_column("uploads", "multipart_upload_id")

    op.add_column("uploads", sa.Column("digest_hex", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE uploads SET digest_hex = encode(digest, 'hex')"))
    # A row the old mechanism never measured records nothing the submission's
    # own commit does not already hold, and there is no digest to give it.
    op.execute(sa.text("DELETE FROM uploads WHERE digest_hex IS NULL"))
    op.drop_column("uploads", "digest")
    op.alter_column("uploads", "digest_hex", new_column_name="digest", nullable=False)

    op.create_check_constraint(op.f("ck_uploads_purpose"), "uploads", PURPOSES)
    op.create_check_constraint(op.f("ck_uploads_status"), "uploads", STATUSES)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_uploads_purpose"), "uploads", type_="check")
    op.drop_constraint(op.f("ck_uploads_status"), "uploads", type_="check")

    # The old shape has no word for an upload that is waiting, nor for a file
    # of a task, and its objects are in a bucket this goes back to a platform
    # that reads. Neither can be carried over, so neither is kept.
    op.execute(sa.text("DELETE FROM uploads WHERE status <> 'consumed' OR purpose <> 'submission'"))

    op.add_column("uploads", sa.Column("digest_bytes", sa.LargeBinary(), nullable=True))
    op.execute(sa.text("UPDATE uploads SET digest_bytes = decode(digest, 'hex')"))
    op.drop_column("uploads", "digest")
    op.alter_column("uploads", "digest_bytes", new_column_name="digest")

    op.add_column("uploads", sa.Column("multipart_upload_id", sa.Text(), nullable=True))
    op.add_column("uploads", sa.Column("object_key", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE uploads SET object_key = 'uploads/' || id::text"))
    op.alter_column("uploads", "object_key", nullable=False)
    op.create_unique_constraint(op.f("uq_uploads_object_key"), "uploads", ["object_key"])
    op.add_column("uploads", sa.Column("actual_size", sa.BigInteger(), nullable=True))
    op.execute(sa.text("UPDATE uploads SET actual_size = size"))
    op.alter_column("uploads", "size", new_column_name="declared_size")
    op.drop_column("uploads", "repo_path")

    op.create_check_constraint(op.f("ck_uploads_purpose"), "uploads", OLD_PURPOSES)
    op.create_check_constraint(op.f("ck_uploads_status"), "uploads", OLD_STATUSES)
