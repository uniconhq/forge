"""A contestant's uploads becoming a submission, and the submissions they read
back. `submit` runs in this order and stops at the first refusal, each with a
code of its own, before anything is written:

1. The task is open by the server's clock plus the contestant's own
   extension, and they are approved (`submitters.refuse`).
2. They have submissions left (`submission_limit`), counted from the
   submissions at the forge, and the task's rate holds (`rate_limited`),
   counted from the grading rows of their submissions within its window.
3. Every upload named is theirs, for this task (`upload_not_yours`), and a
   checked file no submission has used (`upload_not_ready`).
4. Each file is within its input's `max_size`, and the files together within
   the task's `max_size` (`too_large`).
5. What is given fits the task's contestant inputs (`invalid_inputs`).

Then, while a contestant has no submission of the task, their place to
submit it is made, as the platform, or finished when a try stopped halfway;
every part of making it is safe to run again. The files go into it as one commit, as the
contestant, under `files/<input>/<name>` beside
`submission.json`; the commit is named `submission/<n>` as the platform,
with the next number on a collision; one `queued` grading row is inserted per
stage graded on submit, against the task's current publication, attempt 1,
with the hash of its callback token, whose runs start once the submit
commits; and the uploads are marked consumed, their objects removed once it
commits.

Submits of one workspace to one task happen one after another, under a
Postgres advisory lock held until the unit of work ends, so the limits are
counted once for each. A submit carries an idempotency key the browser made
once. The same key sent again returns the submission it made and creates
nothing: its rows are found by the key; and when the forge's writes landed
but the rows did not, because the unit of work failed after them or the
answer to naming the submission was lost, the submission is found at the
forge by the key its protected version's note carries, and only its rows are
inserted. A submit whose commit landed but whose version was never named
leaves a commit that is no submission, and the next try makes one. A
submission named at the forge whose rows never landed is one the contestant
saw fail, and submitting again finishes it.

A contestant reads their own submissions back, newest first, each with its
grading at every stage as that stage's `show` allows: status only, status
and metrics, or everything. `files` gives the inputs a submission was made
with, and `download` the door to one of its files: where the proxy reads it
from the forge and streams it to the person, so its bytes, two gigabytes or
two, never pass through the platform. `run_log` gives the log of a grading's
run where the stage shows everything, of at most `RUN_LOG_MAX` bytes
(`log_too_large`).

What the store or the forge says when it fails goes to the log, and the
contestant is told only that it did not answer, refused the platform, or
refused the submission, in fixed words.
"""

import builtins
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text

from forge.db.tables import Grading
from forge.db.tables import Upload as UploadRow
from forge.domain import submissions as rules
from forge.domain.definitions import SUBMISSION_CEILING, Show
from forge.domain.errors import (
    Conflict,
    Forbidden,
    InvalidIdempotencyKey,
    LogTooLarge,
    Misconfigured,
    NotFound,
    PortError,
    RateLimited,
    Rejected,
    SubmissionLimit,
    TooLarge,
    Unavailable,
    UploadNotReady,
    UploadNotYours,
)
from forge.domain.grading import RUN_LOG_MAX, GradingStatus, log_key
from forge.domain.identity import AsUser
from forge.domain.ids import SubmissionId, TaskId, WorkspaceId
from forge.domain.sessions import Session
from forge.domain.submissions import Submitted, SubmittedInput, UploadedFile
from forge.domain.uploads import Door, UploadStatus, pointer_text
from forge.log import get_logger
from forge.port.uploads import SubmissionPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import gradings, sessions, submitters, uploads
from forge.services.submitters import Entrant

log = get_logger(__name__)

SUBMIT_LOCK = 0x5355424D
"""The first key of every lock on a workspace's submits of a task, the second
being the workspace and task hashed by Postgres."""
NO_SUCH_SUBMISSION = "There is no such submission."
LOG_STORE_UNAVAILABLE = "The run log could not be read; try again in a moment."
NO_LOG = "This submission has no log you may read."
FORGE_UNAVAILABLE = "The forge did not answer; try again in a moment."
FORGE_MISCONFIGURED = "The forge refused the platform's own registration."
FORGE_REFUSED = "The forge refused the submission; submit again, or tell the organisers."
SUBMISSION_FILE = rules.SUBMISSION_FILE
DOCUMENT_MAX = 1024 * 1024
"""The most of a `submission.json` read; the platform writes a few hundred bytes."""


@dataclass(frozen=True, slots=True)
class Result:
    """One grading of a submission as its contestant sees it: its id, stage
    and attempt and where it stands, and of its verdict what the stage's
    `show` allows. `full` gives the outcome, the metrics, the summary, the
    row of every test and whether there is a log; `metrics` gives the
    outcome and the metrics; `hidden` gives the status alone.
    """

    id: uuid.UUID
    stage: str
    attempt: int
    status: GradingStatus
    show: Show
    outcome: str | None
    metrics: dict[str, Any] | None
    summary: str | None
    tests: tuple[dict[str, Any], ...] | None
    log: bool


@dataclass(frozen=True, slots=True)
class Submission:
    """One of the contestant's submissions of a task: its number, when it was
    taken, and the latest attempt of its grading at each stage, in the order
    the task lists its stages.
    """

    task: TaskId
    number: int
    submitted_at: datetime
    gradings: tuple[Result, ...]


@dataclass(frozen=True, slots=True)
class SubmittedFiles:
    """What a submission was made with: its `submission.json` inputs, each
    input's files by their paths in the submission, its language, or its
    value.
    """

    task: TaskId
    number: int
    inputs: dict[str, Any]


@action
async def submit(
    ctx: Context,
    session: Session,
    task: TaskId,
    inputs: Mapping[str, SubmittedInput],
    *,
    idempotency_key: str,
) -> Submission:
    """Submit the signed-in contestant's uploads and values to the task, and
    queue its grading. The same `idempotency_key` sent again answers with the
    submission it made and creates nothing.
    """
    if not isinstance(idempotency_key, str) or not rules.key_is_valid(idempotency_key):
        raise InvalidIdempotencyKey(
            "A submit carries an idempotency key of 8 to 128 letters, digits, - and _."
        )
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    opened = False
    if (
        workspace is not None
        and not any(given.uploads for given in inputs.values())
        and not await _submitted_before(ctx, workspace, task)
    ):
        # A first submission of nothing but typed values names no upload, so
        # no slot made its place; it is made here, by the rules a submit is
        # checked against first, and before the hold below, since making it
        # takes the forge seconds and the hold keeps the connection.
        await submitters.refuse(ctx, entrant)
        await submitters.open_place(ctx, entrant, workspace)
        opened = True
    if workspace is not None:
        await _hold(ctx, workspace, task)
        again = await _again(ctx, entrant, workspace, idempotency_key)
        if again is not None:
            return again
    _, workspace = await submitters.refuse(ctx, entrant)
    made = await _listed(ctx, workspace, task) or ()
    limits = entrant.published.definition.limits
    if len(made) >= limits.submissions:
        raise SubmissionLimit(
            f"You have made all {limits.submissions} submissions this task allows.",
            limit=limits.submissions,
        )
    await _refuse_rate(ctx, entrant, workspace)
    chosen = await _chosen(ctx, entrant, inputs)
    if not made and not chosen and not opened:
        # Naming an upload means a slot made the place already, and a first
        # submission of typed values made it before the hold; one whose
        # gradings are all gone still has its place made here.
        await submitters.open_place(ctx, entrant, workspace)
    layout = rules.lay_out(
        entrant.published.definition.inputs.contestant,
        inputs,
        {upload.id: _uploaded(upload) for upload in chosen.values()},
    )
    files = {SUBMISSION_FILE: layout.document}
    user = AsUser(entrant.session.user_id, await sessions.credential_for(ctx, entrant.session.id))
    for path, upload in layout.files.items():
        files[path] = await _pointer(ctx, entrant, user, chosen[upload])
    recorded = await _record(ctx, user, workspace, task, files, idempotency_key)
    gradings = _insert(ctx, entrant, workspace, recorded, idempotency_key, at=ctx.now)
    for used in chosen.values():
        used.status = UploadStatus.CONSUMED
        used.consumed_by = recorded.id
    await ctx.db.flush()
    log.info(
        "submissions.submitted",
        task=task,
        number=recorded.number,
        gradings=len(gradings),
        user_id=entrant.session.user_id,
    )
    return _submission(ctx, entrant, recorded.number, ctx.now, gradings)


@action
async def mine(ctx: Context, session: Session, task: TaskId) -> tuple[Submission, ...]:
    """The signed-in person's own submissions of the task, newest first."""
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    if workspace is None:
        return ()
    return tuple(await _read(ctx, entrant, workspace))


@action
async def one(ctx: Context, session: Session, task: TaskId, number: int) -> Submission:
    """One of the signed-in person's own submissions of the task, by its
    number. `NotFound` for one that is not theirs.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    found = await _read(ctx, entrant, workspace, number) if workspace is not None else []
    if not found:
        raise NotFound(NO_SUCH_SUBMISSION)
    return found[0]


@action
async def files(ctx: Context, session: Session, task: TaskId, number: int) -> SubmittedFiles:
    """The inputs one of the signed-in person's own submissions was made with,
    as its `submission.json` names them.
    """
    submission, user = await _own(ctx, session, task, number)
    content = await _file(ctx, user, submission, SUBMISSION_FILE)
    try:
        document = _document(content)
    except ValueError as exc:
        log.warning("submissions.unreadable", task=task, number=number)
        raise NotFound(NO_SUCH_SUBMISSION) from exc
    return SubmittedFiles(task, number, document)


@action
async def download(ctx: Context, session: Session, task: TaskId, number: int, path: str) -> Door:
    """Where one file of one of the signed-in person's own submissions is
    read, by the path `files` names it by, and what to present there, for the
    proxy to fetch it and stream it to them. `NotFound` for a submission that
    is not theirs or a path it does not name.
    """
    submission, user = await _own(ctx, session, task, number)
    content = await _file(ctx, user, submission, SUBMISSION_FILE)
    try:
        document = _document(content)
    except ValueError as exc:
        raise NotFound(NO_SUCH_SUBMISSION) from exc
    named = {
        name
        for given in document.values()
        if isinstance(given, dict)
        for name in given.get("files") or ()
    }
    if path not in named:
        raise NotFound("The submission has no such file.")
    door = await ctx.forge.workspaces.download(user, submission, path)
    log.info("submissions.download_opened", submission=submission, user_id=user.user_id)
    return door


@action
async def run_log(
    ctx: Context, session: Session, task: TaskId, number: int, *, stage: str | None = None
) -> bytes:
    """The run log of one of the signed-in person's own submissions, of the
    latest attempt at `stage`, or at the first stage in the task's order
    with a log, where the stage's `show` is `full`. `NotFound` for a
    submission that is not theirs, or one with no log they may read, and
    `LogTooLarge` for a log over `RUN_LOG_MAX` bytes, which is never read
    whole.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    found = await _read(ctx, entrant, workspace, number) if workspace is not None else []
    if not found:
        raise NotFound(NO_SUCH_SUBMISSION)
    shown = [
        result
        for result in found[0].gradings
        if result.log and (stage is None or result.stage == stage)
    ]
    if not shown:
        raise NotFound(NO_LOG)
    grading = str(shown[0].id)
    try:
        return await ctx.forge.objects.read(
            log_key(shown[0].id, shown[0].attempt), max_size=RUN_LOG_MAX
        )
    except NotFound as exc:
        log.warning("submissions.log_missing", task=task, number=number)
        raise NotFound(NO_LOG) from exc
    except Misconfigured as exc:
        raise _log_failure(exc, grading) from None
    except Rejected as exc:
        log.warning("submissions.log_too_large", grading=grading, detail=exc.detail)
        raise LogTooLarge(
            f"The run log is larger than the {RUN_LOG_MAX} bytes shown.", limit=RUN_LOG_MAX
        ) from None
    except PortError as exc:
        raise _log_failure(exc, grading) from None


def _log_failure(exc: PortError, grading: str) -> PortError:
    """What a caller is told when the log store fails, in fixed words. What
    the store said goes to the log; S3's own codes name buckets and keys,
    which is nothing a contestant should read.
    """
    log.warning(
        "submissions.log_unreadable",
        grading=grading,
        error=type(exc).__name__,
        detail=exc.detail,
    )
    return Unavailable(LOG_STORE_UNAVAILABLE)


async def _submitted_before(ctx: Context, workspace: WorkspaceId, task: TaskId) -> bool:
    """Whether the workspace has a grading at the task, and so a place to
    submit it that some earlier submit made. Read without a lock: the
    answer only saves a call to the forge, and a place made twice is made
    once.
    """
    found = await ctx.db.scalar(
        select(Grading.id)
        .where(Grading.workspace_id == workspace, Grading.task_id == task)
        .limit(1)
    )
    return found is not None


async def _hold(ctx: Context, workspace: WorkspaceId, task: TaskId) -> None:
    """Wait for any other submit of the workspace to the task to finish, and
    hold the next one off until this unit of work ends.
    """
    await ctx.db.execute(
        text("SELECT pg_advisory_xact_lock(:space, hashtext(:target))"),
        {"space": SUBMIT_LOCK, "target": f"{workspace}|{task}"},
    )


async def _again(
    ctx: Context, entrant: Entrant, workspace: WorkspaceId, key: str
) -> Submission | None:
    """The submission a submit with this key made already, or none. Its rows
    are found by the key; failing that, the submission is found at the forge
    by its note, and its missing rows are inserted.
    """
    rows = (
        (
            await ctx.db.execute(
                select(Grading).where(
                    Grading.workspace_id == workspace,
                    Grading.task_id == entrant.task,
                    Grading.idempotency_key == key,
                )
            )
        )
        .scalars()
        .all()
    )
    if rows:
        number = rows[0].submission_number
        log.info("submissions.repeated", task=entrant.task, number=number)
        return (await _read(ctx, entrant, workspace, number))[0]
    found = next(
        (made for made in await _listed(ctx, workspace, entrant.task) or () if made.key == key),
        None,
    )
    if found is None:
        return None
    existing = await _read(ctx, entrant, workspace, found.number)
    if existing:
        return existing[0]
    gradings = _insert(ctx, entrant, workspace, found, key, at=found.at)
    await ctx.db.flush()
    log.info("submissions.recovered", task=entrant.task, number=found.number)
    return _submission(ctx, entrant, found.number, found.at, gradings)


async def _listed(
    ctx: Context, workspace: WorkspaceId, task: TaskId
) -> tuple[Submitted, ...] | None:
    """Every submission the workspace made for the task at the forge, or
    none while its place to submit is not made.
    """
    try:
        return await ctx.forge.workspaces.list_submissions(workspace, task)
    except NotFound:
        return None
    except PortError as exc:
        raise _forge_failure(exc, "submissions.list_failed", task=task) from None


async def _refuse_rate(ctx: Context, entrant: Entrant, workspace: WorkspaceId) -> None:
    rate = entrant.published.definition.limits.rate
    since = ctx.now - rate.per
    times = (
        (
            await ctx.db.execute(
                select(func.min(Grading.submitted_at))
                .where(
                    Grading.workspace_id == workspace,
                    Grading.task_id == entrant.task,
                    Grading.submitted_at > since,
                )
                .group_by(Grading.submission_id)
            )
        )
        .scalars()
        .all()
    )
    if len(times) >= rate.count:
        retry_at = sorted(times)[len(times) - rate.count] + rate.per
        raise RateLimited(
            f"This task takes {rate.count} submission(s) every {int(rate.per.total_seconds())}s.",
            rate=str(rate),
            retry_at=retry_at.isoformat(),
        )


async def _chosen(
    ctx: Context, entrant: Entrant, inputs: Mapping[str, SubmittedInput]
) -> dict[uuid.UUID, UploadRow]:
    """Every upload the submit names, held until the unit of work ends, once
    each is the person's own for this task, checked, unused and in time, and
    each file and all of them together are within the task's limits.
    """
    named = [upload for given in inputs.values() for upload in given.uploads]
    rows = await uploads.owned(ctx, entrant.session.user_id, entrant.task, set(named))
    foreign = sorted({str(upload) for upload in named if upload not in rows})
    if foreign:
        raise UploadNotYours("An upload named is not one of yours for this task.", uploads=foreign)
    unready = sorted(
        str(row.id)
        for row in rows.values()
        if row.status != UploadStatus.VERIFIED or row.expires_at <= ctx.now
    )
    if unready:
        raise UploadNotReady(
            "An upload named is not a complete, checked file that is not submitted yet.",
            uploads=unready,
        )
    declared = {entry.id: entry for entry in entrant.published.definition.inputs.contestant}
    for row in sorted(rows.values(), key=lambda row: str(row.id)):
        entry = declared.get(row.input_id or "")
        if entry is not None and entry.max_size is not None and _size(row) > entry.max_size:
            raise TooLarge(
                f"A file for {entry.id} is larger than the {entry.max_size} bytes allowed.",
                limit=entry.max_size,
                input=entry.id,
            )
    limit = min(entrant.published.definition.limits.max_size, SUBMISSION_CEILING)
    if sum(_size(row) for row in rows.values()) > limit:
        raise TooLarge(
            f"The submission is larger than the {limit} bytes allowed.", limit=limit, input=None
        )
    return rows


def _size(row: UploadRow) -> int:
    return row.size


def _uploaded(row: UploadRow) -> UploadedFile:
    return UploadedFile(
        upload=row.id, input=row.input_id or "", filename=row.filename, size=_size(row)
    )


async def _pointer(ctx: Context, entrant: Entrant, as_: AsUser, row: UploadRow) -> bytes:
    """What the commit holds in place of the file: the pointer to the object
    the forge keeps. The forge is asked once more, here, that the place still
    holds it, so a submission never commits a pointer to bytes that are not
    there.
    """
    place = SubmissionPlace(entrant.workspace, entrant.task) if entrant.workspace else None
    if place is None or not await uploads.holds(ctx, place, as_, row):
        log.warning("submissions.upload_gone", upload=str(row.id))
        raise UploadNotReady(
            "An upload is no longer at the forge; upload it again.", uploads=[str(row.id)]
        )
    return pointer_text(row.digest, row.size)


async def _record(
    ctx: Context,
    user: AsUser,
    workspace: WorkspaceId,
    task: TaskId,
    files: dict[str, bytes],
    key: str,
) -> Submitted:
    """The submission at the forge, its forge's refusals in the platform's
    words and what the forge said in the log.
    """
    try:
        return await ctx.forge.workspaces.record_submission(user, workspace, task, files, key=key)
    except Forbidden as exc:
        log.warning("submissions.refused_at_forge", task=task, detail=exc.detail)
        raise Forbidden("Your place to submit this task does not take your submissions.") from exc
    except Conflict as exc:
        log.warning("submissions.numbering_failed", task=task, detail=exc.detail)
        raise Conflict(
            "Other submissions were being made at the same moment; submit again."
        ) from exc
    except PortError as exc:
        raise _forge_failure(exc, "submissions.record_failed", task=task) from None


def _forge_failure(exc: PortError, event: str, **fields: str) -> PortError:
    """The error a contestant is given for a failure of the forge: that it
    did not answer, refused the platform's own registration, or refused the
    submission, in fixed words. What the forge said goes to the log as
    `event`.
    """
    log.warning(event, error=type(exc).__name__, detail=exc.detail, **fields)
    if isinstance(exc, Misconfigured):
        return Misconfigured(FORGE_MISCONFIGURED)
    if isinstance(exc, Rejected):
        return Rejected(FORGE_REFUSED)
    return Unavailable(FORGE_UNAVAILABLE)


def _insert(
    ctx: Context,
    entrant: Entrant,
    workspace: WorkspaceId,
    recorded: Submitted,
    key: str,
    *,
    at: datetime,
) -> builtins.list[Grading]:
    """One queued grading row for each stage the task grades on submit,
    against its current publication, with the hash of its own callback
    token.
    """
    return gradings.queue_submission(
        ctx,
        task=entrant.task,
        workspace=workspace,
        submission=recorded,
        publication=entrant.published.publication,
        definition=entrant.published.definition,
        key=key,
        at=at,
    )


async def _read(
    ctx: Context, entrant: Entrant, workspace: WorkspaceId, number: int | None = None
) -> builtins.list[Submission]:
    """The workspace's submissions of the task from their grading rows,
    newest first, or the one numbered `number`.
    """
    query = select(Grading).where(
        Grading.workspace_id == workspace, Grading.task_id == entrant.task
    )
    if number is not None:
        query = query.where(Grading.submission_number == number)
    rows = (await ctx.db.execute(query)).scalars().all()
    grouped: dict[int, builtins.list[Grading]] = {}
    for row in rows:
        grouped.setdefault(row.submission_number, []).append(row)
    gone = await gradings.lost(
        ctx, [row for found in grouped.values() for row in _latest(found).values()]
    )
    return [
        _submission(
            ctx,
            entrant,
            found,
            min(row.submitted_at for row in grouped[found]),
            grouped[found],
            gone,
        )
        for found in sorted(grouped, reverse=True)
    ]


def _latest(rows: Sequence[Grading]) -> dict[str, Grading]:
    """The latest attempt of each stage among `rows`."""
    latest: dict[str, Grading] = {}
    for row in rows:
        if row.stage not in latest or row.attempt > latest[row.stage].attempt:
            latest[row.stage] = row
    return latest


def _submission(
    ctx: Context,
    entrant: Entrant,
    number: int,
    at: datetime,
    rows: Sequence[Grading],
    lost: frozenset[uuid.UUID] = frozenset(),
) -> Submission:
    """The submission as its owner reads it, each stage by its latest
    attempt, with `lost` naming the gradings whose runs the CI has lost.
    """
    stages = entrant.published.definition.stages_resolved()
    order = {stage.id: index for index, stage in enumerate(stages)}
    shows = {stage.id: stage.show for stage in stages}
    latest = _latest(rows)
    results = tuple(
        _result(row, gradings.status_of(ctx, row, lost), shows.get(row.stage, Show.HIDDEN))
        for row in sorted(
            latest.values(), key=lambda row: (order.get(row.stage, len(order)), row.stage)
        )
    )
    return Submission(entrant.task, number, at, results)


def _result(row: Grading, status: GradingStatus, show: Show) -> Result:
    """The grading as the contestant may see it under the stage's `show`. A
    system error's summary is written for staff, so it is never shown.
    """
    verdict = row.verdict or {}
    full = show is Show.FULL
    shown = show is not Show.HIDDEN
    outcome = verdict.get("outcome") if shown else None
    metrics = verdict.get("metrics") if shown else None
    graded = verdict.get("outcome") != "system_error"
    summary = verdict.get("summary") if full and graded else None
    tests = verdict.get("tests") if full else None
    return Result(
        id=row.id,
        stage=row.stage,
        attempt=row.attempt,
        status=status,
        show=show,
        outcome=str(outcome) if outcome is not None else None,
        metrics=dict(metrics) if isinstance(metrics, dict) else None,
        summary=str(summary) if summary is not None else None,
        tests=tuple(test for test in tests if isinstance(test, dict))
        if isinstance(tests, list)
        else None,
        log=full and row.log_key is not None,
    )


async def _own(
    ctx: Context, session: Session, task: TaskId, number: int
) -> tuple[SubmissionId, AsUser]:
    """One of the signed-in person's own submissions, and the identity its
    files are read as. `NotFound` for one that is not theirs.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    found = (
        (
            await ctx.db.execute(
                select(Grading.submission_id)
                .where(
                    Grading.workspace_id == workspace,
                    Grading.task_id == task,
                    Grading.submission_number == number,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if workspace is not None
        else None
    )
    if found is None:
        raise NotFound(NO_SUCH_SUBMISSION)
    user = AsUser(entrant.session.user_id, await sessions.credential_for(ctx, entrant.session.id))
    return SubmissionId(found), user


async def _file(ctx: Context, user: AsUser, submission: SubmissionId, path: str) -> bytes:
    try:
        return await ctx.forge.workspaces.read_submission_file(
            user, submission, path, max_size=DOCUMENT_MAX
        )
    except (NotFound, Forbidden, Rejected) as exc:
        log.warning("submissions.file_unreadable", submission=submission, detail=exc.detail)
        raise NotFound("The submission has no such file.") from exc
    except PortError as exc:
        raise _forge_failure(exc, "submissions.file_failed", submission=submission) from None


def _document(content: bytes) -> dict[str, Any]:
    """The inputs a `submission.json` names. `ValueError` for one that does
    not read.
    """
    document = json.loads(content)
    if not isinstance(document, dict) or not isinstance(document.get("inputs"), dict):
        raise ValueError("not a submission document")
    inputs: dict[str, Any] = document["inputs"]
    return inputs
