"""A contestant's files on their way into the platform, which never pass
through it: the browser asks for a slot, sends the bytes straight to the
object store, and says when they are there (`forge.domain.uploads` holds the
rules).

`slot` needs the person to be able to submit to the task now
(`submitters.refuse`), the input to be one of the task's contestant inputs a
file is uploaded for, the file name to be one plain name the input
`accept`s, and the declared size to be within the input's `max_size` and the
task's `limits.max_size` (`too_large`, naming the limit), and the person to
hold fewer open uploads for the task than `rules.OPEN_MAX`, declaring with
this one at most `rules.open_bytes` together (`upload_limit`), counted under
a lock on the person and the task so two slots asked at once cannot both
pass. It records the upload in `uploads` and answers with a form for one
request, or with a URL for each part of a larger file. `complete` measures what arrived, once the
parts are joined, and keeps its size and SHA-256 on the row: an upload of
the size declared is `verified`, and one of any other is `rejected` and is
never submitted. Both are answered with where the upload stands, the same
every time they are asked again.

An upload is its owner's alone: every read of one is by its id, its owner
and its task together, and one that is someone else's is no such upload.
`sweep`, the hourly `uploads.sweep` pass, removes each upload no submit used
once its lifetime is over, object and row, and the object of one a submit
did use, keeping the row.

What the store says when it fails, S3's error codes among it, goes to the
log; the caller is told only that the store did not answer, or that it
refused the platform (`store_failure`).
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text

from forge.db.tables import Upload as UploadRow
from forge.domain import uploads as rules
from forge.domain.definitions import SUBMISSION_CEILING, ContestantInput
from forge.domain.errors import (
    InvalidInputs,
    Misconfigured,
    NotFound,
    PortError,
    Rejected,
    TooLarge,
    Unavailable,
    UploadLimit,
    UploadNotReady,
)
from forge.domain.ids import TaskId, new_id
from forge.domain.sessions import Session
from forge.domain.uploads import UploadStatus
from forge.domain.workflow_definition import InputType
from forge.log import get_logger
from forge.port.objects import FinishedPart, Store
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import sessions, submitters

log = get_logger(__name__)

PURPOSE = "submission"
SWEEP_BATCH = 200
NO_SUCH_UPLOAD = "There is no such upload."
STORE_UNAVAILABLE = "The file store did not answer; try again in a moment."
STORE_MISCONFIGURED = "The file store refused the platform's own key."
UPLOAD_LOCK = 0x55504C44
"""The first key of every lock on a person's slots for a task, the second
being the person and task hashed by Postgres."""


@dataclass(frozen=True, slots=True)
class PostSlot:
    """A slot for a file sent in one request: the form to post it with, the
    fields to send before the file, as they are, and when the form stops
    working.
    """

    id: uuid.UUID
    url: str
    fields: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SlotPart:
    """One part of a file sent in parts: its number, from 1, and the URL it
    is sent to, which takes exactly its share of the file.
    """

    number: int
    url: str


@dataclass(frozen=True, slots=True)
class PartsSlot:
    """A slot for a file sent in parts: every part but the last is
    `part_size` bytes, each sent to its own URL, until `expires_at`.
    """

    id: uuid.UUID
    part_size: int
    parts: tuple[SlotPart, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Upload:
    """Where one upload stands: the input and task it is for, its name and
    content type, the size declared and, once measured, the size and
    SHA-256 in hex of what arrived.
    """

    id: uuid.UUID
    task: TaskId
    input: str
    filename: str
    content_type: str | None
    declared_size: int
    size: int | None
    sha256: str | None
    status: UploadStatus


@action
async def slot(
    ctx: Context,
    session: Session,
    task: TaskId,
    *,
    input: str,
    filename: str,
    size: int,
    content_type: str | None = None,
) -> PostSlot | PartsSlot:
    """A slot for one file of `size` bytes named `filename` for the task's
    contestant input `input`, recorded as an upload of the signed-in person.
    """
    entrant = await submitters.entrant(ctx, session, task)
    await submitters.refuse(ctx, entrant)
    definition = entrant.published.definition
    entry = _file_input(definition.inputs.contestant, input)
    _refuse_file(entry, filename, size, content_type)
    limit, whose = _limit(entry, definition.limits.max_size)
    if size > limit:
        raise TooLarge(
            f"The file is larger than the {limit} bytes allowed.", limit=limit, input=whose
        )
    await _refuse_open(
        ctx,
        entrant.session.user_id,
        task,
        size,
        rules.open_bytes(min(definition.limits.max_size, SUBMISSION_CEILING)),
    )
    upload = new_id()
    key = rules.object_key(upload)
    row = UploadRow(
        id=upload,
        owner_user_id=entrant.session.user_id,
        purpose=PURPOSE,
        task_id=task,
        input_id=entry.id,
        object_key=key,
        filename=filename,
        content_type=content_type,
        declared_size=size,
        status=UploadStatus.PRESIGNED,
        expires_at=ctx.now + rules.LIFETIME,
    )
    answer: PostSlot | PartsSlot
    if rules.in_one_request(size):
        form = ctx.forge.objects.upload_form(key, max_size=size, expires_in=rules.FORM_TTL)
        answer = PostSlot(upload, form.url, dict(form.fields), ctx.now + rules.FORM_TTL)
    else:
        try:
            row.multipart_upload_id = await ctx.forge.objects.start_parts(key)
        except PortError as exc:
            raise store_failure(exc, "uploads.start_failed", upload=str(upload)) from None
        parts = tuple(
            SlotPart(
                part.number,
                ctx.forge.objects.part_url(
                    key,
                    row.multipart_upload_id,
                    part.number,
                    length=part.length,
                    expires_in=rules.PARTS_TTL,
                ),
            )
            for part in rules.parts_of(size)
        )
        answer = PartsSlot(upload, rules.part_size(size), parts, ctx.now + rules.PARTS_TTL)
    ctx.db.add(row)
    await ctx.db.flush()
    log.info(
        "uploads.slot",
        upload=str(upload),
        task=task,
        input=entry.id,
        size=size,
        parts=isinstance(answer, PartsSlot),
        user_id=entrant.session.user_id,
    )
    return answer


@action
async def complete(
    ctx: Context,
    session: Session,
    task: TaskId,
    upload: uuid.UUID,
    *,
    parts: Sequence[FinishedPart] = (),
) -> Upload:
    """The signed-in person says the upload's bytes are there, with each part
    and the value the store answered it with for a file sent in parts. The
    upload is measured once and answered with where it stands.
    """
    fresh = await sessions.authenticate(ctx, session.id)
    rows = await owned(ctx, fresh.user_id, task, [upload])
    row = rows.get(upload)
    if row is None:
        raise NotFound(NO_SUCH_UPLOAD)
    if row.status != UploadStatus.PRESIGNED:
        return view(row)
    if row.multipart_upload_id is not None:
        await _join(ctx, row, parts)
    try:
        measured = await ctx.forge.objects.measure(Store.UPLOADS, row.object_key)
    except PortError as exc:
        raise store_failure(exc, "uploads.measure_failed", upload=str(upload)) from None
    if measured is None:
        raise UploadNotReady("The file has not arrived yet.", uploads=[str(upload)])
    row.actual_size = measured.size
    row.digest = measured.sha256
    row.multipart_upload_id = None
    row.status = (
        UploadStatus.VERIFIED if measured.size == row.declared_size else UploadStatus.REJECTED
    )
    await ctx.db.flush()
    log.info(
        "uploads.completed",
        upload=str(upload),
        status=row.status,
        size=measured.size,
        declared=row.declared_size,
        user_id=fresh.user_id,
    )
    return view(row)


async def owned(
    ctx: Context, user_id: int, task: TaskId, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, UploadRow]:
    """The uploads among `ids` that the person made for the task, held until
    the unit of work ends. One made by someone else, for another task, or
    not at all is left out.
    """
    if not ids:
        return {}
    rows = (
        await ctx.db.execute(
            select(UploadRow)
            .where(
                UploadRow.id.in_(list(ids)),
                UploadRow.owner_user_id == user_id,
                UploadRow.task_id == task,
                UploadRow.purpose == PURPOSE,
            )
            .order_by(UploadRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {row.id: row for row in rows}


def store_failure(exc: PortError, event: str, **fields: str) -> PortError:
    """The error a caller is given for a failure of the object store: that it
    did not answer, or that it refused the platform's own key, in fixed
    words. What the store said goes to the log as `event`.
    """
    log.warning(event, error=type(exc).__name__, detail=exc.detail, **fields)
    if isinstance(exc, Misconfigured):
        return Misconfigured(STORE_MISCONFIGURED)
    return Unavailable(STORE_UNAVAILABLE)


def view(row: UploadRow) -> Upload:
    return Upload(
        id=row.id,
        task=TaskId(row.task_id or ""),
        input=row.input_id or "",
        filename=row.filename,
        content_type=row.content_type,
        declared_size=row.declared_size,
        size=row.actual_size,
        sha256=row.digest.hex() if row.digest is not None else None,
        status=UploadStatus(row.status),
    )


async def sweep(ctx: Context) -> int:
    """Remove every upload no submit used whose lifetime is over, its object
    and its row, and the object of every one a submit used, keeping its row
    as `expired`. Returns how many were swept. It takes them `SWEEP_BATCH` at
    a time until none is left, so a busy day never outruns it; an object the
    store fails on is left for the next pass.
    """
    swept = 0
    failed: set[uuid.UUID] = set()
    while True:
        taken = select(UploadRow).where(
            UploadRow.expires_at <= ctx.now, UploadRow.status != UploadStatus.EXPIRED
        )
        if failed:
            taken = taken.where(UploadRow.id.not_in(failed))
        due = (
            await ctx.db.execute(
                taken.order_by(UploadRow.expires_at, UploadRow.id)
                .limit(SWEEP_BATCH)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
        batch = list(due)
        for row in batch:
            if await _swept(ctx, row):
                swept += 1
            else:
                failed.add(row.id)
        await ctx.db.flush()
        if len(batch) < SWEEP_BATCH:
            break
    if swept:
        log.info("uploads.swept", count=swept)
    return swept


async def _swept(ctx: Context, row: UploadRow) -> bool:
    """Remove the upload's object, and its row unless a submit used it;
    whether the store let it.
    """
    try:
        if row.multipart_upload_id is not None:
            await ctx.forge.objects.abandon_parts(row.object_key, row.multipart_upload_id)
        await ctx.forge.objects.delete(Store.UPLOADS, row.object_key)
    except PortError as exc:
        log.warning("uploads.sweep_failed", upload=str(row.id), error=type(exc).__name__)
        return False
    if row.status == UploadStatus.CONSUMED:
        row.status = UploadStatus.EXPIRED
        row.multipart_upload_id = None
    else:
        await ctx.db.delete(row)
    return True


async def _refuse_open(
    ctx: Context, user_id: int, task: TaskId, size: int, most_bytes: int
) -> None:
    """Refuse a slot that would take the person past what they may hold for
    the task before a submit uses it, once any other slot of theirs for the
    task has finished, and hold the next one off until this unit of work
    ends.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:target))"),
        {"space": UPLOAD_LOCK, "target": f"{user_id}|{task}"},
    )
    count, summed = (
        await ctx.db.execute(
            select(func.count(), func.coalesce(func.sum(UploadRow.declared_size), 0)).where(
                UploadRow.owner_user_id == user_id,
                UploadRow.task_id == task,
                UploadRow.purpose == PURPOSE,
                UploadRow.status.in_([status.value for status in rules.OPEN]),
            )
        )
    ).one()
    declared = int(summed)
    if count >= rules.OPEN_MAX or declared + size > most_bytes:
        log.info(
            "uploads.limit",
            task=task,
            count=count,
            declared=declared,
            size=size,
            user_id=user_id,
        )
        raise UploadLimit(
            f"You hold as many uploads for this task as you may before submitting them: "
            f"{rules.OPEN_MAX} files, or {most_bytes} bytes together. Submit them, or wait "
            f"for them to be cleared two days after each was made.",
            limit=rules.OPEN_MAX,
            bytes=most_bytes,
        )


def _file_input(declared: Sequence[ContestantInput], input: str) -> ContestantInput:
    for entry in declared:
        if entry.id == input and entry.type in rules.FILE_INPUTS:
            return entry
    raise InvalidInputs(
        "The task has no input a file is uploaded for by that name.",
        errors=[{"input": input, "message": "The task has no file input by that name."}],
    )


def _refuse_file(
    entry: ContestantInput, filename: str, size: int, content_type: str | None
) -> None:
    problem = rules.filename_problem(filename)
    if problem is None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        problem = "A size is a whole number of bytes."
    if problem is None and content_type is not None and len(content_type) > rules.CONTENT_TYPE_MAX:
        problem = "A content type is at most 255 characters."
    if (
        problem is None
        and entry.type is not InputType.CODE
        and not rules.accepts(entry.accept, filename, content_type)
    ):
        problem = f"This input takes {', '.join(entry.accept or ())}."
    if problem is not None:
        raise InvalidInputs(problem, errors=[{"input": entry.id, "message": problem}])


def _limit(entry: ContestantInput, task_limit: int) -> tuple[int, str | None]:
    """The most a file for the input may be, and the input whose limit that
    is, or none when it is the task's, never above `SUBMISSION_CEILING`.
    """
    task_limit = min(task_limit, SUBMISSION_CEILING)
    if entry.max_size is not None and entry.max_size < task_limit:
        return entry.max_size, entry.id
    return task_limit, None


async def _join(ctx: Context, row: UploadRow, parts: Sequence[FinishedPart]) -> None:
    """Join the parts of a file sent in parts. A join the store already made,
    whose record here was lost, is found by the measure that follows.
    """
    assert row.multipart_upload_id is not None
    expected = [part.number for part in rules.parts_of(row.declared_size)]
    if [part.number for part in parts] != expected:
        raise UploadNotReady(
            "Every part of the file is needed, once each and in order.", uploads=[str(row.id)]
        )
    try:
        await ctx.forge.objects.finish_parts(row.object_key, row.multipart_upload_id, parts)
    except NotFound:
        return
    except Misconfigured as exc:
        raise store_failure(exc, "uploads.join_failed", upload=str(row.id)) from None
    except Rejected as exc:
        log.info("uploads.join_refused", upload=str(row.id), detail=exc.detail)
        raise UploadNotReady(
            "The parts named are not the ones that arrived.", uploads=[str(row.id)]
        ) from None
    except PortError as exc:
        raise store_failure(exc, "uploads.join_failed", upload=str(row.id)) from None
