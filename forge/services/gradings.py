"""The gradings of submissions: the rows a submit, a retry, a rejudge and the
operator's reconcile make, starting each one's run at the CI, what a run of
one is in the port's words, and the organiser's controls over them.

A grading is one row per submission and attempt, and nothing about
one is ever edited into another: a retry and a rejudge make new attempts,
each a new row with a new id and so new secrets, and the old rows stay as
they were, so what was graded when stays readable. Each row is inserted
`queued` with the SHA-256 of its run's callback token, and its run is
started as soon as the unit of work that made it commits (`start`), since
the CI asks the platform about the grading while the start is under way. A
start the CI refuses, or does not answer, ends the grading in
`system_error` saying why: whoever reads it sees that at once and tries
again, a contestant by submitting, an organiser with `retry`.

A grading whose run the CI has lost reads as a system error saying so,
like an overdue one, and nothing is written when it is read: `lost` asks
the CI where the runs of `dispatched` gradings are, once they have waited
long enough for that to mean something (`grading.worth_asking`), at most
once every `RUN_STATE_KEPT` for each run however many people are watching
it, and with the reader's connection let go of first.

An organiser managing the task reads its gradings and acts on one:

- `cancel` ends a submission whose latest grading reads as a system error,
  stored or because it is overdue or lost while its row still waits, when a
  regrade would only repeat the fault: the grading is `cancelled` with a
  sentence its contestant reads, and a run of it still at the CI is stopped
  once the cancel has committed, so it does not hold a machine. The cancel
  is final: a cancelled submission does not count against the task's
  `submissions.max`, a retry is refused, and a rejudge leaves it as it is;
- `retry` makes a new attempt of a submission's latest grading once it is
  finished, against the publication that attempt graded against, unless
  staff cancelled it or another attempt of it is still being graded. One that reads as finished
  only because it is overdue or lost is ended first, with the reason
  written on its row, and the old run is cancelled at the CI once the
  retry has committed, so it does not keep a machine's containers going;
- `rejudge` makes a new attempt of every submission's latest attempt,
  against the task's current publication, as a save that publishes a
  change to how the task grades does (`regrade`).
  A latest attempt still being graded against an older publication is
  cancelled first; one being graded against the current one is left to
  finish, and a submission staff cancelled stays cancelled;
- `run_log` reads a grading's run log, for an organiser observing the
  task: it names every test, hidden ones too, so it is the organisers'
  alone. It is read whole up to `RUN_LOG_MAX` bytes and refused above
  (`log_too_large`), and a store that fails is told in fixed words.

An organiser of a contest reads its gradings together:

- `feed` lists every grading of the contest's tasks the organiser observes,
  newest first, filtered by task, by who submitted, a contestant's own and
  their teams' while they were in them, and by status, each with who
  submitted it, a contestant by username or a team by name, as `list` gives
  a task's; every attempt is a row of its own;
- `queue_depth` counts the ones waiting for a machine by status, `queued`
  and `dispatched`, one that is overdue or lost not among them, since it
  reads as a system error.

Both see the tasks the organiser observes: every task for an observer of
the contest or above, and otherwise the tasks they hold a role at, the rest
left out. Someone holding no role in the contest is refused.

A grading is named by its id, which is no access control: an organiser who
does not observe the task is told there is no such grading. `task_of` gives
the task a grading is of, so the host's guard can check the organiser there
before a control whose route names only the grading.
"""

import builtins
import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, func, not_, or_, select, tuple_

from forge.db.tables import Grading, Team
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import (
    Conflict,
    Forbidden,
    LogTooLarge,
    Misconfigured,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
    WrongStatus,
)
from forge.domain.grading import (
    AT_THE_CI,
    FINISHED,
    LOST_CHECK_AFTER,
    MACHINE_WAIT,
    PLATFORM_POOL,
    RUN_LOG_MAX,
    START_WAIT,
    UNFINISHED,
    WAITING,
    GradingRun,
    GradingStatus,
    RunState,
    callback_token,
    cancel_reason,
    envelope_key,
    log_key,
    overdue,
    token_hash,
    worth_asking,
)
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import (
    ContestId,
    OrgId,
    PublicationId,
    RunId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
    new_id,
)
from forge.domain.live import Nudge, NudgeKind
from forge.domain.names import TeamOwner, UserOwner, is_username
from forge.domain.publications import Publication
from forge.domain.roles import (
    Role,
    ScopeKind,
    contest_id_of,
    contest_scope,
    holds,
    task_id_of,
    task_scope,
)
from forge.domain.submissions import Submitted
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, org_accounts, published, teams
from forge.services.access import Organiser, require
from forge.services.credentials import CannotDecrypt

log = get_logger(__name__)

NO_SUCH_GRADING = "There is no such grading."
NO_LOG = "This grading has no run log."
LOG_STORE_UNAVAILABLE = "The run log could not be read; try again in a moment."
LIST_LIMIT = 500
RUN_STATE_KEPT = timedelta(seconds=15)
"""How long this process keeps the CI's answer about where one run is, so a
contestant's page polling every two seconds asks the CI a few times a
minute at most."""

ACCOUNT_NOT_READY = "The org's grading account is not ready."
NOT_ACTIVATED = "The task is not taken for grading at the CI."
REFUSED = "The CI refused the org's grading account."
NO_RUN = "The CI answered without starting a run."
NO_ANSWER = "The CI did not answer."
PUBLICATION_GONE = "The publication it grades against is gone."

CI_CONFIG_PATH = "/api/v1/ci/config"
"""Where the CI asks what a run is, the configuration extension, under the
platform's internal URL."""
ENVELOPE_PATH = "/api/v1/gradings/{grading}/envelope"
"""Where a run fetches its envelope, under the machine URL, with the envelope
key as `?key=`."""
CALLBACK_PATH = "/api/v1/gradings/{grading}/callback"
"""Where a run reports, under the machine URL."""


@dataclass(frozen=True, slots=True)
class GradingRecord:
    """One grading as an organiser reads it: which submission and attempt,
    whether that is the submission's latest attempt, the one staff act on,
    against which publication, where it stands, why when it is a system
    error, the sentence staff cancelled it with, the result as it came
    back, whether its log was written, the last progress its run reported,
    and its times.
    """

    id: uuid.UUID
    task: TaskId
    workspace: WorkspaceId
    submission_number: int
    submitted_at: datetime
    publication: PublicationId
    attempt: int
    latest: bool
    status: GradingStatus
    error: str | None
    result: dict[str, Any] | None
    log: bool
    progress: dict[str, Any] | None
    queued_at: datetime
    dispatched_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime | None
    cancel_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Submitter:
    """Who made a submission, as an organiser recognises them: a contestant,
    by user id and username, or a team, by id and name. The name is none
    once the account or the team is gone, or when the forge did not say.
    """

    user_id: int | None
    team: uuid.UUID | None
    name: str | None


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One grading as an organiser reads it among a task's or a contest's:
    the grading, who made the submission it grades, and its task's name and
    label, the letter
    of its place in the contest's `tasks`; the label is none once the
    contest no longer lists the task, or when its settings do not read or
    the forge does not say, and the name none for a task the platform has
    no name for.
    """

    grading: GradingRecord
    by: Submitter
    task_name: str | None
    label: str | None


@dataclass(frozen=True, slots=True)
class QueueDepth:
    """How many of a contest's gradings wait for a machine: `queued`, whose
    run is not started yet, and `dispatched`, whose run the CI holds until a
    machine takes it.
    """

    queued: int
    dispatched: int


@dataclass(frozen=True, slots=True)
class Rejudged:
    """What a rejudge did: the publication the new attempts grade against,
    how many it queued, how many unfinished attempts against an older
    publication it cancelled first, and how many it left to finish against
    the current one.
    """

    task: TaskId
    publication: PublicationId
    queued: int
    cancelled: int
    left_running: int


def new_row(
    ctx: Context,
    *,
    task: TaskId,
    workspace: WorkspaceId,
    submission: SubmissionId,
    number: int,
    version: VersionId,
    submitted_at: datetime,
    publication: PublicationId,
    attempt: int,
    key: str | None,
) -> Grading:
    """A new `queued` grading, added to the unit of work, with the hash of
    its run's callback token, whose run starts once the unit of work
    commits.
    """
    row = Grading(
        id=new_id(),
        task_id=task,
        workspace_id=workspace,
        submission_id=submission,
        submission_number=number,
        submission_version=version,
        submitted_at=submitted_at,
        publication_id=publication,
        attempt=attempt,
        idempotency_key=key,
        status=GradingStatus.QUEUED,
        queued_at=ctx.now,
    )
    row.callback_token_hash = token_hash(callback_token_of(ctx, row))
    ctx.db.add(row)
    changed(ctx, row)
    grading = row.id

    async def start_it(later: Context) -> None:
        await start(later, grading)

    ctx.after_commit(start_it)
    return row


def callback_token_of(ctx: Context, row: Grading) -> str:
    """The token the grading's run reports back with."""
    return callback_token(ctx.settings.token_encryption_key_bytes, row.id)


def envelope_key_of(ctx: Context, row: Grading) -> str:
    """The key the URL of the envelope of the grading's run carries."""
    return envelope_key(ctx.settings.token_encryption_key_bytes, row.id)


async def start(ctx: Context, grading: uuid.UUID) -> None:
    """Start the run of a grading that is still `queued`, as its org's
    account, and record it: the grading is then `dispatched`, waiting for a
    machine. A start that fails ends the grading in `system_error`, saying
    why in the platform's words; what the CI said goes to the log.

    The call to the CI holds neither a connection nor a lock, since the CI
    asks the platform about the grading while the start is under way, and a
    rush of starts must leave it a connection to answer on. So what the
    start needs is read and that much committed, the run is started, and
    only then is the row taken and the run recorded, or the run cancelled
    when the grading moved on meanwhile. The work after a commit owns its
    unit of work, which is what lets it commit partway.

    A grading of a long batch, a rejudge or a reconcile pass, may already
    read as overdue by the time its turn comes; it is started all the same,
    since its row still says `queued`, unless an organiser has made a later
    attempt of it meanwhile.
    """
    row = await find(ctx, grading)
    if row is None or row.status != GradingStatus.QUEUED:
        return
    if await _superseded(ctx, row):
        log.info("gradings.start_superseded", grading=str(grading))
        return
    try:
        account = await org_accounts.identity(ctx, org_of(row))
    except (NotFound, CannotDecrypt) as exc:
        await _not_started(ctx, grading, exc, ACCOUNT_NOT_READY)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    try:
        run = await run_of(ctx, row)
    except NotFound as exc:
        await _not_started(ctx, grading, exc, PUBLICATION_GONE)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    await ctx.db.commit()
    try:
        found = await _start_run(ctx, account, run)
    except (PortError, CannotDecrypt) as exc:
        reason = _start_failure(exc) if isinstance(exc, PortError) else ACCOUNT_NOT_READY
        await _not_started(ctx, grading, exc, reason)
        return
    row = await find(ctx, grading, lock=True)
    if row is None or row.status != GradingStatus.QUEUED or await _superseded(ctx, row):
        log.info("gradings.start_overtaken", grading=str(grading), run=found)
        await _cancel_quietly(ctx, found)
        return
    row.status = GradingStatus.DISPATCHED
    row.run_id = found
    row.dispatched_at = ctx.now
    changed(ctx, row)
    log.info("gradings.dispatched", grading=str(row.id), run=found)


async def _superseded(ctx: Context, row: Grading) -> bool:
    """Whether a later attempt of the same submission exists."""
    later = await ctx.db.scalar(
        select(Grading.id)
        .where(
            Grading.submission_id == row.submission_id,
            Grading.attempt > row.attempt,
        )
        .limit(1)
    )
    return later is not None


async def _start_run(ctx: Context, account: AsOrgAccount, run: GradingRun) -> RunId:
    """Start the run, and when the CI refuses the org's account, sign it in
    again and try once more, so a login the CI lost heals at the next start.
    Nothing is held while the CI is called.
    """
    try:
        return await ctx.forge.grading.start_run(account, run)
    except Forbidden:
        account = await org_accounts.renew(ctx, account)
        await ctx.db.commit()
        return await ctx.forge.grading.start_run(account, run)


async def _not_started(ctx: Context, grading: uuid.UUID, exc: Exception, reason: str) -> None:
    """End the grading in `system_error` with `reason`, unless it moved on
    meanwhile.
    """
    log.warning(
        "gradings.start_failed", grading=str(grading), error=type(exc).__name__, detail=str(exc)
    )
    row = await find(ctx, grading, lock=True)
    if row is not None and row.status == GradingStatus.QUEUED:
        finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=reason)


async def _cancel_quietly(ctx: Context, run: RunId) -> None:
    try:
        await ctx.forge.grading.cancel_run(run)
    except PortError as exc:
        log.warning("gradings.cancel_failed", run=run, error=type(exc).__name__)


def _start_failure(exc: PortError) -> str:
    match exc:
        case Forbidden():
            return REFUSED
        case Rejected():
            return NO_RUN
        case NotFound():
            return NOT_ACTIVATED
    return NO_ANSWER


def queue_submission(
    ctx: Context,
    *,
    task: TaskId,
    workspace: WorkspaceId,
    submission: Submitted,
    publication: Publication,
    key: str | None,
    at: datetime,
) -> Grading:
    """The submission's queued grading, attempt 1, against `publication`."""
    return new_row(
        ctx,
        task=task,
        workspace=workspace,
        submission=submission.id,
        number=submission.number,
        version=submission.version,
        submitted_at=at,
        publication=publication.id,
        attempt=1,
        key=key,
    )


async def find(ctx: Context, grading: uuid.UUID, *, lock: bool = False) -> Grading | None:
    """The grading by its id, held until the unit of work ends when `lock`,
    and then read afresh.
    """
    query = select(Grading).where(Grading.id == grading)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await ctx.db.execute(query)).scalar_one_or_none()


def envelope_url(ctx: Context, row: Grading) -> str:
    """Where the grading's current run fetches its envelope, key included."""
    path = ENVELOPE_PATH.format(grading=row.id)
    return f"{_machine_url(ctx)}{path}?key={envelope_key_of(ctx, row)}"


def callback_url(ctx: Context, grading: uuid.UUID) -> str:
    """Where the grading's run reports."""
    return f"{_machine_url(ctx)}{CALLBACK_PATH.format(grading=grading)}"


def _machine_url(ctx: Context) -> str:
    return str(ctx.settings.machine_url).rstrip("/")


async def publication_of(ctx: Context, row: Grading) -> Publication:
    """The publication the grading grades against. `NotFound` when the task
    no longer has it.
    """
    for found in await ctx.forge.workspaces.list_publications(TaskId(row.task_id)):
        if found.id == row.publication_id:
            return found
    raise NotFound(f"{row.task_id} has no publication {row.publication_id}")


async def run_of(ctx: Context, row: Grading) -> GradingRun:
    """What a run of the grading is, in the port's words."""
    publication = await publication_of(ctx, row)
    return GradingRun(
        grading=row.id,
        task=TaskId(row.task_id),
        publication=PublicationId(row.publication_id),
        publication_version=publication.version,
        submission=SubmissionId(row.submission_id),
        submission_version=VersionId(row.submission_version),
        envelope_url=envelope_url(ctx, row),
        compute=PLATFORM_POOL,
    )


def org_of(row: Grading) -> OrgId:
    return OrgId(task_scope(TaskId(row.task_id)).org)


def finish(ctx: Context, row: Grading, status: GradingStatus, *, error: str | None = None) -> None:
    """End the grading with `status`, waiting for nothing more."""
    row.status = status
    row.finished_at = ctx.now
    row.error = error
    changed(ctx, row)


def changed(ctx: Context, row: Grading) -> None:
    """Nudge whoever may hear of the grading that it moved, once the unit of
    work commits: the contestant whose workspace it grades, and the
    organisers who observe its task. Every write of a grading's status or
    progress calls this, which is what moves a contestant's submissions
    while they watch; a grading is the platform's own and no push from the
    forge will ever name one.
    """
    owner = ctx.forge.workspaces.owner_of(WorkspaceId(row.workspace_id))
    ctx.nudge(
        Nudge(
            NudgeKind.GRADING,
            str(row.id),
            user=owner.user_id if isinstance(owner, UserOwner) else None,
            team=str(owner.team_id) if isinstance(owner, TeamOwner) else None,
            scope=task_scope(TaskId(row.task_id)),
        )
    )


def staff_cancelled(row: Grading) -> bool:
    """Whether staff ended the grading's submission by cancelling it, which,
    while it is the latest attempt, is final: the submission is left out of
    what a task's `submissions.max` counts, and a retry, a rejudge and a
    save's regrade grade it no more.
    """
    return row.status == GradingStatus.CANCELLED and row.cancel_reason is not None


def overdue_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> str | None:
    """Why the grading is past what its state may take, or none. `lost`
    names the gradings whose runs the CI was found to have lost.
    """
    return overdue(
        GradingStatus(row.status),
        created_at=row.queued_at,
        dispatched_at=row.dispatched_at,
        deadline=row.deadline_at,
        now=ctx.now,
        lost=row.id in lost,
    )


def status_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> GradingStatus:
    """Where the grading stands now: one past what its state may take, or
    whose run the CI has lost, is a system error (`grading.overdue`).
    """
    if overdue_of(ctx, row, lost) is not None:
        return GradingStatus.SYSTEM_ERROR
    return GradingStatus(row.status)


def _overdue(now: datetime) -> ColumnElement[bool]:
    """The rows `overdue` finds past what their state may take at `now`,
    told in the query: `queued` `START_WAIT` after it was made,
    `dispatched` `MACHINE_WAIT` after its run was started, and `running`
    past its deadline.
    """
    return or_(
        and_(Grading.status == GradingStatus.QUEUED, Grading.queued_at <= now - START_WAIT),
        and_(
            Grading.status == GradingStatus.DISPATCHED,
            Grading.dispatched_at.is_not(None),
            Grading.dispatched_at <= now - MACHINE_WAIT,
        ),
        and_(
            Grading.status == GradingStatus.RUNNING,
            Grading.deadline_at.is_not(None),
            Grading.deadline_at <= now,
        ),
    )


def _worth_asking(now: datetime) -> ColumnElement[bool]:
    """The rows `lost` asks the CI about at `now`, told in the query as
    `worth_asking` tells them: `dispatched` with a run, `LOST_CHECK_AFTER`
    after it was started and not yet `MACHINE_WAIT`.
    """
    return and_(
        Grading.status == GradingStatus.DISPATCHED,
        Grading.run_id.is_not(None),
        Grading.dispatched_at.is_not(None),
        Grading.dispatched_at <= now - LOST_CHECK_AFTER,
        Grading.dispatched_at > now - MACHINE_WAIT,
    )


def _may_read_as(status: GradingStatus, now: datetime) -> ColumnElement[bool]:
    """The rows that may read as `status` at `now`, as `status_of` reads
    them, all but whether the CI lost a run told in the query: for
    `system_error`, the rows stored so, overdue, or whose run the CI is to
    be asked about; for an unfinished status, the rows stored so and not
    overdue, a lost one among them left out once the CI is asked; and for
    a finished one, the rows stored so.
    """
    stored = Grading.status == status
    if status == GradingStatus.SYSTEM_ERROR:
        return or_(stored, _overdue(now), _worth_asking(now))
    if status in UNFINISHED:
        return and_(stored, not_(_overdue(now)))
    return stored


async def lost(ctx: Context, rows: Iterable[Grading]) -> frozenset[uuid.UUID]:
    """The gradings among `rows` whose run the CI no longer holds, or holds
    as finished while the grading still waits for its envelope: a run that
    ended before the harness began, its checkout failed or the run killed at
    the CI, is as lost as one the CI dropped. Only a grading worth asking
    about is asked about, each run at most once every `RUN_STATE_KEPT` by
    this process, and the connection is let go of before the CI is asked. A
    CI that does not answer loses nothing: the grading reads as its row
    says.
    """
    asking = [
        (row.id, RunId(row.run_id))
        for row in rows
        if row.run_id is not None
        and worth_asking(GradingStatus(row.status), row.dispatched_at, ctx.now)
    ]
    if not asking:
        return frozenset()
    await ctx.let_go()
    found: set[uuid.UUID] = set()
    for grading, run in asking:
        if await _run_state(ctx, grading, run) in (RunState.LOST, RunState.FINISHED):
            found.add(grading)
    return frozenset(found)


async def _run_state(ctx: Context, grading: uuid.UUID, run: RunId) -> RunState | None:
    async def ask() -> RunState | None:
        try:
            state = await ctx.forge.grading.run_state(run)
        except PortError as exc:
            log.warning("gradings.run_state_unanswered", run=run, error=type(exc).__name__)
            return None
        if state is RunState.LOST:
            log.warning("gradings.run_lost", grading=str(grading), run=run)
        return state

    return await ctx.memo.remembered(f"gradings.run_state.{run}", RUN_STATE_KEPT, ask)


@action
async def task_of(ctx: Context, grading: uuid.UUID) -> TaskId:
    """The task a grading is of, the scope the host's guard checks an
    organiser at before a control that names only the grading. `NotFound`
    for no such grading.
    """
    row = await find(ctx, grading)
    if row is None:
        raise NotFound(NO_SUCH_GRADING)
    return TaskId(row.task_id)


@action
async def cancel(
    ctx: Context, organiser: Organiser, grading: uuid.UUID, reason: str
) -> GradingRecord:
    """End a submission whose latest grading reads as a system error by
    cancelling that grading, with `reason`, a sentence its contestant reads.
    One that reads so only because it is overdue or lost has that written
    on its row as its error, and its run at the CI is stopped once the
    cancel has committed, holding nothing while the CI is called.
    `InvalidReason` for an empty sentence or one over `CANCEL_REASON_MAX`
    characters, `WrongStatus` for a grading that is not a system error, and
    `Conflict` for one with a later attempt, which is the one to cancel.
    """
    sentence = cancel_reason(reason)
    found = await _managed(ctx, organiser, grading, lock=False)
    gone = await lost(ctx, [found])
    attempts = await _attempts(ctx, [found.submission_id])
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status is not GradingStatus.SYSTEM_ERROR:
        raise WrongStatus(
            f"Only a grading in system_error is cancelled; this one is {status.value}.",
            current=status.value,
        )
    if any(other.attempt > row.attempt for other in attempts):
        raise Conflict("A later attempt of this submission exists; cancel that one.")
    stored = GradingStatus(row.status)
    if stored in AT_THE_CI and row.run_id is not None:
        _cancel_after_commit(ctx, RunId(row.run_id))
    finish(ctx, row, GradingStatus.CANCELLED, error=overdue_of(ctx, row, gone) or row.error)
    row.cancel_reason = sentence
    await ctx.db.flush()
    log.info("gradings.cancelled", grading=str(row.id), user_id=organiser.user.id)
    return record(ctx, row, latest=True)


@action
async def retry(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> GradingRecord:
    """A new attempt of a submission's latest grading once it is finished,
    against the publication it graded against, the old attempt kept as it
    is. `WrongStatus` for one that is not finished and for a submission
    staff cancelled, which that ends, and `Conflict` for one with a later
    attempt, which is the one to retry, or while another attempt of it is
    graded.
    """
    found = await _managed(ctx, organiser, grading, lock=False)
    gone = await lost(ctx, [found])
    attempts = await _attempts(ctx, [found.submission_id])
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status not in FINISHED:
        raise WrongStatus(f"The grading is {status.value}, not finished.", current=status.value)
    if staff_cancelled(row):
        raise WrongStatus(
            "Staff cancelled this submission, which ends it; it is not graded again.",
            current=status.value,
        )
    if any(other.attempt > row.attempt for other in attempts):
        raise Conflict("A later attempt of this submission exists; retry that one.")
    if any(status_of(ctx, other, gone) in UNFINISHED for other in attempts):
        raise Conflict("Another attempt of this grading is still being graded.")
    stored = GradingStatus(row.status)
    if stored in UNFINISHED:
        finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=overdue_of(ctx, row, gone))
    if row.run_id is not None and stored not in (GradingStatus.DONE, GradingStatus.CANCELLED):
        _cancel_after_commit(ctx, RunId(row.run_id))
    made = _next_attempt(ctx, row, PublicationId(row.publication_id), attempts)
    await ctx.db.flush()
    log.info("gradings.retried", grading=str(row.id), attempt=made.attempt)
    return record(ctx, made, latest=True)


def _cancel_after_commit(ctx: Context, run: RunId) -> None:
    """Cancel the run at the CI once the unit of work has committed, holding
    nothing while the CI is called; a run the CI does not stop is refused its
    reports, since its grading is finished.
    """

    async def cancel(after: Context) -> None:
        await _cancel_quietly(after, run)

    ctx.after_commit(cancel)


@action
async def rejudge(ctx: Context, organiser: Organiser, task: TaskId) -> Rejudged:
    """A new attempt of every submission's latest attempt, against the
    task's current publication. `NotFound` for a task with no publication.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    current = await published.task(ctx, task)
    if current is None:
        raise NotFound("The task has no publication to grade against.")
    return await regrade(ctx, task, current.publication.id)


@action
async def list(
    ctx: Context, organiser: Organiser, task: TaskId, *, limit: int = 100
) -> tuple[FeedEntry, ...]:
    """The task's gradings, newest first, at most `limit` of them and never
    more than 500, each with who submitted it, as the feed gives them, for
    an organiser observing the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task)
                .order_by(Grading.created_at.desc(), Grading.id.desc())
                .limit(max(1, min(limit, LIST_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    last = await _latest(ctx, rows)
    gone = await lost(ctx, rows)
    return await _entries(
        ctx,
        contest_id_of(task_scope(task)),
        [record(ctx, row, gone, latest=row.id in last) for row in rows],
    )


@action
async def feed(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    *,
    task: TaskId | None = None,
    user: str | None = None,
    team: uuid.UUID | None = None,
    status: GradingStatus | None = None,
    limit: int = 100,
) -> tuple[FeedEntry, ...]:
    """The gradings of the contest's tasks the organiser observes, newest
    first, at most `limit` of them and never more than 500: of one `task`,
    of the submissions of the contestant whose username is `user`, their
    own and their teams' while they were in them, or of the team `team`,
    and reading as `status`, each filter when given. A task,
    contestant or team the organiser cannot see there gives none, and so
    does a `user` that breaks the forge's username rule (`is_username`).
    `Forbidden` for someone holding no role in the contest.
    """
    observed = _observed(organiser, contest)
    where = [_in_contest(contest, observed)]
    if task is not None:
        if observed is not None and task not in observed:
            return ()
        where.append(Grading.task_id == task)
    if user is not None:
        # A name no one can hold at the forge matches nobody, and is never
        # sent there.
        if not is_username(user):
            return ()
        try:
            found = await ctx.forge.identity.find_user_by_username(user)
        except NotFound:
            return ()
        where.append(await _submitted_by(ctx, contest, found.id))
    if team is not None:
        where.append(Grading.workspace_id == _workspace(ctx, contest, TeamOwner(team)))
    if status is not None:
        where.append(_may_read_as(status, ctx.now))
    size = max(1, min(limit, LIST_LIMIT))
    kept: builtins.list[GradingRecord] = []
    after: tuple[datetime, uuid.UUID] | None = None
    # Whether a `dispatched` row's run is lost is known only once the CI is
    # asked, so a filter by status reads a page at a time until the page is
    # full; the rest of what a status reads as is told in the query.
    while len(kept) < size:
        query = select(Grading).where(*where)
        if after is not None:
            query = query.where(tuple_(Grading.created_at, Grading.id) < after)
        rows = (
            (
                await ctx.db.execute(
                    query.order_by(Grading.created_at.desc(), Grading.id.desc()).limit(size)
                )
            )
            .scalars()
            .all()
        )
        last = await _latest(ctx, rows)
        gone = await lost(ctx, rows)
        kept.extend(
            record(ctx, row, gone, latest=row.id in last)
            for row in rows
            if status is None or status_of(ctx, row, gone) == status
        )
        if len(rows) < size:
            break
        after = (rows[-1].created_at, rows[-1].id)
    return await _entries(ctx, contest, kept[:size])


@action
async def queue_depth(ctx: Context, organiser: Organiser, contest: ContestId) -> QueueDepth:
    """How many gradings of the contest's tasks the organiser observes wait
    for a machine, by status, read in one go. `Forbidden` for someone
    holding no role in the contest.
    """
    observed = _observed(organiser, contest)
    rows = (
        (
            await ctx.db.execute(
                select(Grading).where(_in_contest(contest, observed), Grading.status.in_(WAITING))
            )
        )
        .scalars()
        .all()
    )
    gone = await lost(ctx, rows)
    counted = Counter(status_of(ctx, row, gone) for row in rows)
    return QueueDepth(
        queued=counted[GradingStatus.QUEUED], dispatched=counted[GradingStatus.DISPATCHED]
    )


def _observed(organiser: Organiser, contest: ContestId) -> frozenset[TaskId] | None:
    """The contest's tasks the organiser observes: none to mean every one,
    for an observer of the contest or above, and otherwise the tasks they
    hold a role at. `Forbidden` when that is no task at all.
    """
    scope = contest_scope(contest)
    if holds(organiser.grants, scope, Role.OBSERVER):
        return None
    tasks = frozenset(
        task_id_of(grant.scope)
        for grant in organiser.grants
        if grant.scope.kind is ScopeKind.TASK
        and (grant.scope.org, grant.scope.contest) == (scope.org, scope.contest)
    )
    if not tasks:
        named = organiser.scope if organiser.scope == scope else scope
        raise Forbidden(f"This needs the observer role at {named.name} or one of its tasks.")
    return tasks


def _in_contest(contest: ContestId, observed: frozenset[TaskId] | None) -> ColumnElement[bool]:
    if observed is None:
        return Grading.task_id.startswith(f"{contest}/", autoescape=True)
    return Grading.task_id.in_(sorted(observed))


def _workspace(ctx: Context, contest: ContestId, owner: UserOwner | TeamOwner) -> WorkspaceId:
    return ctx.forge.workspaces.workspace_of(contest, owner)


async def _submitted_by(ctx: Context, contest: ContestId, user_id: int) -> ColumnElement[bool]:
    """The gradings of the submissions made while the person worked in a
    workspace of the contest: their own, and each team's from when they
    joined it until they left, by when the submission was taken.
    """
    spans = [
        and_(
            Grading.workspace_id == _workspace(ctx, contest, TeamOwner(member.team)),
            Grading.submitted_at >= member.joined_at,
            *([Grading.submitted_at < member.left_at] if member.left_at is not None else []),
        )
        for member in await teams.memberships(ctx, contest, user_id)
    ]
    return or_(Grading.workspace_id == _workspace(ctx, contest, UserOwner(user_id)), *spans)


async def _entries(
    ctx: Context, contest: ContestId, records: Sequence[GradingRecord]
) -> tuple[FeedEntry, ...]:
    """Each grading with who submitted it and its task's name and label: the
    names and the teams read first, and the usernames and the contest's
    settings from the forge once the connection is let go of.
    """
    tasks = await names.names_of(ctx, {found.task for found in records})
    by = await _submitters(ctx, {found.workspace for found in records})
    settings = await _settings(ctx, contest) if records else None
    return tuple(
        FeedEntry(
            found,
            by[found.workspace],
            task_name=(name := tasks.get(found.task)),
            label=settings.label_of(name) if settings is not None and name is not None else None,
        )
        for found in records
    )


async def _settings(ctx: Context, contest: ContestId) -> ContestDefinition | None:
    """The contest's settings, or none when they do not read or the forge
    does not answer.
    """
    try:
        return await published.contest(ctx, contest)
    except PortError:
        return None


async def _submitters(
    ctx: Context, workspaces: Collection[WorkspaceId]
) -> dict[WorkspaceId, Submitter]:
    """Who works in each workspace, with the name an organiser knows them
    by: a team's from its row, read first, and a contestant's username from
    the forge once the connection is let go of, a forge that does not say
    leaving the name out.
    """
    owners = {workspace: ctx.forge.workspaces.owner_of(workspace) for workspace in workspaces}
    team_ids = {owner.team_id for owner in owners.values() if isinstance(owner, TeamOwner)}
    teams: dict[uuid.UUID, str] = {}
    if team_ids:
        found = await ctx.db.execute(
            select(Team.id, Team.name).where(Team.id.in_(sorted(team_ids)))
        )
        teams = {row.id: row.name for row in found}
    await ctx.let_go()
    users: dict[int, str | None] = {}
    for owner in owners.values():
        if isinstance(owner, UserOwner) and owner.user_id not in users:
            try:
                users[owner.user_id] = (await ctx.forge.identity.find_user(owner.user_id)).username
            except PortError:
                users[owner.user_id] = None
    return {
        workspace: (
            Submitter(owner.user_id, None, users[owner.user_id])
            if isinstance(owner, UserOwner)
            else Submitter(None, owner.team_id, teams.get(owner.team_id))
        )
        for workspace, owner in owners.items()
    }


@action
async def run_log(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> bytes:
    """The log of the grading's run, for an organiser observing its task.
    `NotFound` for a grading whose task they do not observe, or one with no
    log, `LogTooLarge` for a log over `RUN_LOG_MAX` bytes, which is never
    read whole, and `Unavailable` when the store fails.
    """
    row = await find(ctx, grading)
    if row is None or not holds(organiser.grants, task_scope(TaskId(row.task_id)), Role.OBSERVER):
        raise NotFound(NO_SUCH_GRADING)
    if row.log_key is None:
        raise NotFound(NO_LOG)
    key = log_key(row.id, row.attempt)
    await ctx.let_go()
    try:
        return await ctx.forge.objects.read(key, max_size=RUN_LOG_MAX)
    except NotFound as exc:
        log.warning("gradings.log_missing", grading=str(grading))
        raise NotFound(NO_LOG) from exc
    except Misconfigured as exc:
        raise _log_failure(exc, grading) from None
    except Rejected as exc:
        log.warning("gradings.log_too_large", grading=str(grading), detail=exc.detail)
        raise LogTooLarge(
            f"The run log is larger than the {RUN_LOG_MAX} bytes shown.", limit=RUN_LOG_MAX
        ) from None
    except PortError as exc:
        raise _log_failure(exc, grading) from None


def _log_failure(exc: PortError, grading: uuid.UUID) -> Unavailable:
    """What an organiser is told when the log store fails, in fixed words.
    What the store said goes to the log; its own codes name buckets and
    keys.
    """
    log.warning(
        "gradings.log_unreadable", grading=str(grading), error=type(exc).__name__, detail=exc.detail
    )
    return Unavailable(LOG_STORE_UNAVAILABLE)


def record(
    ctx: Context, row: Grading, lost: Collection[uuid.UUID] = (), *, latest: bool
) -> GradingRecord:
    """The grading as an organiser reads it, `latest` saying whether it is
    its submission's latest attempt.
    """
    late = overdue_of(ctx, row, lost)
    return GradingRecord(
        id=row.id,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission_number=row.submission_number,
        submitted_at=row.submitted_at,
        publication=PublicationId(row.publication_id),
        attempt=row.attempt,
        latest=latest,
        status=GradingStatus.SYSTEM_ERROR if late is not None else GradingStatus(row.status),
        error=late or row.error,
        result=row.result,
        log=row.log_key is not None,
        progress=row.progress,
        queued_at=row.queued_at,
        dispatched_at=row.dispatched_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        deadline_at=row.deadline_at,
        cancel_reason=row.cancel_reason,
    )


async def _latest(ctx: Context, rows: Iterable[Grading]) -> frozenset[uuid.UUID]:
    """The gradings among `rows` that are the latest attempt of their
    submission, worked out over every attempt of it, read or not.
    """
    listed = builtins.list(rows)
    submissions = sorted({row.submission_id for row in listed})
    if not submissions:
        return frozenset()
    found = await ctx.db.execute(
        select(Grading.submission_id, func.max(Grading.attempt))
        .where(Grading.submission_id.in_(submissions))
        .group_by(Grading.submission_id)
    )
    last: dict[str, int] = dict(found.all())
    return frozenset(row.id for row in listed if row.attempt == last.get(row.submission_id))


async def _managed(
    ctx: Context, organiser: Organiser, grading: uuid.UUID, *, lock: bool = True
) -> Grading:
    """The grading, held until the unit of work ends when `lock`, for an
    organiser who manages its task. `NotFound` for one whose task they do
    not observe.
    """
    row = await find(ctx, grading, lock=lock)
    if row is None:
        raise NotFound(NO_SUCH_GRADING)
    scope = task_scope(TaskId(row.task_id))
    if not holds(organiser.grants, scope, Role.OBSERVER):
        raise NotFound(NO_SUCH_GRADING)
    require(organiser, scope, Role.MANAGER)
    return row


async def _attempts(ctx: Context, submissions: Sequence[str]) -> builtins.list[Grading]:
    """Every attempt of the submissions, held until the unit of work ends and
    read afresh, in the order a rejudge takes them, so a retry and a rejudge
    never wait on each other in a circle.
    """
    return builtins.list(
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.submission_id.in_(builtins.list(submissions)))
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _next_attempt(
    ctx: Context, row: Grading, publication: PublicationId, attempts: Sequence[Grading]
) -> Grading:
    return new_row(
        ctx,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission=SubmissionId(row.submission_id),
        number=row.submission_number,
        version=VersionId(row.submission_version),
        submitted_at=row.submitted_at,
        publication=publication,
        attempt=max(other.attempt for other in attempts) + 1,
        key=None,
    )


def _stop_quietly(ctx: Context, row: Grading) -> None:
    """Cancel an unfinished grading a rejudge replaces, and its run at the CI
    once the rejudge has committed; a run the CI does not stop is refused its
    reports, since the grading is cancelled.
    """
    if GradingStatus(row.status) in AT_THE_CI and row.run_id is not None:
        _cancel_after_commit(ctx, RunId(row.run_id))
    finish(ctx, row, GradingStatus.CANCELLED)


async def regrade(ctx: Context, task: TaskId, publication: PublicationId) -> Rejudged:
    """A new attempt of every submission's latest attempt to the task,
    against `publication`, for a rejudge and for a save that publishes a
    change to how the task grades. A latest attempt still being graded
    against another publication is cancelled first, and one being graded
    against `publication` is left to finish; one that reads as finished only
    because it is overdue or lost is ended first, its old run cancelled at
    the CI once this has committed. A submission staff cancelled, its latest
    attempt `cancelled` with a sentence, is left as it is.
    """
    # Which runs the CI has lost is asked before any row is held, since
    # asking takes the CI's time.
    gone = await lost(
        ctx,
        (await ctx.db.execute(select(Grading).where(Grading.task_id == task))).scalars().all(),
    )
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task)
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, Grading] = {}
    attempts: dict[str, builtins.list[Grading]] = {}
    for row in rows:
        latest[row.submission_id] = row
        attempts.setdefault(row.submission_id, []).append(row)
    queued = cancelled = left_running = 0
    for submission, row in latest.items():
        if staff_cancelled(row):
            continue
        status = status_of(ctx, row, gone)
        if status in UNFINISHED:
            if row.publication_id == publication:
                left_running += 1
                continue
            _stop_quietly(ctx, row)
            cancelled += 1
        elif GradingStatus(row.status) in UNFINISHED:
            finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=overdue_of(ctx, row, gone))
            if row.run_id is not None:
                _cancel_after_commit(ctx, RunId(row.run_id))
        _next_attempt(ctx, row, publication, attempts[submission])
        queued += 1
    await ctx.db.flush()
    log.info(
        "gradings.rejudged",
        task=task,
        publication=publication,
        queued=queued,
        cancelled=cancelled,
        left_running=left_running,
    )
    return Rejudged(task, publication, queued, cancelled, left_running)
