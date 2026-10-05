"""A person's files on their way into the platform, which never pass through
it: the browser works out the file's digest, asks for a slot, sends the bytes
through the upload door to the forge's large-file store, and says when they
are there (`forge.domain.uploads` holds the rules).

`slot` needs the person to be able to submit to the task now
(`submitters.refuse`), the input to be one of the task's contestant inputs a
file is uploaded for, the file name to be one plain name the input `accept`s,
the digest to be a SHA-256, and the declared size to be within the input's
`max_size` and the task's `limits.max_size` (`too_large`, naming the limit),
and the person to hold fewer open uploads for the task than `rules.OPEN_MAX`,
declaring with this one at most `rules.open_bytes` together (`upload_limit`),
counted under a lock on the person and the task so two slots asked at once
cannot both pass. It makes the place the bytes will belong to if it is not
there yet, because an object belongs to a repository at the forge and there
has to be one to put it in; then, if the place already holds that object, it
records the upload as checked and asks for nothing, which is what makes
sending the same file again free.

`door` is asked by the proxy, once per upload, before a byte of the body is
read: it says where the bytes go and what credential to present, for an
upload of the signed-in person's that is still waiting and whose declared
length is the one the request carries. Everything it refuses is a 403 with
no reason, since the browser learns where the upload stands by asking for
it. The credential it answers with is the person's own, so the forge's check
of their write access to the place is the backstop under the platform's.

`complete` asks the forge whether the place holds the object. It never
answers that an upload was rejected: the forge keeps nothing that did not
hash to the digest in its address, so an upload either arrived as declared
or did not arrive.

An upload is its owner's alone: every read of one is by its id, its owner and
its task together, and one that is someone else's is no such upload. Before a
slot is counted, the person's own uploads that no submit used and whose
lifetime is over lose their rows, so they stop counting against what the
person may hold; their bytes are the forge's to collect once no commit names
them.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text

from forge.db.tables import Upload as UploadRow
from forge.domain import uploads as rules
from forge.domain.content import check_path
from forge.domain.definitions import FILE_CEILING, SUBMISSION_CEILING, ContestantInput
from forge.domain.errors import (
    Forbidden,
    InvalidInputs,
    NotFound,
    PortError,
    Rejected,
    TooLarge,
    Unavailable,
    UploadLimit,
    UploadNotReady,
)
from forge.domain.identity import AsUser
from forge.domain.ids import TaskId, new_id
from forge.domain.roles import Role, task_scope
from forge.domain.sessions import Session
from forge.domain.uploads import Door, UploadPurpose, UploadStatus
from forge.domain.workflow_definition import InputType
from forge.log import get_logger
from forge.port.uploads import SubmissionPlace, TaskPlace, UploadPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import sessions, submitters
from forge.services.access import Organiser, require

log = get_logger(__name__)

NO_SUCH_UPLOAD = "There is no such upload."
FORGE_UNAVAILABLE = "The file store did not answer; try again in a moment."
FORGE_REFUSED = "The file store will not take this file from you."
NO_SUCH_PLACE = "There is nowhere for that file to go."
DOOR_REFUSED = "That upload cannot be sent."
UPLOAD_LOCK = 0x55504C44
"""The first key of every lock on a person's slots for a task, the second
being the person and task hashed by Postgres."""


@dataclass(frozen=True, slots=True)
class Slot:
    """A slot for one file: where to send it, and when the slot stops being
    good. `ready` is true for a file the forge already holds, which is sent
    again by sending nothing.
    """

    id: uuid.UUID
    url: str | None
    expires_at: datetime
    ready: bool


@dataclass(frozen=True, slots=True)
class Upload:
    """Where one upload stands: the input and task it is for, its name and
    content type, the size and digest declared for it, and its status.
    """

    id: uuid.UUID
    task: TaskId
    input: str
    filename: str
    content_type: str | None
    size: int
    sha256: str
    status: UploadStatus


def door_url(upload: uuid.UUID) -> str:
    """Where the browser sends one upload's bytes. The proxy matches this
    path, asks `door` whether the upload may start, and puts the body
    through to the forge; nothing of the browser's request decides where the
    bytes land.
    """
    return f"/-/uploads/{upload}"


@action
async def slot(
    ctx: Context,
    session: Session,
    task: TaskId,
    *,
    input: str,
    filename: str,
    size: int,
    sha256: str,
    content_type: str | None = None,
) -> Slot:
    """A slot for one file of `size` bytes named `filename`, whose content
    hashes to `sha256`, for the task's contestant input `input`, recorded as
    an upload of the signed-in person.
    """
    entrant = await submitters.entrant(ctx, session, task)
    _, workspace = await submitters.refuse(ctx, entrant)
    definition = entrant.published.definition
    entry = _file_input(definition.inputs.contestant, input)
    _refuse_file(entry, filename, size, sha256, content_type)
    limit, whose = _limit(entry, definition.limits.max_size)
    if size > limit:
        raise TooLarge(
            f"The file is larger than the {limit} bytes allowed.", limit=limit, input=whose
        )
    if not await _asked_before(
        ctx, entrant.session.user_id, task, UploadPurpose.SUBMISSION, since=entrant.since
    ):
        # An object belongs to a repository at the forge, so the place has to
        # be there before any bytes are sent. A row for this task, taken or
        # not, means an earlier slot made it, so a submission of twenty files
        # costs one call between them and not one each. Two first slots at
        # once both make it, which is making it once.
        await submitters.open_place(ctx, entrant)
    await _clear_lapsed(ctx, entrant.session.user_id, UploadPurpose.SUBMISSION)
    await _refuse_open(
        ctx,
        entrant.session.user_id,
        task,
        size,
        rules.open_bytes(min(definition.limits.max_size, SUBMISSION_CEILING)),
    )
    place = SubmissionPlace(workspace, task)
    as_ = AsUser(entrant.session.user_id, await sessions.credential_for(ctx, entrant.session.id))
    return await _record(
        ctx,
        place=place,
        as_=as_,
        purpose=UploadPurpose.SUBMISSION,
        user_id=entrant.session.user_id,
        task=task,
        input_id=entry.id,
        filename=filename,
        content_type=content_type,
        size=size,
        digest=sha256,
    )


@action
async def task_file_slot(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    *,
    path: str,
    size: int,
    sha256: str,
    content_type: str | None = None,
) -> Slot:
    """A slot for one file an organiser puts into the task's own repository
    at `path`. The next save of the task writes the pointer to it there
    (`publications.save`), so nothing is in the repository until then.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    checked = check_path(path)
    name = checked.rsplit("/", 1)[-1]
    problem = rules.filename_problem(name) or rules.digest_problem(sha256)
    if problem is None and (isinstance(size, bool) or not isinstance(size, int) or size < 1):
        problem = "A size is a whole number of bytes."
    if problem is None and size > FILE_CEILING:
        problem = f"A file is at most {FILE_CEILING} bytes."
    if problem is not None:
        raise InvalidInputs(problem, errors=[{"input": path, "message": problem}])
    as_ = organiser.identity
    await _clear_lapsed(ctx, as_.user_id, UploadPurpose.TASK_FILE)
    await _refuse_open(
        ctx, as_.user_id, task, size, rules.open_bytes(SUBMISSION_CEILING), UploadPurpose.TASK_FILE
    )
    return await _record(
        ctx,
        place=TaskPlace(task),
        as_=as_,
        purpose=UploadPurpose.TASK_FILE,
        user_id=as_.user_id,
        task=task,
        input_id=None,
        filename=name,
        content_type=content_type,
        size=size,
        digest=sha256,
        repo_path=checked,
    )


async def for_save(
    ctx: Context, user_id: int, task: TaskId, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, UploadRow]:
    """The task-file uploads among `ids` that this organiser made for this
    task, held until the unit of work ends. One that is someone else's, for
    another task, or not at all is left out.
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
                UploadRow.purpose == UploadPurpose.TASK_FILE,
            )
            .order_by(UploadRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {row.id: row for row in rows}


@action
async def complete(ctx: Context, session: Session, task: TaskId, upload: uuid.UUID) -> Upload:
    """The signed-in person says the upload's bytes are there. The forge is
    asked whether the place holds the object; the answer is the same every
    time it is asked again.
    """
    fresh = await sessions.authenticate(ctx, session.id)
    row = await _mine(ctx, fresh.user_id, task, upload)
    if row is None:
        raise NotFound(NO_SUCH_UPLOAD)
    if row.status != UploadStatus.WAITING:
        return view(row)
    as_ = AsUser(fresh.user_id, await sessions.credential_for(ctx, session.id))
    if not await holds(ctx, await _place_of(ctx, row, fresh.user_id), as_, row):
        raise UploadNotReady("The file has not arrived yet.", uploads=[str(upload)])
    row.status = UploadStatus.VERIFIED
    await ctx.db.flush()
    log.info("uploads.completed", upload=str(upload), size=row.size, user_id=fresh.user_id)
    return view(row)


@action
async def door(ctx: Context, session: Session, upload: uuid.UUID, *, length: int) -> Door:
    """Where one upload's bytes go and what to present there, answered for
    the proxy before it reads the body. Everything that is not this person's
    own waiting upload of exactly this length is `Forbidden` with no reason;
    the log says which it was.
    """
    fresh = await sessions.authenticate(ctx, session.id)
    row = (
        await ctx.db.execute(
            select(UploadRow).where(
                UploadRow.id == upload, UploadRow.owner_user_id == fresh.user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        log.info(
            "uploads.door_refused",
            why="no such upload",
            upload=str(upload),
            user_id=fresh.user_id,
        )
        raise Forbidden(DOOR_REFUSED)
    if row.status != UploadStatus.WAITING:
        log.info(
            "uploads.door_refused",
            why=f"status {row.status}",
            upload=str(upload),
            user_id=fresh.user_id,
        )
        raise Forbidden(DOOR_REFUSED)
    if length != row.size:
        log.info(
            "uploads.door_refused",
            why="length differs from the slot",
            upload=str(upload),
            length=length,
            declared=row.size,
            user_id=fresh.user_id,
        )
        raise Forbidden(DOOR_REFUSED)
    if row.expires_at <= ctx.now:
        # A row lives two days and is cleared at its owner's next slot, so one
        # can outlive its lifetime by a while; the door is where that stops.
        log.info("uploads.door_refused", why="lapsed", upload=str(upload), user_id=fresh.user_id)
        raise Forbidden(DOOR_REFUSED)
    as_ = AsUser(fresh.user_id, await sessions.credential_for(ctx, session.id))
    answer = ctx.forge.uploads.door(
        await _place_of(ctx, row, fresh.user_id), as_=as_, digest=row.digest, size=row.size
    )
    log.info("uploads.door_opened", upload=str(upload), size=row.size, user_id=fresh.user_id)
    return answer


async def holds(ctx: Context, place: UploadPlace, as_: AsUser, row: UploadRow) -> bool:
    """Whether the place holds the upload's object, with a failure of the
    forge told apart from a plain no.
    """
    try:
        return await ctx.forge.uploads.holds(place, as_=as_, digest=row.digest, size=row.size)
    except PortError as exc:
        raise forge_failure(exc, "uploads.verify_failed", upload=str(row.id)) from None


async def holds_object(
    ctx: Context, place: UploadPlace, as_: AsUser, digest: str, size: int
) -> bool:
    """Whether the place holds the object of that digest and size, as
    `holds` asks it.
    """
    try:
        return await ctx.forge.uploads.holds(place, as_=as_, digest=digest, size=size)
    except PortError as exc:
        raise forge_failure(exc, "uploads.verify_failed", digest=digest) from None


async def _mine(ctx: Context, user_id: int, task: TaskId, upload: uuid.UUID) -> UploadRow | None:
    """One upload of this person for this task, whatever it is for, held
    until the unit of work ends."""
    return (
        await ctx.db.execute(
            select(UploadRow)
            .where(
                UploadRow.id == upload,
                UploadRow.owner_user_id == user_id,
                UploadRow.task_id == task,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


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
                UploadRow.purpose == UploadPurpose.SUBMISSION,
            )
            .order_by(UploadRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {row.id: row for row in rows}


def forge_failure(exc: PortError, event: str, **fields: str) -> PortError:
    """The error a caller is given for a failure of the forge's store, in
    fixed words. What the forge said goes to the log as `event`.

    A refusal is kept apart from a failure. The forge refuses a person who
    may no longer write where their upload goes, and telling them the store
    did not answer would send them round a retry that can never come right;
    so is a place that is not there, for the same reason.
    """
    log.warning(event, error=type(exc).__name__, detail=exc.detail, **fields)
    if isinstance(exc, Forbidden | Rejected):
        return Forbidden(FORGE_REFUSED)
    if isinstance(exc, NotFound):
        return NotFound(NO_SUCH_PLACE)
    return Unavailable(FORGE_UNAVAILABLE)


def view(row: UploadRow) -> Upload:
    return Upload(
        id=row.id,
        task=TaskId(row.task_id or ""),
        input=row.input_id or "",
        filename=row.filename,
        content_type=row.content_type,
        size=row.size,
        sha256=row.digest,
        status=UploadStatus(row.status),
    )


async def _record(
    ctx: Context,
    *,
    place: UploadPlace,
    as_: AsUser,
    purpose: UploadPurpose,
    user_id: int,
    task: TaskId,
    input_id: str | None,
    filename: str,
    content_type: str | None,
    size: int,
    digest: str,
    repo_path: str | None = None,
) -> Slot:
    """Write the upload's row and answer with its slot. A place that already
    holds the object needs no upload, and the row is written checked.
    """
    upload = new_id()
    row = UploadRow(
        id=upload,
        owner_user_id=user_id,
        purpose=purpose,
        task_id=task,
        input_id=input_id,
        repo_path=repo_path,
        filename=filename,
        content_type=content_type,
        size=size,
        digest=digest,
        status=UploadStatus.WAITING,
        expires_at=ctx.now + rules.LIFETIME,
    )
    try:
        already = await ctx.forge.uploads.holds(place, as_=as_, digest=digest, size=size)
    except PortError as exc:
        raise forge_failure(exc, "uploads.verify_failed", upload=str(upload)) from None
    if already:
        row.status = UploadStatus.VERIFIED
    ctx.db.add(row)
    await ctx.db.flush()
    log.info(
        "uploads.slot",
        upload=str(upload),
        task=task,
        input=input_id,
        size=size,
        ready=already,
        user_id=user_id,
    )
    return Slot(
        id=upload,
        url=None if already else door_url(upload),
        expires_at=row.expires_at,
        ready=already,
    )


async def _place_of(ctx: Context, row: UploadRow, user_id: int) -> UploadPlace:
    """The repository an upload's bytes belong to: the place to submit the
    task the person works in, their team's while they are in one, or the
    task itself for an organiser's file.
    """
    task = TaskId(row.task_id or "")
    if row.purpose == UploadPurpose.TASK_FILE:
        return TaskPlace(task)
    return SubmissionPlace(await submitters.place_of(ctx, task, user_id), task)


async def _clear_lapsed(ctx: Context, user_id: int, purpose: UploadPurpose) -> None:
    """Remove the rows of the person's uploads of this kind that nothing took
    and whose lifetime is over, so they stop counting against what the person
    may hold. Their bytes are the forge's to collect once no commit names
    them.

    One kind at a time: asking for a slot to submit with must not clear a
    file an organiser uploaded into a task and has not saved yet, which is
    the same person's and would otherwise go for being two days old.
    """
    lapsed = (
        await ctx.db.execute(
            select(UploadRow)
            .where(
                UploadRow.owner_user_id == user_id,
                UploadRow.purpose == purpose,
                UploadRow.expires_at <= ctx.now,
                UploadRow.status != UploadStatus.CONSUMED,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    for row in list(lapsed):
        await ctx.db.delete(row)
    await ctx.db.flush()


async def _asked_before(
    ctx: Context,
    user_id: int,
    task: TaskId,
    purpose: UploadPurpose,
    *,
    since: datetime | None = None,
) -> bool:
    """Whether any slot of this kind was kept for the person at the task,
    taken or not, since `since`, when they joined or left a team, and so
    whether the place they now upload into is made already with them on it.
    Read without a lock: the answer only saves a call to the forge, and a
    place made twice is made once.
    """
    query = select(UploadRow.id).where(
        UploadRow.owner_user_id == user_id,
        UploadRow.task_id == task,
        UploadRow.purpose == purpose,
    )
    if since is not None:
        # Asked for at `expires_at` less the lifetime, by the same clock as
        # `since`; the row's own `created_at` is the database's.
        query = query.where(UploadRow.expires_at > since + rules.LIFETIME)
    found = await ctx.db.scalar(query.limit(1))
    return found is not None


async def _refuse_open(
    ctx: Context,
    user_id: int,
    task: TaskId,
    size: int,
    most_bytes: int,
    purpose: UploadPurpose = UploadPurpose.SUBMISSION,
) -> None:
    """Refuse a slot that would take the person past what they may hold for
    the task before a submit uses it, once any other slot of theirs for the
    task has finished, and hold the next one off until this unit of work
    ends.

    An organiser's files are counted on their own: a person who is both is
    not refused a file for their task because of what they hold to submit.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:target))"),
        {"space": UPLOAD_LOCK, "target": f"{user_id}|{task}|{purpose}"},
    )
    open_statuses = [status.value for status in rules.OPEN]
    count, summed = (
        await ctx.db.execute(
            select(
                func.count().filter(UploadRow.status.in_(open_statuses)),
                func.coalesce(
                    func.sum(UploadRow.size).filter(UploadRow.status.in_(open_statuses)), 0
                ),
            ).where(
                UploadRow.owner_user_id == user_id,
                UploadRow.task_id == task,
                UploadRow.purpose == purpose,
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
    entry: ContestantInput, filename: str, size: int, digest: str, content_type: str | None
) -> None:
    problem = rules.filename_problem(filename)
    if problem is None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        problem = "A size is a whole number of bytes."
    if problem is None:
        problem = rules.digest_problem(digest)
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
    is, or none when it is the task's. Never above `FILE_CEILING`, which is
    what the forge takes for one object: a slot above it would be handed out
    for a file the forge then refuses, with nothing saying why.
    """
    task_limit = min(task_limit, SUBMISSION_CEILING, FILE_CEILING)
    if entry.max_size is not None and entry.max_size < task_limit:
        return entry.max_size, entry.id
    return task_limit, None
