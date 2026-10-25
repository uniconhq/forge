"""What a run task's grading keeps of its Woodpecker steps, the store of
step outputs, and the large-file objects a publication's planned files are
(SIMPLER-CORE.md 11.6, 11.9).

`grading_steps` is one row per grading and Woodpecker step, made when the
step's token is minted: the pipeline it runs in, the SHA-256 of its token,
when the token expires, whether the grading reuses nothing (`from_scratch`),
and, once the step has asked, when (`job_at`), the items its job held, each
run with its key (`items`), the files the door serves it by SHA-256,
each with where it is read from and the SHA-256 the plan pinned
(`served`), the first of its box users (`box_first`, unique while held),
the files it said it would upload (`uploads`), when it said it was done
(`done_at`) and, when what it wrote is no outputs, why (`fault`).

`step_outputs` is one row per item a step did: the org whose machine did
it, the item's key, the pipeline it was measured in, the grading and step
that did it and the run, what `outputs.json` said of it (`outputs`) and the
files those name, each with its object in the results store, its SHA-256
and size (`files`). The first row of an org's key in a pipeline is the one
reused (`reusable`, unique among those), until a grading from scratch
keeps one of that key, which is reused in its place; any other is kept
only for the grading that made it.

`grading_reuses` is, per grading and key, the `step_outputs` row the
grading's first read of that key in the store resolved to: every later
read of the key by that grading takes the same row, so a grading from
scratch that makes another row the reused one of the key changes nothing
for a grading already under way.

`publication_objects` is the SHA-256 and size of every large-file object a
publication's planned files are, the task's and its workflow's, read once:
the only hashes the forge signs a link into the large-file store for.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.db.base import Base, Timestamped
from forge.domain.ids import new_id
from forge.domain.run_plan import PIPELINES


class GradingStep(Base, Timestamped):
    __tablename__ = "grading_steps"

    grading_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(primary_key=True)
    pipeline: Mapped[str]
    token_hash: Mapped[bytes]
    expires_at: Mapped[datetime]
    from_scratch: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    job_at: Mapped[datetime | None]
    items: Mapped[list[Any] | None] = mapped_column(JSONB)
    served: Mapped[dict[str, Any] | None]
    box_first: Mapped[int | None]
    uploads: Mapped[dict[str, Any] | None]
    done_at: Mapped[datetime | None]
    fault: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(f"pipeline in {PIPELINES}", name="pipeline"),
        Index(
            "ix_grading_steps_box_first",
            "box_first",
            unique=True,
            postgresql_where=text("box_first is not null"),
        ),
    )


class StepOutput(Base):
    __tablename__ = "step_outputs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    org_id: Mapped[str]
    key: Mapped[str]
    pipeline: Mapped[str]
    reusable: Mapped[bool]
    grading_id: Mapped[uuid.UUID]
    step: Mapped[str]
    run: Mapped[str | None]
    outputs: Mapped[dict[str, Any]]
    files: Mapped[dict[str, Any]]
    measured_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint(f"pipeline in {PIPELINES}", name="pipeline"),
        Index(
            "ix_step_outputs_org_id_key_pipeline",
            "org_id",
            "key",
            "pipeline",
            unique=True,
            postgresql_where=text("reusable"),
        ),
        Index("ix_step_outputs_grading_id", "grading_id"),
    )


class PublicationObjects(Base):
    __tablename__ = "publication_objects"

    publication_id: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str]
    objects: Mapped[dict[str, Any]]
    read_at: Mapped[datetime]


class GradingReuse(Base):
    __tablename__ = "grading_reuses"

    grading_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    output_id: Mapped[uuid.UUID]
