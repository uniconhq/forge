"""A contestant's uploads becoming a submission of their entrant's, and the
submissions they read back. `submit` runs in this order and stops at the
first refusal, each with a code of its own, before anything is written:

1. The task is open by the server's clock plus the entrant's extension, the
   entrant is approved and has as many members as the contest asks
   (`submitters.refuse`).
2. The entrant has submissions left (`submission_limit`), counted from the
   submissions at the forge, leaving out one that has no result and whose
   latest attempt is cancelled or over the limit, void, or is a system
   error, delayed (`gradings.place_of`), and the task's rate holds
   (`rate_limited`), counted from the grading rows of its submissions
   within its window.
3. Every upload named is theirs, for this task (`upload_not_yours`), and a
   checked file no submission has used (`upload_not_ready`).
4. Each file is within its input's `max_size` (`too_large`).
5. What is given fits the contestant inputs the task's plan declares, with
   the task's form details, the files under each input together within its
   `max_size` (`invalid_inputs`, `forge.domain.submissions`).

Then, while the entrant has no submission of the task, its place to
submit it is made, as the platform, or finished when a try stopped halfway;
every part of making it is safe to run again. The files go into it as one commit, as the
contestant, under `files/<input>/<name>` beside
`submission.json`; the commit is named `submission/<n>` as the platform,
with the next number on a collision; one `queued` grading row is inserted,
against the task's current publication, attempt 1, with the hash of its
callback token and the member who submitted, whose run starts once the
submit commits; and the uploads
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
attempt of its grading that counts as the task's test groups show it
(`forge.domain.showing`). The
attempt that counts is the one the leaderboards count (`scores.counted`): the
latest that finished with a result, so a regrade under way, a system error
and a staff cancel leave the result read as it was; and while none has, the
latest, of which a run in `system_error` is read as `delayed` and nothing
more, and one staff then cancelled as `cancelled` with the sentence they
gave. A submission with no result whose latest attempt is delayed, staff
cancelled or over the limit does not count against the task's
`submissions.max`. A delayed one is graded again only while its entrant
has room under the most, and otherwise ends over the limit, read as
`over_limit` with the most it reached (`gradings.retry_by_itself`), so no
entrant gets more results than the most allows. A grading is
shown with the publication it ran under: its sealed facts always, and
the latest publication's `test_groups` and value meanings when its own is
of the same generation (no grading change published since) and the latest
plan lists the same tests, its own otherwise (`showing.under`,
`scores.GradedOf`). A past publication's `task.yaml` and plan are read once
per process, since a publication never changes; one that does not read
shows nothing of its gradings but where they stand. A grading graded test
by test is not scored (stage 5 section 6, item 123). `files` gives
the inputs a submission was made with, and `download` the door to one of
its files: where the proxy reads it from the forge and streams it to the
person, so its bytes, two gigabytes or two, never pass through the
platform. A run's log names every test, hidden ones too, so it is the
organisers' alone. A run task's result is read through
`forge.domain.run_showing`, its values worked out on read
(`scores.run_scored`), a mark entered for it counted only once a leaderboard
cell shown to the entrant counted the submission, before the reveal or
after it (`leaderboards.chosen`, SIMPLER-CORE.md 14): until then, and always
when none does, it is worked out as if none were entered. A text it
shows is read one at a time, only while the result shows it to that
reader: an entrant's by where it is shown (`read_text`), never by the
stored output that holds it, whose id is another entrant's too and says
when it was made; organisers' by that output (`read_organised_text`).

Organisers who observe the task read any row's submission the same way
(`organised`, TASK-FORMAT.md section 1.7): the same attempt, read by the
same code, shown as it is once the task has revealed, so every group's
outcome and tests and every sealed value are filled in, with each
group's `shown_at` kept from what the row is shown now, when it sees that
group, and the grading's own status in place of the row's words for it.

What the store or the forge says when it fails goes to the log, and the
contestant is told only that it did not answer, refused the platform, or
refused the submission, in fixed words.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

from sqlalchemy import func, select, text

from forge.db.tables import Grading, StepOutput
from forge.db.tables import Upload as UploadRow
from forge.db.tables.gradings import FRONT
from forge.domain import exact_json, run_showing
from forge.domain import submissions as rules
from forge.domain.blocks.catalogue import ENTERED
from forge.domain.blocks.evaluate import using
from forge.domain.definitions import ContestDefinition
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
from forge.domain.grading import GradingStatus, Place
from forge.domain.identity import AsUser
from forge.domain.ids import PublicationId, SubmissionId, TaskId, WorkspaceId
from forge.domain.leaderboards import entries_open
from forge.domain.meanings import ValueMeaning, shown_meanings
from forge.domain.names import WorkspaceOwner
from forge.domain.release import Extension
from forge.domain.roles import Role, contest_id_of, task_scope
from forge.domain.run_showing import TextAt, ValueShown
from forge.domain.sessions import Session
from forge.domain.showing import GroupShown, filled_in, shown
from forge.domain.step_data import RESULT_VERSION
from forge.domain.submission_state import SubmissionState, told
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
from forge.services import (
    entrant_view,
    gradings,
    leaderboards,
    scores,
    sessions,
    submitters,
    timelines,
    uploads,
)
from forge.services.access import Organiser, require
from forge.services.entrant_view import PublishedTask
from forge.services.submitters import Person

log = get_logger(__name__)

SUBMIT_LOCK = 0x5355424D
"""The first key of every lock on a workspace's submits of a task, the second
being the workspace and task hashed by Postgres."""
NO_SUCH_SUBMISSION = "There is no such submission."
NO_SUCH_TEXT = "There is no such text."
FORGE_UNAVAILABLE = "The server did not answer. Try again in a moment."
FORGE_MISCONFIGURED = "Unicon is not set up right, so this cannot be done. Tell whoever runs it."
FORGE_REFUSED = "The submission could not be saved. Submit again, or tell the organisers."
SUBMISSION_FILE = rules.SUBMISSION_FILE
DOCUMENT_MAX = 1024 * 1024
"""The most of a `submission.json` read; the platform writes a few hundred bytes."""


@dataclass(frozen=True, slots=True)
class Result:
    """The latest attempt of a submission's grading as its contestant sees
    it: its id and attempt, where it stands, and once it is done, what
    stopped the run, the outcome over the groups shown, each test group as
    its `show` allows, and the values reported once. Where it stands is in the contestant's words
    (`SubmissionState`): a run in `system_error` is `delayed` to its
    contestant, with nothing else; one staff cancelled is `cancelled`, with
    `reason`, the sentence they gave; and one ended over the limit is
    `over_limit`, with `limit`, the `submissions.max` its entrant had
    reached. Read by organisers, everything is shown and where it stands is
    the grading's own `GradingStatus`. Once done, `meanings` lists every
    value the workflow reports, in its order, with the title and unit a
    page names it by and how it folds, a value a sealed step holds back
    left out until the reveal. A run task's grading has no groups and
    no `values`: `run_values` holds each value of its scoring shown to its
    reader, `runs` each run shown, by group, `once` what its steps done
    once reported, the compile log among them, and `compiled`, once a run
    is shown and none while it waits, and, to organisers alone, `facts`
    (`forge.domain.run_showing`), an entrant's texts there with no output
    named, `texts_at` where each is shown and what holds it, for
    `read_text` and never served; and `reveal_at`, until then, when the
    task reveals, when what its entrant reads changes with no grading's
    change to say so, so a page open on it reads it again then, as at a
    classic group's `shown_at`.
    """

    id: uuid.UUID
    attempt: int
    status: SubmissionState | GradingStatus
    stopped: str | None
    outcome: str | None
    groups: tuple[GroupShown, ...]
    values: dict[str, Any]
    reason: str | None = None
    limit: int | None = None
    meanings: tuple[ValueMeaning, ...] = ()
    runs: tuple[Mapping[str, Any], ...] = ()
    reveal_at: datetime | None = None
    run_values: tuple[ValueShown, ...] = ()
    once: Mapping[str, Any] = field(default_factory=dict)
    compiled: int | None = None
    facts: Mapping[str, Fraction] = field(default_factory=dict)
    texts_at: Mapping[TextAt, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Submission:
    """One of the contestant's submissions of a task: its number, when it was
    taken, and the latest attempt of its grading.
    """

    task: TaskId
    number: int
    submitted_at: datetime
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
    person = await submitters.person(ctx, session, task, submitting=True)
    workspace = person.workspace
    opened = False
    if (
        workspace is not None
        and not _uploads_of(inputs)
        and not await _submitted_before(ctx, workspace, task)
    ):
        # A first submission of nothing but typed values names no upload, so
        # no slot made its place; it is made here, by the rules a submit is
        # checked against first, and before the hold below, since making it
        # takes the forge seconds and the hold keeps the connection.
        await submitters.refuse(ctx, person)
        await submitters.open_place(ctx, person)
        opened = True
    # The task is held shared from here and read again under it: a save of
    # the task waits for this submit, or this submit reads what the save
    # published, so a grading change's regrade never misses it
    # (TASK-FORMAT.md section 2, step 12). Another task's save never waits.
    await timelines.hold_task(ctx, task, shared=True)
    person = await submitters.person(ctx, session, task, submitting=True)
    workspace = person.workspace
    if workspace is not None:
        await _hold(ctx, workspace, task)
        person = await submitters.hold_standing(ctx, person)
        again = await _again(ctx, person, workspace, idempotency_key)
        if again is not None:
            return again
    # The submission's time is when it was checked open, not when the forge
    # has recorded it, which may be seconds later and past the close.
    at = ctx.now
    _, workspace = await submitters.refuse(ctx, person)
    made = await _listed(ctx, workspace, task) or ()
    await _finish_unrecorded(ctx, person, workspace, made)
    caps = person.published.definition.submissions
    uncounted = await _uncounted(ctx, workspace, task)
    if len([found for found in made if found.id not in uncounted]) >= caps.max:
        raise SubmissionLimit(
            f"You have made all {caps.max} submissions this task allows.", limit=caps.max
        )
    await _refuse_rate(ctx, person, workspace)
    chosen = await _chosen(ctx, person, inputs)
    if not made and not chosen and not opened:
        # Naming an upload means a slot made the place already, and a first
        # submission of typed values made it before the hold; one whose
        # gradings are all gone still has its place made here.
        await submitters.open_place(ctx, person)
    form = await entrant_view.form(ctx, person.published)
    uploaded = {upload.id: _uploaded(upload) for upload in chosen.values()}
    layout = (
        rules.lay_out_runs(form.fields, form.parts, inputs, uploaded)
        if form.runs
        else rules.lay_out(form.fields, form.tests, inputs, uploaded)
    )
    files = {SUBMISSION_FILE: layout.document}
    user = AsUser(person.session.user_id, await sessions.credential_for(ctx, person.session.id))
    for path, upload in layout.files.items():
        files[path] = await _pointer(ctx, person, user, chosen[upload])
    recorded = await _record(ctx, user, workspace, task, files, idempotency_key)
    grading = await _insert(
        ctx, person, workspace, recorded, idempotency_key, at=at, by=person.session.user_id
    )
    for used in chosen.values():
        used.status = UploadStatus.CONSUMED
        used.consumed_by = recorded.id
    await ctx.db.flush()
    log.info(
        "submissions.submitted",
        task=task,
        number=recorded.number,
        user_id=person.session.user_id,
    )
    return await _submission(ctx, Reading.of(person, workspace), recorded.number, at, [grading])


@action
async def mine(ctx: Context, session: Session, task: TaskId) -> tuple[Submission, ...]:
    """The signed-in person's own submissions of the task, newest first."""
    person = await submitters.person(ctx, session, task)
    workspace = person.workspace
    if workspace is None:
        return ()
    return tuple(await _read(ctx, Reading.of(person, workspace)))


@action
async def one(ctx: Context, session: Session, task: TaskId, number: int) -> Submission:
    """One of the signed-in person's own submissions of the task, by its
    number. `NotFound` for one that is not theirs.
    """
    person = await submitters.person(ctx, session, task)
    workspace = person.workspace
    found = await _read(ctx, Reading.of(person, workspace), number) if workspace is not None else []
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


async def _uncounted(ctx: Context, workspace: WorkspaceId, task: TaskId) -> set[str]:
    """The workspace's submissions of the task that do not count against its
    `submissions.max`, by the rule a delayed one is graded again by
    (`gradings.place_of`): those none of whose attempts finished with a
    result and whose latest is a cancel or over the limit, void as on the
    leaderboards, or reads as a system error, delayed. Whether the CI lost a run
    is not asked here, under the submit's holds: such a run counts until a
    read finds it lost and its retry starts.
    """
    rows = (
        await ctx.db.execute(
            select(Grading).where(Grading.workspace_id == workspace, Grading.task_id == task, FRONT)
        )
    ).scalars()
    grouped: dict[str, list[Grading]] = {}
    for row in rows:
        grouped.setdefault(row.submission_id, []).append(row)
    return {
        submission
        for submission, attempts in grouped.items()
        if gradings.place_of(ctx, attempts) is not Place.HOLDS
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
    ctx: Context, person: Person, workspace: WorkspaceId, key: str
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
                    Grading.task_id == person.task,
                    Grading.idempotency_key == key,
                )
            )
        )
        .scalars()
        .all()
    )
    if rows:
        number = rows[0].submission_number
        log.info("submissions.repeated", task=person.task, number=number)
        return (await _read(ctx, Reading.of(person, workspace), number))[0]
    found = next(
        (made for made in await _listed(ctx, workspace, person.task) or () if made.key == key),
        None,
    )
    if found is None:
        return None
    existing = await _read(ctx, Reading.of(person, workspace), found.number)
    if existing:
        return existing[0]
    # The key is the submitter's own, so the submission found by it is theirs.
    grading = await _recover(ctx, person, workspace, found, by=person.session.user_id)
    return await _submission(ctx, Reading.of(person, workspace), found.number, found.at, [grading])


async def _finish_unrecorded(
    ctx: Context, person: Person, workspace: WorkspaceId, made: Sequence[Submitted]
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
                .where(Grading.workspace_id == workspace, Grading.task_id == person.task)
                .distinct()
            )
        ).scalars()
    )
    for found in made:
        if found.id not in graded:
            await _recover(ctx, person, workspace, found)


async def _recover(
    ctx: Context,
    person: Person,
    workspace: WorkspaceId,
    found: Submitted,
    *,
    by: int | None = None,
) -> Grading:
    """The grading row of a submission the forge holds and the database does
    not, inserted with the key its note carries and `by` as whoever made it:
    the submitter sending it again with their own key, or none when it was
    found otherwise, since the forge's answer does not say; and the uploads
    it used marked consumed.
    """
    grading = await _insert(ctx, person, workspace, found, found.key, at=found.at, by=by)
    await _consume_used(ctx, person, found)
    await ctx.db.flush()
    log.info("submissions.recovered", task=person.task, number=found.number)
    return grading


async def _consume_used(ctx: Context, person: Person, found: Submitted) -> None:
    """Mark consumed, by `found`, the uploads its files point at: for each
    pointer, the oldest checked upload, by when it was kept on the
    package's clock (`expires_at` less one `LIFETIME`), for the task with its digest of
    whoever works in the workspace, the entrant's members. A
    submission whose `submission.json` does not read marks nothing, and its
    uploads lapse as any unused one does.
    """
    user = AsUser(person.session.user_id, await sessions.credential_for(ctx, person.session.id))
    try:
        document = _document(await _file(ctx, user, found.id, SUBMISSION_FILE))
    except NotFound, ValueError:
        log.warning("submissions.recovered_unreadable", submission=found.id)
        return
    digests: list[str] = []
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
                UploadRow.owner_user_id.in_(person.members or (person.session.user_id,)),
                UploadRow.task_id == person.task,
                UploadRow.purpose == UploadPurpose.SUBMISSION,
                UploadRow.status == UploadStatus.VERIFIED,
                UploadRow.digest.in_(set(digests)),
            )
            .order_by(UploadRow.expires_at, UploadRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    waiting: dict[str, list[UploadRow]] = {}
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


async def _refuse_rate(ctx: Context, person: Person, workspace: WorkspaceId) -> None:
    rate = person.published.definition.submissions.rate
    since = ctx.now - rate.window
    times = (
        (
            await ctx.db.execute(
                select(func.min(Grading.submitted_at))
                .where(
                    Grading.workspace_id == workspace,
                    Grading.task_id == person.task,
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
            f"This task takes {rate.count} {'submission' if rate.count == 1 else 'submissions'} "
            f"every {rate.per} seconds. Wait a moment, then submit again.",
            rate=f"{rate.count} per {rate.per}s",
            retry_at=retry_at.isoformat(),
        )


async def _chosen(
    ctx: Context, person: Person, inputs: Mapping[str, SubmittedInput]
) -> dict[uuid.UUID, UploadRow]:
    """Every upload the submit names, held until the unit of work ends, once
    each is the person's own for this task, checked, unused and in time, and
    each file within its input's `max_size`.
    """
    named = _uploads_of(inputs)
    rows = await uploads.owned(ctx, person.session.user_id, person.task, set(named))
    foreign = sorted({str(upload) for upload in named if upload not in rows})
    if foreign:
        raise UploadNotYours(
            "A file in this submission is not one you uploaded for this task. Upload it again.",
            uploads=foreign,
        )
    unready = sorted(
        str(row.id)
        for row in rows.values()
        if row.status != UploadStatus.VERIFIED or row.expires_at <= ctx.now
    )
    if unready:
        raise UploadNotReady(
            "A file in this submission has not finished uploading, or was submitted already. "
            "Upload it again.",
            uploads=unready,
        )
    form = await entrant_view.form(ctx, person.published)
    declared = {entry.id: entry for entry in form.fields}
    for row in sorted(rows.values(), key=lambda row: str(row.id)):
        entry = declared.get(row.input_id or "")
        if entry is not None and _size(row) > entry.max_size:
            raise TooLarge(
                f"A file for {entry.id} is larger than the {entry.max_size} bytes allowed. "
                "Choose a smaller file.",
                limit=entry.max_size,
                input=entry.id,
            )
    return rows


def _uploads_of(inputs: Mapping[str, SubmittedInput]) -> list[uuid.UUID]:
    """Every upload the submit names, a run task's parts' among them."""
    return [
        upload
        for given in inputs.values()
        for each in (given, *given.parts.values())
        for upload in each.uploads
    ]


def _size(row: UploadRow) -> int:
    return row.size


def _uploaded(row: UploadRow) -> UploadedFile:
    return UploadedFile(
        upload=row.id, input=row.input_id or "", filename=row.filename, size=_size(row)
    )


async def _pointer(ctx: Context, person: Person, as_: AsUser, row: UploadRow) -> bytes:
    """What the commit holds in place of the file: the pointer to the object
    the forge keeps. The forge is asked once more, here, that the place still
    holds it, so a submission never commits a pointer to bytes that are not
    there.
    """
    place = SubmissionPlace(person.workspace, person.task) if person.workspace else None
    if place is None or not await uploads.holds(ctx, place, as_, row):
        log.warning("submissions.upload_gone", upload=str(row.id))
        raise UploadNotReady(
            "A file you uploaded is no longer kept. Upload it again.", uploads=[str(row.id)]
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
        raise Forbidden(
            "Your place to submit this task does not take your submissions. Tell the organisers."
        ) from exc
    except Conflict as exc:
        log.warning("submissions.numbering_failed", task=task, detail=exc.detail)
        raise Conflict(
            "Other submissions were being made at the same moment. Submit again."
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


async def _insert(
    ctx: Context,
    person: Person,
    workspace: WorkspaceId,
    recorded: Submitted,
    key: str | None,
    *,
    at: datetime,
    by: int | None,
) -> Grading:
    """The submission's queued grading row, against the task's current
    publication, with the hash of its own callback token and the member
    who submitted it, `by`, taking the released split while the task's
    results are released.
    """
    released = bool(person.published.definition.runs) and await entrant_view.results_released(
        ctx, person.task
    )
    return gradings.queue_submission(
        ctx,
        task=person.task,
        workspace=workspace,
        submission=recorded,
        publication=person.published.publication,
        key=key,
        at=at,
        submitted_by=by,
        released_split=released,
    )


@action
async def organised(
    ctx: Context, organiser: Organiser, task: TaskId, owner: WorkspaceOwner, number: int
) -> Submission:
    """The submission numbered `number` of the entrant `owner`, as organisers
    read it: as its row reads it, with everything its
    row is not shown yet filled in, each group's `shown_at` saying when the
    row is shown it, and the grading's own status. Needs the observer role
    at the task, as its gradings do. `NotFound` for a task with no
    publication or a row with no such submission.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    settings = await entrant_view.contest(ctx, contest_id_of(task_scope(task)))
    found = await entrant_view.task(ctx, task, settings)
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


@action
async def read_text(ctx: Context, session: Session, task: TaskId, number: int, at: TextAt) -> str:
    """A text the signed-in person's own submission of the task shows them
    now, by where it shows it (`TextAt`): the stored output that holds it
    is found here and never told them. `NotFound` for a place that shows
    no text now, and for a submission that is not theirs.
    """
    person = await submitters.person(ctx, session, task)
    workspace = person.workspace
    found = await _read(ctx, Reading.of(person, workspace), number) if workspace is not None else []
    if not found:
        raise NotFound(NO_SUCH_SUBMISSION)
    grading = found[0].grading
    held = grading.texts_at.get(at) if grading is not None else None
    if held is None:
        raise NotFound(NO_SUCH_TEXT)
    return await _text(ctx, *held)


@action
async def read_organised_text(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    owner: WorkspaceOwner,
    number: int,
    output: str,
    name: str,
) -> str:
    """A text the submission numbered `number` of the entrant `owner` names,
    as organisers read it (`organised`), by the stored output that holds it
    and that output's name. `NotFound` for one it does not name.
    """
    grading = (await organised(ctx, organiser, task, owner, number)).grading
    if grading is None or (output, name) not in run_showing.texts(grading.runs, grading.once):
        raise NotFound(NO_SUCH_TEXT)
    return await _text(ctx, output, name)


async def _text(ctx: Context, output: str, name: str) -> str:
    """The text of the name `name` that the stored output `output` holds,
    and nothing else of it, since it may be another entrant's of the same
    org (stage 4 open 114).
    """
    try:
        held = await ctx.db.get(StepOutput, uuid.UUID(output))
    except ValueError:
        held = None
    found = held.outputs.get(name) if held is not None else None
    if not isinstance(found, str):
        raise NotFound(NO_SUCH_TEXT)
    return found


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
    def of(cls, person: Person, workspace: WorkspaceId) -> Reading:
        return cls(person.task, person.settings, person.published, workspace)


async def _read(
    ctx: Context, reading: Reading, number: int | None = None, *, everything: bool = False
) -> list[Submission]:
    """The workspace's submissions of the task from their grading rows,
    newest first, or the one numbered `number`; with `everything`, as
    organisers read them.
    """
    query = select(Grading).where(
        Grading.workspace_id == reading.workspace, Grading.task_id == reading.task, FRONT
    )
    if number is not None:
        query = query.where(Grading.submission_number == number)
    rows = (await ctx.db.execute(query)).scalars().all()
    grouped: dict[int, list[Grading]] = {}
    for row in rows:
        grouped.setdefault(row.submission_number, []).append(row)
    latest = [_latest(found) for found in grouped.values()]
    gone = await gradings.lost(ctx, latest)
    await gradings.notice(ctx, latest, gone)
    await gradings.notice_late(ctx, latest)
    graded = scores.GradedOf(ctx, reading.published)
    chosen = None if everything or not grouped else await _chosen_by_leaderboards(ctx, reading)
    return [
        await _submission(
            ctx,
            reading,
            found,
            min(row.submitted_at for row in grouped[found]),
            grouped[found],
            gone,
            graded,
            chosen,
            everything=everything,
        )
        for found in sorted(grouped, reverse=True)
    ]


async def _chosen_by_leaderboards(ctx: Context, reading: Reading) -> leaderboards.Chosen | None:
    """What the leaderboards' cells counted of the row's submissions to the task,
    which its values reading an entered mark wait on; none when the task
    has no such value."""
    scoring = reading.published.definition.scoring
    if scoring is None or not using(scoring.values, ENTERED):
        return None
    return await leaderboards.chosen(
        ctx,
        contest_id_of(task_scope(reading.task)),
        reading.settings,
        ctx.forge.workspaces.owner_of(reading.workspace),
        reading.published.name,
    )


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
    graded: scores.GradedOf | None = None,
    chosen: leaderboards.Chosen | None = None,
    *,
    everything: bool = False,
) -> Submission:
    """The submission as its owner reads it, by the attempt of its grading
    that counts (`scores.counted`), with `lost` naming the gradings whose
    runs the CI has lost and `chosen` what the leaderboards' cells counted of
    the row's submissions; with `everything`, as organisers read it.
    """
    settings = reading.settings
    task = reading.published.name
    if not rows:
        return Submission(reading.task, number, at, None)
    contest = contest_id_of(task_scope(reading.task))
    extension = await timelines.of_owner(
        ctx, contest, ctx.forge.workspaces.owner_of(reading.workspace)
    )
    row = scores.counted(rows)
    status = gradings.status_of(ctx, row, lost)
    reveal_at = await timelines.reveal(ctx, contest, settings, task)
    result = await _result(
        ctx,
        graded or scores.GradedOf(ctx, reading.published),
        row,
        status,
        reveal_at,
        settings,
        extension,
        chosen,
        everything=everything,
    )
    return Submission(reading.task, number, at, result)


async def _result(
    ctx: Context,
    graded_of: scores.GradedOf,
    row: Grading,
    status: GradingStatus,
    reveal_at: datetime | None,
    settings: ContestDefinition,
    extension: Extension,
    chosen: leaderboards.Chosen | None = None,
    *,
    everything: bool = False,
) -> Result:
    """The grading as its contestant may see it now, with the publication it
    ran under. A run in `system_error` is told as delayed, with
    nothing of it shown, and one staff cancelled as cancelled, with the
    sentence they gave; but a run task's row that failed with the runs it
    gave is read as one done, its faulted runs waiting, still told as
    delayed. With `everything`, as organisers read it: where it stands in
    its own words, and shown as after the reveal, each group's `shown_at`
    kept from what its contestant is shown now.
    """
    state: SubmissionState | GradingStatus = status if everything else told(status)
    if (
        row.result is not None
        and row.result.get("schema_version") == RESULT_VERSION
        and status in scores.FINISHED_WITH_RESULT
    ):
        return await _runs(
            ctx,
            graded_of.current,
            row,
            state,
            reveal_at,
            settings,
            extension,
            chosen,
            everything=everything,
        )
    if status is GradingStatus.CANCELLED:
        return Result(row.id, row.attempt, state, None, None, (), {}, row.cancel_reason)
    if status is GradingStatus.OVER_LIMIT:
        return Result(row.id, row.attempt, state, None, None, (), {}, limit=row.submission_limit)
    if status is not GradingStatus.DONE or row.result is None:
        return Result(row.id, row.attempt, state, None, None, (), {})
    graded = await graded_of.graded(PublicationId(row.publication_id))
    if graded is None:
        return Result(row.id, row.attempt, state, None, None, (), {})
    revealed = reveal_at is not None and ctx.now >= reveal_at
    seen = shown(row.result, graded.groups, graded.sealed, revealed=revealed, reveal_at=reveal_at)
    if everything:
        seen = filled_in(
            seen,
            shown(row.result, graded.groups, graded.sealed, revealed=True, reveal_at=reveal_at),
        )
    return Result(
        row.id,
        row.attempt,
        state,
        seen.stopped,
        seen.outcome,
        seen.groups,
        dict(seen.values),
        meanings=shown_meanings(
            graded.meanings, graded.sealed.values, revealed=revealed or everything
        ),
    )


async def _runs(
    ctx: Context,
    current: PublishedTask,
    row: Grading,
    state: SubmissionState | GradingStatus,
    reveal_at: datetime | None,
    settings: ContestDefinition,
    extension: Extension,
    chosen: leaderboards.Chosen | None,
    *,
    everything: bool,
) -> Result:
    """A run task's grading as its reader sees it now, from both its rows
    (11.3): its values worked out on read (`scores.run_scored`) and shown
    by what the task's latest publication shows now, `on_release` while
    its results are released (`run_showing.shown`, 9.4), so a group moved
    to `now` shows its runs even where they ran late. Its values are
    worked out as if no mark were entered until a leaderboard cell shown to the
    entrant counted the submission, of those `chosen` gives, and never when
    none did (14), so a mark held back tells nothing a submission with none
    does not. Before the reveal,
    and with no release, the late row is told to no entrant; after it,
    while that attempt has no result, the grading is told as where it
    stands, `grading` or `delayed`; and a task with
    nothing shown during the contest is told as `submitted` until the
    reveal. Organisers read every value and every run of both rows at
    once, and where each row stands in the gradings routes.
    """
    late = await scores.late_of(ctx, row)
    revealed = reveal_at is not None and ctx.now >= reveal_at
    marks: dict[str, Fraction] | None = None
    if not everything:
        counted = chosen or leaderboards.Chosen()
        number = row.submission_number
        if not entries_open(number, counted.shown):
            # Worked out with no mark, so it reads as a submission with none.
            marks = {}
    scored = await scores.run_scored(ctx, current, settings, extension, row, late, marks)
    released = not everything and await entrant_view.results_released(ctx, current.id)
    scoring = current.definition.scoring
    assert scoring is not None, "a run task's definition always has scoring"
    seen = run_showing.shown(
        scoring,
        scored.held.runs,
        scored.held.files,
        scored.whole,
        scored.early,
        scored.self_reported,
        revealed=revealed,
        released=released,
        everything=everything,
        late=scored.late,
    )
    runs, once = seen.runs, seen.once
    texts_at: dict[run_showing.TextAt, tuple[str, str]] = {}
    if not everything:
        texts_at = run_showing.addresses(runs, once)
        runs, once = run_showing.bare(runs, once)
        if seen.later and state is SubmissionState.GRADED:
            state = SubmissionState.SUBMITTED
        if late is not None and revealed and state is SubmissionState.GRADED:
            status = gradings.status_of(ctx, late)
            if status is not GradingStatus.DONE:
                state = told(status)
    coming = reveal_at if reveal_at is not None and not revealed and not everything else None
    return Result(
        row.id,
        row.attempt,
        state,
        None,
        None,
        (),
        {},
        runs=runs,
        reveal_at=coming,
        run_values=seen.values,
        once=once,
        compiled=seen.compiled,
        facts=seen.facts,
        texts_at=texts_at,
    )


async def _own(
    ctx: Context, session: Session, task: TaskId, number: int
) -> tuple[SubmissionId, AsUser]:
    """One of the signed-in person's own submissions, and the identity its
    files are read as. `NotFound` for one that is not theirs.
    """
    person = await submitters.person(ctx, session, task)
    workspace = person.workspace
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
    user = AsUser(person.session.user_id, await sessions.credential_for(ctx, person.session.id))
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
