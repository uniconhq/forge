"""A contestant's uploads becoming a submission, and the submissions they read
back. `submit` runs in this order and stops at the first refusal, each with a
code of its own, before anything is written:

1. The task is open by the server's clock plus the contestant's own
   extension, and they are approved (`submitters.refuse`).
2. They have submissions left (`submission_limit`), counted from the
   submissions at the forge, leaving out one staff cancelled
   (`gradings.staff_cancelled`) that no fallback keeps a result for, and the
   task's rate holds (`rate_limited`), counted from the grading rows of their
   submissions within its window.
3. Every upload named is theirs, for this task (`upload_not_yours`), and a
   checked file no submission has used (`upload_not_ready`).
4. Each file is within its input's `max_size` (`too_large`).
5. What is given fits the contestant inputs the task's plan declares, with
   the task's form details, the files under each input together within its
   `max_size` (`invalid_inputs`, `forge.domain.submissions`).

Then, while a contestant has no submission of the task, their place to
submit it is made, as the platform, or finished when a try stopped halfway;
every part of making it is safe to run again. The files go into it as one commit, as the
contestant, under `files/<input>/<name>` beside
`submission.json`; the commit is named `submission/<n>` as the platform,
with the next number on a collision; one `queued` grading row is inserted,
against the task's current publication, attempt 1, with the hash of its
callback token, whose run starts once the submit commits; and the uploads
are marked consumed, their objects removed once it commits.

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
saw fail, and submitting again finishes it: a retry with its key, or any
later submit of the task, inserts its rows. A submission found this way
also has the uploads it used marked consumed, so they stop counting against
what the person may hold and no later submit can name them: each file of it
is a large-file pointer naming a SHA-256, and for each one, the oldest of the
person's checked uploads for the task with that digest is the one taken.
What it used is read from the submission itself and never from the request,
since a retry may carry the same key with other uploads. A later submit that
is refused rolls its finishing back with the rest, and the next one finishes
it again.

A contestant reads their own submissions back, newest first, each with the
latest attempt of its grading as the task's test groups show it
(`forge.domain.showing`), how many started days late it was, nothing of
a run in `system_error` but that it is still being graded, and of one staff
then cancelled, that it is `cancelled` and the sentence they gave. A
submission staff cancelled does not count against the task's
`submissions.max`. While a fallback is in force for a submission in
`system_error` or staff cancelled (`scores.fallen_back`), it is read by its
last good result instead, as the boards count it, and counts against
`submissions.max`. A grading is
shown with the publication it ran under: its sealed facts, and its
`test_groups` unless the latest publication's plan lists the same tests. A
past publication's `task.yaml` and plan are read once per process, since a
publication never changes; one that does not read shows nothing of its
gradings but where they stand. Each grading is scored on read
(`services.scores`): every shown group's points and most, each shown test's
credit, and the points shown with those still to be decided at the reveal,
the late factor of the submission's started days applied. `files` gives
the inputs a submission was made with, and `download` the door to one of
its files: where the proxy reads it from the forge and streams it to the
person, so its bytes, two gigabytes or two, never pass through the
platform. A run's log names every test, hidden ones too, so it is the
organisers' alone.

Organisers who observe the task read any row's submission the same way
(`organised`, TASK-FORMAT.md section 1.7): the same attempt, scored by the
same code, shown as it is once the task has revealed, so every group's
outcome, tests and points and every sealed value are filled in, with each
group's `shown_at` kept from what the row is shown now, when it sees that
group, and the grading's own status in place of the row's words for it.

What the store or the forge says when it fails goes to the log, and the
contestant is told only that it did not answer, refused the platform, or
refused the submission, in fixed words.
"""

import builtins
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

from sqlalchemy import func, select, text

from forge.db.tables import Grading
from forge.db.tables import Upload as UploadRow
from forge.domain import exact_json
from forge.domain import submissions as rules
from forge.domain.definitions import ContestDefinition, OnSystemError
from forge.domain.errors import (
    Conflict,
    Forbidden,
    InvalidIdempotencyKey,
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
from forge.domain.grading import GradingStatus
from forge.domain.identity import AsUser
from forge.domain.ids import PublicationId, SubmissionId, TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner
from forge.domain.release import due_of, late_days
from forge.domain.roles import Role, contest_id_of, task_scope
from forge.domain.scoring import Points
from forge.domain.sessions import Session
from forge.domain.showing import GroupShown, SubmissionState, filled_in, shown, told
from forge.domain.submissions import Submitted, SubmittedInput, UploadedFile
from forge.domain.uploads import (
    POINTER_MAX,
    Door,
    UploadPurpose,
    UploadStatus,
    pointer_text,
    read_pointer,
)
from forge.log import get_logger
from forge.port.uploads import SubmissionPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import gradings, published, scores, sessions, submitters, timelines, uploads
from forge.services.access import Organiser, require
from forge.services.published import PublishedTask
from forge.services.submitters import Entrant

log = get_logger(__name__)

SUBMIT_LOCK = 0x5355424D
"""The first key of every lock on a workspace's submits of a task, the second
being the workspace and task hashed by Postgres."""
NO_SUCH_SUBMISSION = "There is no such submission."
FORGE_UNAVAILABLE = "The forge did not answer; try again in a moment."
FORGE_MISCONFIGURED = "The forge refused the platform's own registration."
FORGE_REFUSED = "The forge refused the submission; submit again, or tell the organisers."
SUBMISSION_FILE = rules.SUBMISSION_FILE
DOCUMENT_MAX = 1024 * 1024
"""The most of a `submission.json` read; the platform writes a few hundred bytes."""


@dataclass(frozen=True, slots=True)
class Result:
    """The latest attempt of a submission's grading as its contestant sees
    it: its id and attempt, where it stands, and once it is done, what
    stopped the run, the outcome over the groups shown, each test group as
    its `show` allows, the values reported once, and each value with a
    fold, folded over the tests shown; on a task that gives points, the
    points shown and those pending until the reveal, and the late factor
    they include. Where it stands is in the contestant's words
    (`SubmissionState`): a run in `system_error` is still `grading` to its
    contestant, with nothing else; one staff cancelled is `cancelled`, with
    `reason`, the sentence they gave. Read by organisers, everything is
    shown and where it stands is the grading's own `GradingStatus`.
    """

    id: uuid.UUID
    attempt: int
    status: SubmissionState | GradingStatus
    stopped: str | None
    outcome: str | None
    groups: tuple[GroupShown, ...]
    values: dict[str, Any]
    reason: str | None = None
    points: Points | None = None
    factor: Fraction | None = None
    folded: Mapping[str, Fraction] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Submission:
    """One of the contestant's submissions of a task: its number, when it was
    taken, how many started days after the row's due it was, and the latest
    attempt of its grading.
    """

    task: TaskId
    number: int
    submitted_at: datetime
    late_days: int
    grading: Result | None


@dataclass(frozen=True, slots=True)
class SubmittedFiles:
    """What a submission was made with: its `submission.json` inputs, each
    input's files by their paths in the submission, or its value.
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
        await submitters.open_place(ctx, entrant)
        opened = True
    if workspace is not None:
        await _hold(ctx, workspace, task)
        await submitters.hold_standing(ctx, entrant)
        again = await _again(ctx, entrant, workspace, idempotency_key)
        if again is not None:
            return again
    _, workspace = await submitters.refuse(ctx, entrant)
    made = await _listed(ctx, workspace, task) or ()
    await _finish_unrecorded(ctx, entrant, workspace, made)
    caps = entrant.published.definition.submissions
    cancelled = await _cancelled(ctx, workspace, task, entrant.settings.on_system_error)
    if len([found for found in made if found.id not in cancelled]) >= caps.max:
        raise SubmissionLimit(
            f"You have made all {caps.max} submissions this task allows.", limit=caps.max
        )
    await _refuse_rate(ctx, entrant, workspace)
    chosen = await _chosen(ctx, entrant, inputs)
    if not made and not chosen and not opened:
        # Naming an upload means a slot made the place already, and a first
        # submission of typed values made it before the hold; one whose
        # gradings are all gone still has its place made here.
        await submitters.open_place(ctx, entrant)
    form = await published.form(ctx, entrant.published)
    layout = rules.lay_out(
        form.fields,
        form.tests,
        inputs,
        {upload.id: _uploaded(upload) for upload in chosen.values()},
    )
    files = {SUBMISSION_FILE: layout.document}
    user = AsUser(entrant.session.user_id, await sessions.credential_for(ctx, entrant.session.id))
    for path, upload in layout.files.items():
        files[path] = await _pointer(ctx, entrant, user, chosen[upload])
    recorded = await _record(ctx, user, workspace, task, files, idempotency_key)
    grading = _insert(ctx, entrant, workspace, recorded, idempotency_key, at=ctx.now)
    for used in chosen.values():
        used.status = UploadStatus.CONSUMED
        used.consumed_by = recorded.id
    await ctx.db.flush()
    log.info(
        "submissions.submitted",
        task=task,
        number=recorded.number,
        user_id=entrant.session.user_id,
    )
    return await _submission(
        ctx, Reading.of(entrant, workspace), recorded.number, ctx.now, [grading]
    )


@action
async def mine(ctx: Context, session: Session, task: TaskId) -> tuple[Submission, ...]:
    """The signed-in person's own submissions of the task, newest first."""
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    if workspace is None:
        return ()
    return tuple(await _read(ctx, Reading.of(entrant, workspace)))


@action
async def one(ctx: Context, session: Session, task: TaskId, number: int) -> Submission:
    """One of the signed-in person's own submissions of the task, by its
    number. `NotFound` for one that is not theirs.
    """
    entrant = await submitters.entrant(ctx, session, task)
    workspace = entrant.workspace
    found = (
        await _read(ctx, Reading.of(entrant, workspace), number) if workspace is not None else []
    )
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
    if path not in _paths(document):
        raise NotFound("The submission has no such file.")
    door = await ctx.forge.workspaces.download(user, submission, path)
    log.info("submissions.download_opened", submission=submission, user_id=user.user_id)
    return door


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


async def _cancelled(
    ctx: Context, workspace: WorkspaceId, task: TaskId, contest: OnSystemError
) -> set[str]:
    """The workspace's submissions of the task that staff cancelled and that
    no fallback keeps a result for: those whose latest attempt is a cancel
    with a sentence, with no fallback in force for it by staff or by the
    contest's `on_system_error`, or no earlier attempt that finished with a
    result.
    """
    rows = (
        await ctx.db.execute(
            select(Grading).where(Grading.workspace_id == workspace, Grading.task_id == task)
        )
    ).scalars()
    grouped: dict[str, builtins.list[Grading]] = {}
    for row in rows:
        grouped.setdefault(row.submission_id, []).append(row)
    return {
        submission
        for submission, attempts in grouped.items()
        if gradings.staff_cancelled(_latest(attempts))
        and scores.fallen_back(ctx, attempts, (), contest) is None
    }


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
        return (await _read(ctx, Reading.of(entrant, workspace), number))[0]
    found = next(
        (made for made in await _listed(ctx, workspace, entrant.task) or () if made.key == key),
        None,
    )
    if found is None:
        return None
    existing = await _read(ctx, Reading.of(entrant, workspace), found.number)
    if existing:
        return existing[0]
    grading = await _recover(ctx, entrant, workspace, found)
    return await _submission(ctx, Reading.of(entrant, workspace), found.number, found.at, [grading])


async def _finish_unrecorded(
    ctx: Context, entrant: Entrant, workspace: WorkspaceId, made: Sequence[Submitted]
) -> None:
    """Give every submission at the forge that has no grading row its rows,
    and mark consumed the uploads it used, before the limits are counted and
    the uploads named are checked.
    """
    if not made:
        return
    graded = set(
        (
            await ctx.db.execute(
                select(Grading.submission_id)
                .where(Grading.workspace_id == workspace, Grading.task_id == entrant.task)
                .distinct()
            )
        ).scalars()
    )
    for found in made:
        if found.id not in graded:
            await _recover(ctx, entrant, workspace, found)


async def _recover(
    ctx: Context, entrant: Entrant, workspace: WorkspaceId, found: Submitted
) -> Grading:
    """The grading row of a submission the forge holds and the database does
    not, inserted with the key its note carries, and the uploads it used
    marked consumed.
    """
    grading = _insert(ctx, entrant, workspace, found, found.key, at=found.at)
    await _consume_used(ctx, entrant, found)
    await ctx.db.flush()
    log.info("submissions.recovered", task=entrant.task, number=found.number)
    return grading


async def _consume_used(ctx: Context, entrant: Entrant, found: Submitted) -> None:
    """Mark consumed, by `found`, the uploads its files point at: for each
    pointer, the oldest checked upload for the task with its digest of
    whoever works in the workspace, the person or their team's members. A
    submission whose `submission.json` does not read marks nothing, and its
    uploads lapse as any unused one does.
    """
    user = AsUser(entrant.session.user_id, await sessions.credential_for(ctx, entrant.session.id))
    try:
        document = _document(await _file(ctx, user, found.id, SUBMISSION_FILE))
    except NotFound, ValueError:
        log.warning("submissions.recovered_unreadable", submission=found.id)
        return
    digests: builtins.list[str] = []
    for path in sorted(_paths(document)):
        try:
            blob = await ctx.forge.workspaces.read_submission_blob(
                user, found.id, path, max_size=POINTER_MAX
            )
        except NotFound, Forbidden, Rejected:
            continue
        except PortError as exc:
            raise _forge_failure(exc, "submissions.file_failed", submission=found.id) from None
        named = read_pointer(blob)
        if named is not None:
            digests.append(named[0])
    if not digests:
        return
    rows = (
        await ctx.db.execute(
            select(UploadRow)
            .where(
                UploadRow.owner_user_id.in_(entrant.members or (entrant.session.user_id,)),
                UploadRow.task_id == entrant.task,
                UploadRow.purpose == UploadPurpose.SUBMISSION,
                UploadRow.status == UploadStatus.VERIFIED,
                UploadRow.digest.in_(set(digests)),
            )
            .order_by(UploadRow.created_at, UploadRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    waiting: dict[str, builtins.list[UploadRow]] = {}
    for row in rows:
        waiting.setdefault(row.digest, []).append(row)
    for digest in digests:
        if waiting.get(digest):
            used = waiting[digest].pop(0)
            used.status = UploadStatus.CONSUMED
            used.consumed_by = found.id


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
    rate = entrant.published.definition.submissions.rate
    since = ctx.now - rate.window
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
        retry_at = sorted(times)[len(times) - rate.count] + rate.window
        raise RateLimited(
            f"This task takes {rate.count} submission(s) every {rate.per}s.",
            rate=f"{rate.count} per {rate.per}s",
            retry_at=retry_at.isoformat(),
        )


async def _chosen(
    ctx: Context, entrant: Entrant, inputs: Mapping[str, SubmittedInput]
) -> dict[uuid.UUID, UploadRow]:
    """Every upload the submit names, held until the unit of work ends, once
    each is the person's own for this task, checked, unused and in time, and
    each file within its input's `max_size`.
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
    form = await published.form(ctx, entrant.published)
    declared = {entry.id: entry for entry in form.fields}
    for row in sorted(rows.values(), key=lambda row: str(row.id)):
        entry = declared.get(row.input_id or "")
        if entry is not None and _size(row) > entry.max_size:
            raise TooLarge(
                f"A file for {entry.id} is larger than the {entry.max_size} bytes allowed.",
                limit=entry.max_size,
                input=entry.id,
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
    key: str | None,
    *,
    at: datetime,
) -> Grading:
    """The submission's queued grading row, against the task's current
    publication, with the hash of its own callback token.
    """
    return gradings.queue_submission(
        ctx,
        task=entrant.task,
        workspace=workspace,
        submission=recorded,
        publication=entrant.published.publication,
        key=key,
        at=at,
    )


@action
async def organised(
    ctx: Context, organiser: Organiser, task: TaskId, owner: WorkspaceOwner, number: int
) -> Submission:
    """The submission numbered `number` of the row `owner`, a contestant or a
    team, as organisers read it: as its row reads it, with everything its
    row is not shown yet filled in, each group's `shown_at` saying when the
    row is shown it, and the grading's own status. Needs the observer role
    at the task, as its gradings do. `NotFound` for a task with no
    publication or a row with no such submission.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    settings = await published.contest(ctx, contest_id_of(task_scope(task)))
    found = await published.task(ctx, task, settings)
    if found is None:
        raise NotFound(NO_SUCH_SUBMISSION)
    workspace = ctx.forge.workspaces.workspace_of(contest_id_of(task_scope(task)), owner)
    read = await _read(ctx, Reading(task, settings, found, workspace), number, everything=True)
    if not read:
        raise NotFound(NO_SUCH_SUBMISSION)
    log.info(
        "submissions.organised",
        task=task,
        number=number,
        user_id=organiser.user.id,
    )
    return read[0]


@dataclass(frozen=True, slots=True)
class Reading:
    """Whose submissions of a task are read: the task as its latest
    publication froze it, the contest's settings, and the workspace.
    """

    task: TaskId
    settings: ContestDefinition
    published: PublishedTask
    workspace: WorkspaceId

    @classmethod
    def of(cls, entrant: Entrant, workspace: WorkspaceId) -> Reading:
        return cls(entrant.task, entrant.settings, entrant.published, workspace)


async def _read(
    ctx: Context, reading: Reading, number: int | None = None, *, everything: bool = False
) -> builtins.list[Submission]:
    """The workspace's submissions of the task from their grading rows,
    newest first, or the one numbered `number`; with `everything`, as
    organisers read them.
    """
    query = select(Grading).where(
        Grading.workspace_id == reading.workspace, Grading.task_id == reading.task
    )
    if number is not None:
        query = query.where(Grading.submission_number == number)
    rows = (await ctx.db.execute(query)).scalars().all()
    grouped: dict[int, builtins.list[Grading]] = {}
    for row in rows:
        grouped.setdefault(row.submission_number, []).append(row)
    gone = await gradings.lost(ctx, [_latest(found) for found in grouped.values()])
    scorer = scores.Scorer(ctx, reading.published, reading.settings.on_system_error)
    return [
        await _submission(
            ctx,
            reading,
            found,
            min(row.submitted_at for row in grouped[found]),
            grouped[found],
            gone,
            scorer,
            everything=everything,
        )
        for found in sorted(grouped, reverse=True)
    ]


def _latest(rows: Sequence[Grading]) -> Grading:
    """The latest attempt among `rows`, the attempts of one submission."""
    return max(rows, key=lambda row: row.attempt)


async def _submission(
    ctx: Context,
    reading: Reading,
    number: int,
    at: datetime,
    rows: Sequence[Grading],
    lost: frozenset[uuid.UUID] = frozenset(),
    scorer: scores.Scorer | None = None,
    *,
    everything: bool = False,
) -> Submission:
    """The submission as its owner reads it, by the latest attempt of its
    grading, or by its last good result while a fallback is in force for
    it, with `lost` naming the gradings whose runs the CI has lost; with
    `everything`, as organisers read it.
    """
    settings = reading.settings
    task = reading.published.name
    entry = settings.entry(task)
    contest = contest_id_of(task_scope(reading.task))
    extension = await timelines.of_owner(
        ctx, contest, ctx.forge.workspaces.owner_of(reading.workspace)
    )
    late = late_days(due_of(settings, entry, extension), at) if entry is not None else 0
    if not rows:
        return Submission(reading.task, number, at, late, None)
    row = scores.fallen_back(ctx, rows, lost, settings.on_system_error) or _latest(rows)
    status = gradings.status_of(ctx, row, lost)
    reveal_at = await timelines.reveal(ctx, contest, settings, task)
    factor = scores.factor(settings, reading.published, extension, at)
    result = await _result(
        ctx,
        scorer or scores.Scorer(ctx, reading.published, settings.on_system_error),
        row,
        status,
        reveal_at,
        factor,
        everything=everything,
    )
    return Submission(reading.task, number, at, late, result)


async def _result(
    ctx: Context,
    scorer: scores.Scorer,
    row: Grading,
    status: GradingStatus,
    reveal_at: datetime | None,
    factor: Fraction,
    *,
    everything: bool = False,
) -> Result:
    """The grading as its contestant may see it now, with the publication it
    ran under, scored. A run in `system_error` is told as still grading,
    with nothing of it shown, and one staff cancelled as cancelled, with the
    sentence they gave. With `everything`, as organisers read it: where it
    stands in its own words, and shown as after the reveal, each group's
    `shown_at` kept from what its contestant is shown now.
    """
    state: SubmissionState | GradingStatus = status if everything else told(status)
    if status is GradingStatus.CANCELLED:
        return Result(row.id, row.attempt, state, None, None, (), {}, row.cancel_reason)
    if status is not GradingStatus.DONE or row.result is None:
        return Result(row.id, row.attempt, state, None, None, (), {})
    graded = await scorer.graded(PublicationId(row.publication_id))
    if graded is None:
        return Result(row.id, row.attempt, state, None, None, (), {})
    revealed = reveal_at is not None and ctx.now >= reveal_at
    scored = await scorer.scored(row, graded, factor)
    seen = shown(
        row.result,
        graded.groups,
        graded.sealed,
        revealed=revealed,
        reveal_at=reveal_at,
        scored=scored,
    )
    if everything:
        seen = filled_in(
            seen,
            shown(
                row.result,
                graded.groups,
                graded.sealed,
                revealed=True,
                reveal_at=reveal_at,
                scored=scored,
            ),
        )
    return Result(
        row.id,
        row.attempt,
        state,
        seen.stopped,
        seen.outcome,
        seen.groups,
        dict(seen.values),
        points=seen.points,
        factor=factor if scored.gives_points else None,
        folded=seen.folded,
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


def _paths(document: Mapping[str, Any]) -> set[str]:
    """The path of every file a submission's inputs name."""
    return {
        name
        for given in document.values()
        if isinstance(given, dict)
        for name in given.get("files") or ()
        if isinstance(name, str)
    }


def _document(content: bytes) -> dict[str, Any]:
    """The inputs a `submission.json` names, each number read exactly.
    `ValueError` for one that does not read.
    """
    document = exact_json.loads(content)
    if not isinstance(document, dict) or not isinstance(document.get("inputs"), dict):
        raise ValueError("not a submission document")
    inputs: dict[str, Any] = document["inputs"]
    return inputs
