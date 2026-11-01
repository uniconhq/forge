"""Resumable organiser uploads; only Forgejo-verified transfers enter a save."""

import asyncio
import uuid
from collections.abc import Sequence
from contextlib import aclosing, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.sql.elements import ColumnElement

from forge.db.tables import PartUpload, Upload
from forge.domain import uploads as rules
from forge.domain.content import check_path
from forge.domain.errors import (
    Conflict,
    Forbidden,
    InvalidInputs,
    NotFound,
    PortError,
    Rejected,
    TooLarge,
    UploadLimit,
)
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import TaskId, WorkflowId, new_id
from forge.domain.roles import Role, task_scope
from forge.domain.sessions import Session
from forge.domain.uploads import UploadPurpose, UploadStatus
from forge.port.objects import Part
from forge.port.uploads import TaskPlace, WorkflowPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import sessions
from forge.services.access import Organiser, require

ACTIVE = ("open", "finishing")
SIGN_LIFETIME = timedelta(hours=1)
FINISH_LIMIT = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class Transfer:
    id: uuid.UUID
    path: str
    sha256: str
    size: int
    part_size: int
    status: str
    reason: str | None
    deadline_at: datetime
    parts: tuple[Part, ...] = ()


def _unfinished(ctx: Context) -> ColumnElement[bool]:
    pending = exists(
        select(Upload.id).where(
            Upload.id == PartUpload.id,
            Upload.status == UploadStatus.VERIFIED,
            Upload.expires_at > ctx.now,
        )
    )
    return or_(PartUpload.status.in_(ACTIVE), and_(PartUpload.status == "stored", pending))


def _place(row: PartUpload) -> TaskPlace | WorkflowPlace:
    if row.task_id is not None:
        return TaskPlace(TaskId(row.task_id))
    assert row.workflow_id is not None
    return WorkflowPlace(WorkflowId(row.workflow_id))


def _view(row: PartUpload, parts: Sequence[Part] = ()) -> Transfer:
    return Transfer(
        row.id,
        row.path,
        row.digest,
        row.size,
        row.part_size,
        row.status,
        row.reason,
        row.deadline_at,
        tuple(parts),
    )


async def _permission(ctx: Context, as_: AsUser, place: TaskPlace | WorkflowPlace) -> None:
    if isinstance(place, TaskPlace):
        if not await ctx.forge.content.may_write(place.task, as_.user_id):
            raise Forbidden("You no longer have write access to this task.")
    else:
        await ctx.forge.workflows.require_workflow_write(as_, place.workflow)


@action
async def start_task(
    ctx: Context, organiser: Organiser, task: TaskId, *, path: str, digest: str, size: int
) -> Transfer:
    require(organiser, task_scope(task), Role.MANAGER)
    return await start(
        ctx, organiser.identity, TaskPlace(task), path=path, digest=digest, size=size
    )


async def start(
    ctx: Context,
    as_: AsUser,
    place: TaskPlace | WorkflowPlace,
    *,
    path: str,
    digest: str,
    size: int,
) -> Transfer:
    path = check_path(path)
    problem = rules.digest_problem(digest)
    if problem:
        raise InvalidInputs(problem)
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise InvalidInputs("A size is a positive whole number of bytes.")
    if size > ctx.settings.organiser_file_max:
        raise TooLarge(
            f"A file is at most {ctx.settings.organiser_file_max} bytes.",
            limit=ctx.settings.organiser_file_max,
        )
    await _permission(ctx, as_, place)
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:owner))"),
        {"space": 0x50415254, "owner": str(as_.user_id)},
    )
    rows = list(
        await ctx.db.scalars(
            select(PartUpload)
            .where(PartUpload.owner_user_id == as_.user_id, _unfinished(ctx))
            .order_by(PartUpload.id)
            .with_for_update()
        )
    )
    for row in rows:
        await _expire(ctx, row)
        if (
            row.status in (*ACTIVE, "stored")
            and _place(row) == place
            and row.path == path
            and row.digest == digest
            and row.size == size
        ):
            return _view(row, await _parts(ctx, row, as_))
    if sum(row.status in ACTIVE for row in rows) >= ctx.settings.open_part_uploads:
        raise UploadLimit(
            "Finish or cancel an open upload first; at most "
            f"{ctx.settings.open_part_uploads} can be open."
        )
    row = PartUpload(
        id=new_id(),
        owner_user_id=as_.user_id,
        task_id=place.task if isinstance(place, TaskPlace) else None,
        workflow_id=place.workflow if isinstance(place, WorkflowPlace) else None,
        path=path,
        digest=digest,
        size=size,
        part_size=ctx.settings.upload_part_size,
        status="open",
        upload_id=None,
        reason=None,
        deadline_at=ctx.now + timedelta(days=1),
    )
    ctx.db.add(row)
    if await ctx.forge.uploads.holds(place, as_=as_, digest=digest, size=size):
        await _stored(ctx, row)
    else:
        row.upload_id = await ctx.forge.objects.create_multipart(str(row.id))
    await ctx.db.flush()
    return _view(row)


async def _mine(
    ctx: Context,
    session: Session,
    upload: uuid.UUID,
    expected: TaskPlace | WorkflowPlace | None = None,
) -> PartUpload:
    fresh = await sessions.authenticate(ctx, session.id)
    row = await ctx.db.scalar(
        select(PartUpload)
        .where(PartUpload.id == upload, PartUpload.owner_user_id == fresh.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or (expected is not None and _place(row) != expected):
        raise NotFound("There is no such upload.")
    await _expire(ctx, row)
    return row


async def _expire(ctx: Context, row: PartUpload) -> None:
    if row.status not in ACTIVE or row.deadline_at >= ctx.now:
        return
    if row.status == "finishing":
        row.status, row.reason = "refused", "The upload was interrupted; send it again."
    else:
        row.status = "aborted"
    await _discard(ctx, row)


async def _discard(ctx: Context, row: PartUpload) -> None:
    if row.upload_id:
        with suppress(PortError):
            await ctx.forge.objects.abort_multipart(str(row.id), row.upload_id)
    with suppress(PortError):
        await ctx.forge.objects.delete_upload(str(row.id))


async def _parts(ctx: Context, row: PartUpload, who: AsUser | Session) -> list[Part]:
    if row.status != "open" or row.upload_id is None:
        return []
    try:
        return await ctx.forge.objects.list_parts(str(row.id), row.upload_id)
    except NotFound:
        await _gone(ctx, row, who)
        return []


async def _gone(ctx: Context, row: PartUpload, who: AsUser | Session) -> None:
    """The multipart upload is no longer there: joined by a completion whose
    answer was lost when the temporary object is there at the declared
    size, so it is stored as any completed one is, and lost otherwise.
    """
    try:
        joined = await ctx.forge.objects.head_upload(str(row.id)) == row.size
    except NotFound:
        joined = False
    if joined:
        as_ = (
            who
            if isinstance(who, AsUser)
            else AsUser(row.owner_user_id, await sessions.credential_for(ctx, who.id))
        )
        _finishing(ctx, row, as_)
        return
    row.status = "refused"
    row.reason = "The temporary upload is no longer available; send it again."
    await _discard(ctx, row)


def _finishing(ctx: Context, row: PartUpload, as_: AsUser) -> None:
    row.status, row.deadline_at = "finishing", ctx.now + FINISH_LIMIT
    upload = row.id

    async def store(later: Context) -> None:
        await _finish(later, upload, as_)

    ctx.in_background(store)


@action
async def read(
    ctx: Context,
    session: Session,
    upload: uuid.UUID,
    *,
    place: TaskPlace | WorkflowPlace | None = None,
) -> Transfer:
    row = await _mine(ctx, session, upload, place)
    parts = await _parts(ctx, row, session)
    return _view(row, parts)


@action
async def opened(
    ctx: Context, session: Session, place: TaskPlace | WorkflowPlace
) -> tuple[Transfer, ...]:
    fresh = await sessions.authenticate(ctx, session.id)
    condition = (
        PartUpload.task_id == place.task
        if isinstance(place, TaskPlace)
        else PartUpload.workflow_id == place.workflow
    )
    rows = list(
        await ctx.db.scalars(
            select(PartUpload)
            .where(PartUpload.owner_user_id == fresh.user_id, condition, _unfinished(ctx))
            .order_by(PartUpload.created_at, PartUpload.id)
            .with_for_update()
        )
    )
    for row in rows:
        await _expire(ctx, row)
    return tuple(_view(row) for row in rows if row.status in (*ACTIVE, "stored"))


@action
async def sign(
    ctx: Context,
    session: Session,
    upload: uuid.UUID,
    numbers: Sequence[int],
    *,
    place: TaskPlace | WorkflowPlace | None = None,
) -> dict[int, str]:
    row = await _mine(ctx, session, upload, place)
    if row.status != "open" or row.upload_id is None:
        raise Conflict("This upload is no longer open.")
    count = (row.size + row.part_size - 1) // row.part_size
    if (
        not numbers
        or len(numbers) > 1000
        or any(isinstance(number, bool) or not 1 <= number <= count for number in numbers)
    ):
        raise InvalidInputs("Ask only for part numbers within this file's declared size.")
    return {
        number: ctx.forge.objects.part_url(
            str(row.id), row.upload_id, number, expires_in=SIGN_LIFETIME
        )
        for number in numbers
    }


@action
async def finish(
    ctx: Context,
    session: Session,
    upload: uuid.UUID,
    parts: Sequence[tuple[int, str]],
    *,
    place: TaskPlace | WorkflowPlace | None = None,
) -> Transfer:
    row = await _mine(ctx, session, upload, place)
    if row.status != "open":
        return _view(row)
    assert row.upload_id is not None
    actual = await _parts(ctx, row, session)
    if row.status != "open":
        return _view(row)
    count = (row.size + row.part_size - 1) // row.part_size
    valid = [part.number for part in actual] == list(range(1, count + 1))
    valid = valid and sum(part.size for part in actual) == row.size
    valid = valid and all(
        part.size == min(row.part_size, row.size - (part.number - 1) * row.part_size)
        for part in actual
    )
    if not valid:
        row.status, row.reason = (
            "refused",
            "The uploaded parts do not match the declared file size. Send it again.",
        )
        await _discard(ctx, row)
        return _view(row)
    if [(part.number, part.etag) for part in actual] != sorted(parts):
        # The store's parts are whole; the browser's list is behind them.
        raise Conflict("The parts sent are not the parts the store holds. Read the upload again.")
    as_ = AsUser(row.owner_user_id, await sessions.credential_for(ctx, session.id))
    try:
        await ctx.forge.objects.complete_multipart(str(row.id), row.upload_id, actual)
    except NotFound:
        # A retried completion finds the upload its first attempt joined.
        await _gone(ctx, row, as_)
        return _view(row)
    _finishing(ctx, row, as_)
    return _view(row)


async def _finish(ctx: Context, upload: uuid.UUID, as_: AsUser) -> None:
    row = await ctx.db.get(PartUpload, upload)
    if row is None or row.status != "finishing":
        return
    place = _place(row)
    # The background work owns this transaction; do not hold a connection while streaming.
    await ctx.db.commit()
    try:
        async with asyncio.timeout(FINISH_LIMIT.total_seconds()):
            await _permission(ctx, as_, place)
            async with aclosing(ctx.forge.objects.stream_upload(str(upload))) as content:
                await ctx.forge.uploads.store(
                    place, as_=as_, digest=row.digest, size=row.size, content=content
                )
            # Asked as the platform: the person's credential may have expired
            # while a large file streamed.
            if not await ctx.forge.uploads.holds(
                place, as_=PLATFORM, digest=row.digest, size=row.size
            ):
                raise Rejected("Forgejo did not store the declared file.")
        row = await ctx.db.scalar(
            select(PartUpload)
            .where(PartUpload.id == upload)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None and row.status == "finishing":
            await _expire(ctx, row)
            if row.status == "finishing":
                await _stored(ctx, row)
                # Forgejo holds the file; the bucket's lifecycle rule clears a
                # temporary object left behind.
                with suppress(PortError):
                    await ctx.forge.objects.delete_upload(str(upload))
    except (PortError, TimeoutError) as error:
        row = await ctx.db.scalar(
            select(PartUpload)
            .where(PartUpload.id == upload)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None and row.status == "finishing":
            row.status = "refused"
            row.reason = (
                error.detail[:1000]
                if isinstance(error, PortError)
                else "Storing the upload took too long; send it again."
            )
            await _discard(ctx, row)
        return


async def _stored(ctx: Context, row: PartUpload) -> None:
    row.status = "stored"
    ctx.db.add(
        Upload(
            id=row.id,
            owner_user_id=row.owner_user_id,
            purpose=UploadPurpose.TASK_FILE if row.task_id else UploadPurpose.WORKFLOW_FILE,
            task_id=row.task_id,
            workflow_id=row.workflow_id,
            input_id=None,
            repo_path=row.path,
            filename=row.path.rsplit("/", 1)[-1],
            content_type=None,
            size=row.size,
            digest=row.digest,
            status=UploadStatus.VERIFIED,
            consumed_by=None,
            expires_at=ctx.now + rules.LIFETIME,
        )
    )


@action
async def abort(
    ctx: Context,
    session: Session,
    upload: uuid.UUID,
    *,
    place: TaskPlace | WorkflowPlace | None = None,
) -> Transfer:
    row = await _mine(ctx, session, upload, place)
    if row.status in ("aborted", "refused"):
        return _view(row)
    stored = await ctx.db.get(Upload, upload)
    if stored is not None:
        if stored.status == UploadStatus.CONSUMED:
            raise Conflict("That file has already been saved.")
        await ctx.db.delete(stored)
    row.status = "aborted"
    await _discard(ctx, row)
    return _view(row)
