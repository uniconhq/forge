"""The gradings of submissions: the rows a submit, a retry, a rejudge and the
operator's reconcile make, starting each one's run at the CI, what a run of
one is in the port's words, and the organiser's controls over them.

A grading is one row per submission and attempt, and nothing about
one is ever edited into another: a retry and a rejudge make new attempts,
each a new row with a new id and so new secrets, and the old rows stay as
they were, so what was graded when stays readable. Each row is inserted
`queued` with the SHA-256 of its run's callback token, and its run is
started as soon as the unit of work that made it commits (`start`), since
a CI that asks may ask the platform about the grading while the start is
under way. A
start the CI refuses, or does not answer, ends the grading in
`system_error` saying why: whoever reads it sees that at once and tries
again, a contestant by submitting, an organiser with `retry`.

A grading whose run the CI has lost reads as a system error saying so,
like an overdue one, and nothing is written when it is read: `lost` asks
the CI where the runs of `dispatched` gradings are, once they have waited
long enough for that to mean something (`grading.worth_asking`), at most
once every `RUN_STATE_KEPT` for each run however many people are watching
it, and with the reader's connection let go of first.

A latest attempt that ends in `system_error` is tried again once by itself
(`notice`): whatever finds it so, the CI's report, a start that fails, or
any read of it that finds it overdue or lost, starts the retry once its
unit of work commits, in the background (`Context.in_background`), and
nothing runs on a timer. The retry holds the task and the submission's
attempts as a staff retry does, and makes a new attempt, marked
`automatic`, only while the failed one is still the latest and was not
itself made so; so two readers finding the failure at once make one
retry, and a second failure waits for staff. A retry that never starts is
started by the next read that finds the failure.

Of a submission's attempts the latest that finished with a result counts,
on the boards, for relative credit, to its row and against
`submissions.max`, until a newer attempt finishes with one: a failure or a
cancel never takes a result away (`scores.usable`).

A delayed submission, one with no result whose latest attempt reads as a
system error, holds no place under `submissions.max` while it waits, so the
first `max` to get a result are the ones that count: its retry by itself, a
staff retry and a regrade grade it again only while its entrant's
submissions with a result, those waiting that are not delayed, and the
delayed ones made before it are fewer than the most
(`forge.domain.grading.room`), read under the task's hold. Otherwise the
new attempt ends `over_limit` at once, run by nothing, with the most it
reached; the submission then counts on no board and against nothing. One
over the limit is checked again the same way by a staff retry or a
regrade, and a regrade leaves one still without room as it is. No one
grades one past the most; the organisers' list says when a retry would end
over the limit (`retry_limit`), before it runs.

An organiser managing the task reads its gradings and acts on one:

- `cancel` ends a submission whose latest grading reads as a system error,
  stored or because it is overdue or lost while its row still waits, when a
  regrade would only repeat the fault: the grading is `cancelled` with a
  sentence its contestant reads, and a run of it still at the CI is stopped
  once the cancel has committed, so it does not hold a machine. The cancel
  is final: a retry is refused and a rejudge leaves it as it is. A
  submission that never had a result is then void and does not count
  against the task's `submissions.max`; one that had keeps it;
- `retry` makes a new attempt of a submission's latest grading once it is
  finished, against the publication that attempt graded against, unless
  staff cancelled it or another attempt of it is still being graded. One
  that reads as finished only because it is overdue or lost is ended
  first, with the reason written on its row, and the old run is cancelled
  at the CI once the retry has committed, so it does not keep a machine's
  containers going. A delayed or over-the-limit submission is held to its
  entrant's room, as above;
- `rejudge` makes a new attempt of every submission's latest attempt,
  against the task's current publication, as a save that publishes a
  change to how the task grades does (`regrade`).
  A latest attempt still being graded against an older publication is
  cancelled first; one being graded against the current one is left to
  finish, a submission staff cancelled stays cancelled, and delayed and
  over-the-limit ones are held to their entrant's room, the earliest
  first. A rejudge or a save's regrade that makes an attempt is kept as one
  `Regrade`, what started it, who and when, and every attempt it makes,
  and each retry of one of those, names it (`regrade_id`), so what it
  changed can be read (`forge.services.regrades`);
- `run_log` reads a grading's run log, for an organiser observing the
  task: it names every test, hidden ones too, so it is the organisers'
  alone. It is read whole up to `RUN_LOG_MAX` bytes and refused above
  (`log_too_large`), and a store that fails is told in fixed words.

A run task's grading runs as Woodpecker pipelines (`forge.services.pipelines`,
phase 4.9), a row each: the `early` row is the grading as its entrant and
every reader above knows it (`FRONT`), and a `late` row, made when the early
one ends `done`, follows it, told to organisers alone. A row's attempts are
counted on its line, its submission's rows of its pipeline, so a late row is
retried, cancelled and found lost by itself as any row is, and only the
early row decides whether a submission holds a place under
`submissions.max`. Every read of early rows that looks for failures looks
at their submissions' latest late rows too (`notice_late`), since no
entrant reads those. A `save` row checks a publication rather than a
submission; no list here reads it, and its organisers' reads look for its
failures (`pipelines.checks`).

An organiser of a contest reads its gradings together:

- `feed` lists every grading of the contest's tasks the organiser observes,
  newest first by when each was queued (the package's clock, never the
  database's), filtered by task, by the member who submitted, by entrant,
  and by status, each with whose it is, the entrant by the name it is shown
  under and the member who submitted it by username, as `list` gives a
  task's; every attempt is a row of its own, or, with `latest`, only each
  submission's latest attempt, so `status=system_error` with `latest` lists
  the failed gradings that wait for staff;
- `queue_depth` counts the ones waiting for a machine by status, `queued`
  and `dispatched`, one that is overdue or lost not among them, since it
  reads as a system error; and the ones that failed and wait for staff,
  `failed`: the latest attempts that read as a system error, the ones
  `feed` lists with `status=system_error` and `latest`.

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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, exists, func, not_, or_, select, tuple_
from sqlalchemy.orm import aliased

from forge.db.tables import Entrant, Grading, Regrade
from forge.db.tables.gradings import EARLY, FRONT, LATE, SAVE
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
    PipelineRun,
    Place,
    RunSpec,
    RunState,
    callback_token,
    cancel_reason,
    envelope_key,
    log_key,
    overdue,
    room,
    token_hash,
    worth_asking,
)
from forge.domain.identity import PLATFORM, AsOrgAccount
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
from forge.domain.names import EntrantOwner, is_username
from forge.domain.plans import PLAN_PATH, Plan
from forge.domain.publications import Publication
from forge.domain.regrades import Compared, RegradeCause
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
from forge.services import entrants, names, org_accounts, pipelines, published, timelines
from forge.services.access import Organiser, require
from forge.services.credentials import CannotDecrypt

log = get_logger(__name__)

NO_SUCH_GRADING = "There is no such grading."
NO_LOG = "This grading has no log."
LOG_STORE_UNAVAILABLE = "The grading's log could not be read. Try again in a moment."
LIST_LIMIT = 500
RUN_STATE_KEPT = timedelta(seconds=15)
"""How long this process keeps the CI's answer about where one run is, so a
contestant's page polling every two seconds asks the CI a few times a
minute at most."""
START_RETRY_WAIT = timedelta(minutes=1)
"""How long a grading whose run could not be started waits before a read
may start its retry by itself, so the one retry is not spent against a CI
that refused a moment ago; still nothing runs on a timer."""

NOTICED_KEPT = timedelta(seconds=30)
"""How long this process remembers that it started the retry of a failed
grading, so a crowd of readers finding the failure together starts it once;
a retry that never ran is started by the next read after this."""

ACCOUNT_NOT_READY = "The org's grading account is not ready."
NOT_ACTIVATED = "The task is not set up for grading yet."
REFUSED = "The grading service refused the org's grading account."
NO_RUN = "The grading service answered without starting the grading."
NO_ANSWER = "The grading service did not answer."
PUBLICATION_GONE = "The publication it grades against is gone."

CI_CONFIG_PATH = "/api/v1/ci/config"
"""Where a CI that asks what a run is asks it, under the platform's internal
URL."""
ENVELOPE_PATH = "/api/v1/gradings/{grading}/envelope"
"""Where a run fetches its envelope, under the machine URL, with the envelope
key as `?key=`."""
CALLBACK_PATH = "/api/v1/gradings/{grading}/callback"
"""Where a run reports, under the machine URL."""


Listed = Literal["early", "late"]
"""The rows an organiser's list reads: every one but a save's."""


@dataclass(frozen=True, slots=True)
class GradingRecord:
    """One grading as an organiser reads it: which submission and attempt,
    who of the entrant submitted it, none when that is not known, whether
    that is the submission's latest attempt, the one staff act on,
    against which publication, where it stands, why when it is a system
    error, the sentence staff cancelled it with, the result as it came
    back, whether its log was written, the last progress its run reported,
    its times, and whether the platform made it by itself, retrying the
    attempt before it (`automatic`).

    On a latest attempt that has not finished with a result, still being
    graded, a system error or staff cancelled, `last_good` is the latest
    earlier attempt that did, the one its submission counts meanwhile; none
    on any other, and when there is no such attempt.

    `limit` is the task's `submissions.max` an `over_limit` attempt ended
    against. On a latest attempt of a submission with no result that reads
    as a system error or over the limit, `retry_limit` is the task's
    `submissions.max` when a retry of it now would end over the limit, so
    the retry's confirmation can say so before it runs; none otherwise.
    `regrade` names the regrade of many the attempt belongs to, and
    `change`, given only where the gradings are read by regrade, what that
    regrade did to the submission.
    """

    id: uuid.UUID
    task: TaskId
    workspace: WorkspaceId
    submission_number: int
    submitted_at: datetime
    publication: PublicationId
    attempt: int
    latest: bool
    submitted_by: int | None
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
    automatic: bool = False
    last_good: int | None = None
    limit: int | None = None
    retry_limit: int | None = None
    regrade: uuid.UUID | None = None
    change: Compared | None = None
    pipeline: Listed = EARLY
    early: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Submitter:
    """Whose a submission is, as an organiser recognises it: the entrant, by
    its id and the name it is shown under, none once it is gone; and the
    member who submitted it, by user id and username, none when a reconcile
    found it at the forge, the username none when the forge did not say.
    """

    entrant: uuid.UUID
    entrant_name: str | None
    user_id: int | None
    username: str | None


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One grading as an organiser reads it among a task's or a contest's:
    the grading, who made the submission it grades, and its task's name and
    label, the letter of its place in the contest's `tasks`. The label is
    none once the contest no longer lists the task, or when its settings do
    not read or the forge does not say, and the name none for a task the
    platform has no name for.
    """

    grading: GradingRecord
    by: Submitter
    task_name: str | None
    label: str | None


@dataclass(frozen=True, slots=True)
class QueueDepth:
    """How many of a contest's gradings wait: for a machine, `queued`, whose
    run is not started yet, and `dispatched`, whose run the CI holds until a
    machine takes it; and for staff, `failed`, the latest attempts of
    submissions that read as a system error.
    """

    queued: int
    dispatched: int
    failed: int


@dataclass(frozen=True, slots=True)
class Rejudged:
    """What a rejudge did: the publication the new attempts grade against,
    how many it queued, how many unfinished attempts against an older
    publication it cancelled first, how many it left to finish against
    the current one, how many delayed submissions it ended over the limit,
    their entrants having no room left under `submissions.max`, and the
    regrade it is recorded as, none when it made no attempt.
    """

    task: TaskId
    publication: PublicationId
    queued: int
    cancelled: int
    left_running: int
    over_limit: int = 0
    regrade: uuid.UUID | None = None


def new_row(
    ctx: Context,
    *,
    task: TaskId,
    workspace: str | None,
    submission: str | None,
    number: int | None,
    version: str | None,
    submitted_at: datetime | None,
    publication: PublicationId,
    attempt: int,
    key: str | None,
    submitted_by: int | None,
    automatic: bool = False,
    regrade: uuid.UUID | None = None,
    over_limit: int | None = None,
    pipeline: str = EARLY,
    early: uuid.UUID | None = None,
    unwaited: bool = False,
) -> Grading:
    """A new `queued` grading, added to the unit of work, with the hash of
    its run's callback token, whose run starts once the unit of work
    commits, in the background when it is `unwaited`, since nobody asking
    waits on it; a start that never runs leaves the row `queued`, which a
    read finds overdue and retries (`notice`). `automatic` when the platform
    makes it by itself, retrying the
    attempt before it, and `regrade` naming the regrade of many it belongs
    to. With `over_limit`, the task's `submissions.max` its entrant has no
    room left under, it is ended `over_limit` at once instead, and nothing
    runs. A run task's late row names the `early` row it follows, and a
    save row has no submission.
    """
    row = Grading(
        id=new_id(),
        task_id=task,
        workspace_id=workspace,
        submitted_by=submitted_by,
        submission_id=submission,
        submission_number=number,
        submission_version=version,
        submitted_at=submitted_at,
        publication_id=publication,
        attempt=attempt,
        idempotency_key=key,
        status=GradingStatus.QUEUED,
        queued_at=ctx.now,
        automatic=automatic,
        regrade_id=regrade,
        pipeline=pipeline,
        early_id=early,
    )
    row.callback_token_hash = token_hash(callback_token_of(ctx, row))
    ctx.db.add(row)
    if over_limit is not None:
        row.submission_limit = over_limit
        finish(ctx, row, GradingStatus.OVER_LIMIT)
        return row
    changed(ctx, row)
    grading = row.id

    async def start_it(later: Context) -> None:
        await start(later, grading)

    if unwaited:
        ctx.in_background(start_it)
    else:
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

    The call to the CI holds neither a connection nor a lock, since a CI
    that asks may ask the platform about the grading while the start is
    under way, and a rush of starts must leave it a connection to answer on. So what the
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
        planned = row.pipeline != EARLY or await pipelines.planned(ctx, row)
    except Unavailable as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    if planned:
        await _start_pipeline(ctx, grading, row, account)
        return
    try:
        run = await run_of(ctx, row)
    except NotFound as exc:
        await _not_started(ctx, grading, exc, PUBLICATION_GONE)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    try:
        spec = spec_of(ctx, await plan_of(ctx, run))
    except pipelines.ImageWithdrawn as exc:
        await _not_started(ctx, grading, exc, str(exc))
        return
    except (NotFound, Forbidden, ValidationError) as exc:
        # Refused as the CI refused a run whose plan its question could not
        # be answered from, before the plan was read at the start.
        await _not_started(ctx, grading, exc, NO_RUN)
        return
    except PortError as exc:
        # Likewise a plan the forge could not hand over, which the CI
        # answered with no run.
        await _not_started(ctx, grading, exc, NO_RUN)
        return
    await ctx.db.commit()
    try:
        found = await _start_run(ctx, account, run, spec)
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
    """Whether a later attempt on the row's line exists."""
    later = await ctx.db.scalar(
        select(Grading.id).where(_on_line(row), Grading.attempt > row.attempt).limit(1)
    )
    return later is not None


def _on_line(row: Grading) -> ColumnElement[bool]:
    """The rows on `row`'s line: its submission's of its pipeline, or a save
    row's publication's."""
    if row.pipeline == SAVE:
        return and_(Grading.pipeline == SAVE, Grading.publication_id == row.publication_id)
    return and_(Grading.pipeline == row.pipeline, Grading.submission_id == row.submission_id)


async def _start_pipeline(
    ctx: Context, grading: uuid.UUID, row: Grading, account: AsOrgAccount
) -> None:
    """`start` for a run task's row: its steps' settings minted, a user
    taken for it and committed, then its pipeline started on the org's
    dispatch repository, which is made and activated the first time a
    start finds it missing. A pipeline with nothing left to run is not
    started: the row is concluded at once from what the store holds, the
    platform's images needed or not.
    """
    try:
        run = await pipelines.prepare(ctx, row)
    except (pipelines.NoImage, pipelines.ImageWithdrawn) as exc:
        await _not_started(ctx, grading, exc, str(exc))
        return
    except NotFound as exc:
        await _not_started(ctx, grading, exc, PUBLICATION_GONE)
        return
    except (Misconfigured, Forbidden, Conflict) as exc:
        await _not_started(ctx, grading, exc, NO_RUN)
        return
    except PortError as exc:
        await _not_started(ctx, grading, exc, NO_ANSWER)
        return
    if run is None:
        return
    await ctx.db.commit()
    try:
        found = await _start_on_dispatch(ctx, account, run)
    except (PortError, CannotDecrypt) as exc:
        reason = _start_failure(exc) if isinstance(exc, PortError) else ACCOUNT_NOT_READY
        await _not_started(ctx, grading, exc, reason)
        return
    held = await find(ctx, grading, lock=True)
    if held is None or held.status != GradingStatus.QUEUED or await _superseded(ctx, held):
        log.info("gradings.start_overtaken", grading=str(grading), run=found)
        await _cancel_quietly(ctx, found)
        return
    held.status = GradingStatus.DISPATCHED
    held.run_id = found
    held.dispatched_at = ctx.now
    changed(ctx, held)
    log.info("gradings.dispatched", grading=str(held.id), run=found, pipeline=held.pipeline)


async def _start_on_dispatch(ctx: Context, account: AsOrgAccount, run: PipelineRun) -> RunId:
    """Start the pipeline, making and activating the org's dispatch
    repository first when the CI does not know it, and signing the org's
    account in again once when the CI refuses it, as `_start_run` does.
    """
    try:
        return await _start_signed_in(ctx, account, run)
    except NotFound:
        log.info("gradings.dispatch_made", org=run.org)
        await ctx.forge.content.create_dispatch(run.org)
        try:
            await ctx.forge.grading.activate_dispatch(account, run.org)
        except Forbidden:
            account = await org_accounts.renew(ctx, account)
            await ctx.db.commit()
            await ctx.forge.grading.activate_dispatch(account, run.org)
        return await _start_signed_in(ctx, account, run)


async def _start_signed_in(ctx: Context, account: AsOrgAccount, run: PipelineRun) -> RunId:
    try:
        return await ctx.forge.grading.start_pipeline(account, run)
    except Forbidden:
        account = await org_accounts.renew(ctx, account)
        await ctx.db.commit()
        return await ctx.forge.grading.start_pipeline(account, run)


async def _start_run(ctx: Context, account: AsOrgAccount, run: GradingRun, spec: RunSpec) -> RunId:
    """Start the run, and when the CI refuses the org's account, sign it in
    again and try once more, so a login the CI lost heals at the next start.
    Nothing is held while the CI is called.
    """
    try:
        return await ctx.forge.grading.start_run(account, run, spec)
    except Forbidden:
        account = await org_accounts.renew(ctx, account)
        await ctx.db.commit()
        return await ctx.forge.grading.start_run(account, run, spec)


async def _not_started(ctx: Context, grading: uuid.UUID, exc: Exception, reason: str) -> None:
    """End the grading in `system_error` with `reason`, unless it moved on
    meanwhile. Its retry by itself is left to the first read that finds it
    `START_RETRY_WAIT` after (`notice`).
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
    submitted_by: int | None,
) -> Grading:
    """The submission's queued grading, attempt 1, against `publication`,
    made by the member `submitted_by`, none when that is not known.
    """
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
        submitted_by=submitted_by,
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


async def plan_of(ctx: Context, run: GradingRun) -> Plan:
    """The plan of the publication `run` grades against, as it froze it.
    `NotFound` or `Forbidden` when the file cannot be read, and pydantic's
    `ValidationError` when it is not a plan.
    """
    found = await ctx.forge.content.read_file(
        PLATFORM, run.task, PLAN_PATH, at=run.publication_version
    )
    return Plan.from_bytes(found.content)


def spec_of(ctx: Context, plan: Plan) -> RunSpec:
    """What a run of `plan` runs: the harness image it names and the
    platform's clone image. `ImageWithdrawn` when a step of the plan runs
    an image the platform has withdrawn, which the harness would run.
    """
    for step in plan.steps:
        if ctx.settings.withdraws(step.image):
            raise pipelines.ImageWithdrawn(pipelines.WITHDRAWN.format(image=step.image))
    return RunSpec(harness_image=plan.harness_image, clone_image=ctx.settings.clone_image)


def org_of(row: Grading) -> OrgId:
    return OrgId(task_scope(TaskId(row.task_id)).org)


def finish(ctx: Context, row: Grading, status: GradingStatus, *, error: str | None = None) -> None:
    """End the grading with `status`, waiting for nothing more."""
    row.status = status
    row.finished_at = ctx.now
    row.error = error
    changed(ctx, row)


def changed(ctx: Context, row: Grading, *, organisers_only: bool = False) -> None:
    """Nudge whoever may hear of the grading that it moved, once the unit of
    work commits: the members of the entrant whose workspace it grades, and the
    organisers who observe its task. Every write of a grading's status or
    progress calls this, which is what moves a contestant's submissions
    while they watch; a grading is the platform's own and no push from the
    forge will ever name one. Progress goes to the organisers alone
    (`organisers_only`): a per-test step reports it once a test, hidden ones
    too, so a nudge to the row on each would count and time a hidden
    group's tests (TASK-FORMAT.md section 3.5). So does every move of a run
    task's late or save row, which no entrant is told of (11.8), but for a
    late row's end once its task has revealed (`told_late`).
    """
    owner = (
        None
        if organisers_only or row.pipeline != EARLY
        else ctx.forge.workspaces.owner_of(WorkspaceId(row.workspace_id))
    )
    ctx.nudge(
        Nudge(
            NudgeKind.GRADING,
            str(row.id),
            entrant=str(owner.entrant_id) if owner is not None else None,
            scope=task_scope(TaskId(row.task_id)),
        )
    )


def told_late(ctx: Context, late: Grading) -> None:
    """Nudge the members of the entrant whose submission a late row grades
    that their grading moved, by its early row, the grading they know, once
    the unit of work commits. Only once the task has revealed, which the
    caller checks: from then on their result reads the late row
    (`submissions`), and before it no entrant hears of one (`changed`).
    """
    owner = ctx.forge.workspaces.owner_of(WorkspaceId(late.workspace_id))
    ctx.nudge(
        Nudge(
            NudgeKind.GRADING,
            str(late.early_id),
            entrant=str(owner.entrant_id),
            scope=task_scope(TaskId(late.task_id)),
        )
    )


def staff_cancelled(row: Grading) -> bool:
    """Whether staff ended the grading's submission by cancelling it, which,
    while it is the latest attempt, is final: a retry, a rejudge and a
    save's regrade grade it no more, and, unless an earlier attempt of it
    finished with a result, it is left out of what a task's
    `submissions.max` counts.
    """
    return row.status == GradingStatus.CANCELLED and row.cancel_reason is not None


def good(row: Grading) -> bool:
    """Whether an attempt finished with a result, one its submission may
    count: `GOOD` told on a row in hand.
    """
    return row.status == GradingStatus.DONE and row.result is not None


GOOD = and_(Grading.status == GradingStatus.DONE, Grading.result.is_not(None))
"""`good` told in a query."""


def latest_good(attempts: Iterable[Grading]) -> Grading | None:
    """The latest of one submission's attempts that finished with a result,
    the one the submission counts, or none while none has.
    """
    return max(
        (other for other in attempts if good(other)),
        key=lambda other: other.attempt,
        default=None,
    )


def last_good(attempts: Iterable[Grading], row: Grading) -> Grading | None:
    """The latest attempt of `row`'s submission before it that finished
    with a result, or none.
    """
    return latest_good(other for other in attempts if other.attempt < row.attempt)


def place_of(ctx: Context, attempts: Sequence[Grading], lost: Collection[uuid.UUID] = ()) -> Place:
    """Where one submission stands against its task's `submissions.max`, by
    its attempts: it holds a place once one finished with a result, and
    while its latest is still being graded; it is delayed while it has no
    result and its latest reads as a system error; and it holds none once
    that is cancelled or over the limit.
    """
    if latest_good(attempts) is not None:
        return Place.HOLDS
    status = status_of(ctx, max(attempts, key=lambda row: row.attempt), lost)
    if status in UNFINISHED or status is GradingStatus.DONE:
        return Place.HOLDS
    if status is GradingStatus.SYSTEM_ERROR:
        return Place.DELAYED
    return Place.NONE


def waits_for_room(
    ctx: Context, attempts: Sequence[Grading], lost: Collection[uuid.UUID] = ()
) -> bool:
    """Whether a submission is graded again only while its entrant has room
    under the task's `submissions.max`: none of its attempts finished with a
    result, and its latest reads as a system error, delayed, or ended over
    the limit.
    """
    if latest_good(attempts) is not None:
        return False
    latest = max(attempts, key=lambda row: row.attempt)
    return status_of(ctx, latest, lost) in (GradingStatus.SYSTEM_ERROR, GradingStatus.OVER_LIMIT)


async def task_limit(ctx: Context, task: TaskId) -> int | None:
    """The task's `submissions.max` as its latest publication says, none
    while it has none.
    """
    found = await published.task(ctx, task)
    return found.definition.submissions.max if found is not None else None


async def _places(
    ctx: Context, workspace: str, task: str, lost: Collection[uuid.UUID] = ()
) -> dict[int, Place]:
    """Where each of the workspace's submissions to the task stands against
    its `submissions.max` (`place_of`), by number.
    """
    grouped: dict[int, builtins.list[Grading]] = {}
    for row in await ctx.db.scalars(
        select(Grading).where(Grading.workspace_id == workspace, Grading.task_id == task, FRONT)
    ):
        grouped.setdefault(row.submission_number, []).append(row)
    return {number: place_of(ctx, attempts, lost) for number, attempts in grouped.items()}


async def _over_limit(
    ctx: Context, row: Grading, attempts: Sequence[Grading], lost: Collection[uuid.UUID]
) -> int | None:
    """The task's `submissions.max` when grading `row`'s submission again now,
    `attempts` being all of its attempts, would end it over the limit: it
    waits for room (`waits_for_room`) and its entrant has none left for it
    (`room`); none otherwise, and always for a run task's late row, which
    holds no place of its own. Read under the task's hold, so no submit or
    other retry takes a place meanwhile.
    """
    if row.pipeline != EARLY or not waits_for_room(ctx, attempts, lost):
        return None
    limit = await task_limit(ctx, TaskId(row.task_id))
    if limit is None:
        return None
    places = await _places(ctx, row.workspace_id, row.task_id, lost)
    number = row.submission_number
    return None if number in room(places, limit, [number]) else limit


async def retry_limits(
    ctx: Context, rows: Iterable[Grading], lost: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """For each of `rows`, latest attempts, whose retry now would end over
    the limit, the task's `submissions.max` it would end against: read
    without a hold, so it says what a retry would do as things stand.
    """
    waiting: dict[tuple[str, str], builtins.list[Grading]] = {}
    for row in rows:
        if row.pipeline == EARLY and GradingStatus(row.status) is not GradingStatus.DONE:
            waiting.setdefault((row.task_id, row.workspace_id), []).append(row)
    if not waiting:
        return {}
    found: dict[uuid.UUID, int] = {}
    limits: dict[str, int | None] = {}
    for (task, workspace), latest in waiting.items():
        attempts: dict[int, builtins.list[Grading]] = {}
        for row in await ctx.db.scalars(
            select(Grading).where(Grading.workspace_id == workspace, Grading.task_id == task, FRONT)
        ):
            attempts.setdefault(row.submission_number, []).append(row)
        asking = [
            row
            for row in latest
            if waits_for_room(ctx, attempts.get(row.submission_number, [row]), lost)
        ]
        if not asking:
            continue
        if task not in limits:
            limits[task] = await task_limit(ctx, TaskId(task))
        limit = limits[task]
        if limit is None:
            continue
        places = {number: place_of(ctx, each, lost) for number, each in attempts.items()}
        for row in asking:
            if row.submission_number not in room(places, limit, [row.submission_number]):
                found[row.id] = limit
    return found


async def notice(ctx: Context, rows: Iterable[Grading], lost: Collection[uuid.UUID] = ()) -> None:
    """Start, once this unit of work commits, the retry the platform makes by
    itself of each of `rows`, the latest attempts of their submissions, that
    reads as a system error now and was not itself such a retry, and, for
    one whose run could not be started, failed `START_RETRY_WAIT` ago or
    more (`start_waits`). Each is started once by this process in
    `NOTICED_KEPT` however many readers find it, and `retry_by_itself`
    makes it once whatever any other process does. Nothing is started
    while `retry_failed` is off.
    """
    if not ctx.settings.retry_failed:
        return
    for row in rows:
        if row.automatic or status_of(ctx, row, lost) is not GradingStatus.SYSTEM_ERROR:
            continue
        if start_waits(ctx, row):
            continue
        if not await _first_to_notice(ctx, row.id):
            continue
        grading = row.id

        async def retry_it(later: Context, grading: uuid.UUID = grading) -> None:
            await retry_by_itself(later, grading)

        ctx.in_background(retry_it)


async def notice_late(ctx: Context, fronts: Iterable[Grading]) -> None:
    """The lost check and the retry by itself (`notice`) over the latest
    late row of each submission of `fronts`, the early rows a read finds:
    no entrant reads a late row, so whatever reads its early row sees to it
    as it does to that one.
    """
    submissions = sorted({row.submission_id for row in fronts})
    if not submissions:
        return
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.submission_id.in_(submissions), Grading.pipeline == LATE)
                .order_by(Grading.submission_id, Grading.attempt.desc())
                .distinct(Grading.submission_id)
            )
        )
        .scalars()
        .all()
    )
    if rows:
        await notice(ctx, rows, await lost(ctx, rows))


def start_waits(ctx: Context, row: Grading) -> bool:
    """Whether a grading whose run could not be started (a system error
    with no run) failed less than `START_RETRY_WAIT` ago, so its retry by
    itself waits. A failure the CI reported, and an overdue or lost run,
    wait for nothing.
    """
    return (
        row.status == GradingStatus.SYSTEM_ERROR
        and row.run_id is None
        and row.finished_at is not None
        and ctx.now < row.finished_at + START_RETRY_WAIT
    )


async def _first_to_notice(ctx: Context, grading: uuid.UUID) -> bool:
    """Whether this reader is the first in this process, in `NOTICED_KEPT`,
    to find the grading failed: the first one's claim is kept, and every
    later reader is handed it and not its own.
    """
    mine = object()

    async def claim() -> object:
        return mine

    return await ctx.memo.remembered(f"gradings.noticed.{grading}", NOTICED_KEPT, claim) is mine


async def retry_by_itself(ctx: Context, grading: uuid.UUID) -> Grading | None:
    """The retry the platform makes by itself of a grading that ended in
    `system_error`, a new attempt marked `automatic` against the publication
    it graded against, or none when there is nothing to retry: the grading
    is not the latest attempt of its submission any more, does not read as a
    system error, its run could not be started and it still waits
    (`start_waits`), staff ended it, it was itself such a retry, or another
    attempt is still being graded; or, for a late row, its early row is no
    longer its submission's latest early attempt, the late row settled then
    if it still waits. The task and the submission's attempts are held as a
    staff retry holds them, so two of these at once, or one beside a staff
    retry, a cancel or a rejudge, make one attempt between them. One that
    reads failed only because it is overdue or lost is ended first, with the
    reason written on its row, its run cancelled at the CI once this
    commits. A submission with no result is graded again only while its
    entrant has room under the task's `submissions.max`, and otherwise its
    new attempt ends `over_limit` (`_over_limit`). The new attempt belongs
    to the regrade the failed one belonged to.
    """
    found = await find(ctx, grading)
    if found is None or found.automatic:
        return None
    await timelines.hold_task(ctx, TaskId(found.task_id))
    gone = await lost(ctx, [found])
    attempts = await _line(ctx, found)
    row = next(attempt for attempt in attempts if attempt.id == grading)
    if row.automatic or any(other.attempt > row.attempt for other in attempts):
        return None
    if status_of(ctx, row, gone) is not GradingStatus.SYSTEM_ERROR or start_waits(ctx, row):
        return None
    if row.pipeline == LATE and await _follows_an_earlier_attempt(ctx, row):
        _settle(ctx, row, gone)
        log.info("gradings.late_left", grading=str(row.id))
        return None
    if any(status_of(ctx, other, gone) in UNFINISHED for other in attempts):
        return None
    over = await _over_limit(ctx, row, attempts, gone)
    _settle(ctx, row, gone)
    made = _next_attempt(
        ctx, row, PublicationId(row.publication_id), attempts, automatic=True, over_limit=over
    )
    await ctx.db.flush()
    log.info(
        "gradings.retried_by_itself",
        grading=str(row.id),
        attempt=made.attempt,
        over_limit=over is not None,
    )
    return made


async def _follows_an_earlier_attempt(ctx: Context, late: Grading) -> bool:
    """Whether a late row follows an early row that is no longer its
    submission's latest early attempt, since a rejudge or a save's regrade
    made another: its runs are read by no one, so it is not tried again.
    """
    latest = await ctx.db.scalar(
        select(Grading.id)
        .where(Grading.submission_id == late.submission_id, Grading.pipeline == EARLY)
        .order_by(Grading.attempt.desc())
        .limit(1)
    )
    return latest != late.early_id


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
    cancel has committed, holding nothing while the CI is called. A
    submission an earlier attempt of which finished with a result keeps
    counting as that result; one with no such attempt is void.
    `InvalidReason` for an empty sentence or one over `CANCEL_REASON_MAX`
    characters, `WrongStatus` for a grading that is not a system error, and
    `Conflict` for one with a later attempt, which is the one to cancel. The
    task is held alone, as by a retry, so a retry by itself under way lands
    its attempt first and the cancel finds it.
    """
    sentence = cancel_reason(reason)
    found = await _managed(ctx, organiser, grading, lock=False)
    await timelines.hold_task(ctx, TaskId(found.task_id))
    gone = await lost(ctx, [found])
    attempts = await _line(ctx, found)
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status is not GradingStatus.SYSTEM_ERROR:
        raise WrongStatus(
            f"Only a grading that failed with a system error can be cancelled, and this one is "
            f"{status.value}.",
            current=status.value,
        )
    if any(other.attempt > row.attempt for other in attempts):
        raise Conflict("This submission has a later attempt. Cancel that one instead.")
    stored = GradingStatus(row.status)
    if stored in AT_THE_CI and row.run_id is not None:
        _cancel_after_commit(ctx, RunId(row.run_id))
    finish(ctx, row, GradingStatus.CANCELLED, error=overdue_of(ctx, row, gone) or row.error)
    row.cancel_reason = sentence
    await ctx.db.flush()
    log.info("gradings.cancelled", grading=str(row.id), user_id=organiser.user.id)
    good = last_good(attempts, row)
    return record(ctx, row, gone, latest=True, last_good=good.attempt if good else None)


@action
async def retry(ctx: Context, organiser: Organiser, grading: uuid.UUID) -> GradingRecord:
    """A new attempt of a submission's latest grading once it is finished,
    against the publication it graded against, the old attempt kept as it
    is. `WrongStatus` for one that is not finished and for a submission
    staff cancelled, which that ends, and `Conflict` for one with a later
    attempt, which is the one to retry, or while another attempt of it is
    graded. The task is held alone, as by a save and a rejudge, so no
    regrade numbers an attempt from rows read before this one landed. A
    submission with no result, delayed or over the limit, is graded again
    only while its entrant has room under the task's `submissions.max`, and
    otherwise the new attempt ends `over_limit` at once, as the grading's
    `retry_limit` said it would; no one grades one past the most. The new
    attempt belongs to the regrade the old one belonged to.
    """
    found = await _managed(ctx, organiser, grading, lock=False)
    await timelines.hold_task(ctx, TaskId(found.task_id))
    gone = await lost(ctx, [found])
    attempts = await _line(ctx, found)
    row = next(attempt for attempt in attempts if attempt.id == grading)
    status = status_of(ctx, row, gone)
    if status not in FINISHED:
        raise WrongStatus(
            f"This grading is {status.value} and has not finished, so it cannot be retried yet.",
            current=status.value,
        )
    if staff_cancelled(row):
        raise WrongStatus(
            "The organisers cancelled this submission, which ends it, so it is not graded again.",
            current=status.value,
        )
    if any(other.attempt > row.attempt for other in attempts):
        raise Conflict("This submission has a later attempt. Retry that one instead.")
    if any(status_of(ctx, other, gone) in UNFINISHED for other in attempts):
        raise Conflict(
            "Another attempt of this submission is still being graded. Wait for it to finish."
        )
    over = await _over_limit(ctx, row, attempts, gone)
    _settle(ctx, row, gone)
    made = _next_attempt(ctx, row, PublicationId(row.publication_id), attempts, over_limit=over)
    await ctx.db.flush()
    log.info(
        "gradings.retried", grading=str(row.id), attempt=made.attempt, over_limit=over is not None
    )
    good = latest_good(attempts)
    return record(ctx, made, latest=True, last_good=good.attempt if good else None)


def _settle(ctx: Context, row: Grading, gone: Collection[uuid.UUID]) -> None:
    """Write an attempt that reads as a system error while its row says it
    is still being graded, overdue or lost, as one, with why, and stop any
    run of it the CI may still hold once the unit of work has committed.
    """
    stored = GradingStatus(row.status)
    if stored in UNFINISHED:
        finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=overdue_of(ctx, row, gone))
    if row.run_id is not None and stored not in (GradingStatus.DONE, GradingStatus.CANCELLED):
        _cancel_after_commit(ctx, RunId(row.run_id))


async def free_user(ctx: Context, user: int) -> bool:
    """Free `user`, a run task's row's (`pipelines`), from the row not
    finished that holds it when that row is past what its state may take
    (`overdue_of`): it is written as a system error, with why, and its run
    stopped once the unit of work commits, as a retry or a cancel settles
    it. Without this, a row nobody reads again, such as an automatic retry
    whose run the CI lost, a late row after its contest, or the save row of
    a task since removed, would hold its user for good. A row another unit
    of work holds is left to it. Whether the user is free now.
    """
    holder = await ctx.db.scalar(
        select(Grading)
        .where(Grading.run_user == user, Grading.status.in_([each.value for each in UNFINISHED]))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if holder is None:
        return True
    if overdue_of(ctx, holder) is None:
        return False
    _settle(ctx, holder, ())
    await ctx.db.flush()
    log.info("gradings.user_freed", grading=str(holder.id), user=user)
    return True


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
    The task is held alone, as a save holds it, so a save's regrade and
    this one each start from every attempt the other made. It is recorded
    as a regrade of many, a `rejudge` by the organiser.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    await timelines.hold_task(ctx, task)
    current = await published.task(ctx, task)
    if current is None:
        raise NotFound(
            "The task has no publication to grade against yet. Save it so that it publishes."
        )
    return await regrade(
        ctx,
        task,
        current.publication.id,
        cause=RegradeCause.REJUDGE,
        by=organiser.user.id,
        limit=current.definition.submissions.max,
    )


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
                .where(Grading.task_id == task, Grading.pipeline != SAVE)
                .order_by(Grading.queued_at.desc(), Grading.id.desc())
                .limit(max(1, min(limit, LIST_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    last = await latest_of(ctx, rows)
    goods = await last_goods(ctx, rows)
    gone = await lost(ctx, rows)
    await notice(ctx, [row for row in rows if row.id in last], gone)
    limits = await retry_limits(ctx, [row for row in rows if row.id in last], gone)
    return await entries(
        ctx,
        contest_id_of(task_scope(task)),
        [
            record(
                ctx,
                row,
                gone,
                latest=row.id in last,
                last_good=goods.get(row.id),
                retry_limit=limits.get(row.id),
            )
            for row in rows
        ],
    )


@action
async def feed(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    *,
    task: TaskId | None = None,
    user: str | None = None,
    entrant: uuid.UUID | None = None,
    status: GradingStatus | None = None,
    latest: bool = False,
    limit: int = 100,
) -> tuple[FeedEntry, ...]:
    """The gradings of the contest's tasks the organiser observes, newest
    first, at most `limit` of them and never more than 500: of one `task`,
    of the submissions the member whose username is `user` made, of the
    entrant `entrant`, and reading as `status`, each filter when given, and
    with `latest` only each submission's latest attempt, so `status`
    `system_error` with `latest` gives the ones `queue_depth` counts as
    `failed`. A task, person or entrant the organiser cannot see there
    gives none, and so does a `user` that breaks the forge's username rule
    (`is_username`). `Forbidden` for someone holding no role in the
    contest.
    """
    observed = observed_tasks(organiser, contest)
    where = [in_contest(contest, observed), Grading.pipeline != SAVE]
    if task is not None:
        if observed is not None and task not in observed:
            return ()
        where.append(Grading.task_id == task)
    whose = await narrowed(ctx, contest, user=user, entrant=entrant)
    if whose is None:
        return ()
    where.extend(whose)
    if status is not None:
        where.append(_may_read_as(status, ctx.now))
    if latest:
        where.append(_is_latest())
    size = max(1, min(limit, LIST_LIMIT))
    kept: builtins.list[GradingRecord] = []
    after: tuple[datetime, uuid.UUID] | None = None
    # Whether a `dispatched` row's run is lost is known only once the CI is
    # asked, so a filter by status reads a page at a time until the page is
    # full; the rest of what a status reads as is told in the query.
    while len(kept) < size:
        query = select(Grading).where(*where)
        if after is not None:
            query = query.where(tuple_(Grading.queued_at, Grading.id) < after)
        rows = (
            (
                await ctx.db.execute(
                    query.order_by(Grading.queued_at.desc(), Grading.id.desc()).limit(size)
                )
            )
            .scalars()
            .all()
        )
        last = await latest_of(ctx, rows)
        goods = await last_goods(ctx, rows)
        gone = await lost(ctx, rows)
        await notice(ctx, [row for row in rows if row.id in last], gone)
        shown = [row for row in rows if status is None or status_of(ctx, row, gone) == status]
        limits = await retry_limits(ctx, [row for row in shown if row.id in last], gone)
        kept.extend(
            record(
                ctx,
                row,
                gone,
                latest=row.id in last,
                last_good=goods.get(row.id),
                retry_limit=limits.get(row.id),
            )
            for row in shown
        )
        if len(rows) < size:
            break
        after = (rows[-1].queued_at, rows[-1].id)
    return await entries(ctx, contest, kept[:size])


@action
async def queue_depth(ctx: Context, organiser: Organiser, contest: ContestId) -> QueueDepth:
    """How many gradings of the contest's tasks the organiser observes wait
    for a machine, by status, and how many failed and wait for staff, read
    in one go. `Forbidden` for someone holding no role in the contest.
    """
    observed = observed_tasks(organiser, contest)
    failing = and_(_is_latest(), _may_read_as(GradingStatus.SYSTEM_ERROR, ctx.now))
    rows = (
        (
            await ctx.db.execute(
                select(Grading).where(
                    in_contest(contest, observed),
                    Grading.pipeline != SAVE,
                    or_(Grading.status.in_(WAITING), failing),
                )
            )
        )
        .scalars()
        .all()
    )
    last = await latest_of(ctx, rows)
    gone = await lost(ctx, rows)
    await notice(ctx, [row for row in rows if row.id in last], gone)
    read = [(row, status_of(ctx, row, gone)) for row in rows]
    counted = Counter(status for _, status in read)
    failed = sum(
        1 for row, status in read if status is GradingStatus.SYSTEM_ERROR and row.id in last
    )
    return QueueDepth(
        queued=counted[GradingStatus.QUEUED],
        dispatched=counted[GradingStatus.DISPATCHED],
        failed=failed,
    )


def observed_tasks(organiser: Organiser, contest: ContestId) -> frozenset[TaskId] | None:
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
        raise Forbidden(
            f"This needs the observer role at {named.name} or at one of its tasks. Ask an "
            f"admin of {named.name} if you need it."
        )
    return tasks


async def narrowed(
    ctx: Context, contest: ContestId, *, user: str | None, entrant: uuid.UUID | None
) -> builtins.list[ColumnElement[bool]] | None:
    """What narrows a contest's gradings to the submissions the member whose
    username is `user` made and to the entrant `entrant`, each when given;
    none when no grading can match, a `user` that breaks the forge's
    username rule (`is_username`) or that no one holds among them.
    """
    where: builtins.list[ColumnElement[bool]] = []
    if user is not None:
        # A name no one can hold at the forge matches nobody, and is never
        # sent there.
        if not is_username(user):
            return None
        try:
            found = await ctx.forge.identity.find_user_by_username(user)
        except NotFound:
            return None
        where.append(Grading.submitted_by == found.id)
    if entrant is not None:
        where.append(Grading.workspace_id == _workspace(ctx, contest, EntrantOwner(entrant)))
    return where


def _is_latest() -> ColumnElement[bool]:
    """The rows that are their line's latest attempt, told in the query: no
    attempt of the same submission and pipeline after them.
    """
    later = aliased(Grading)
    return not_(
        exists().where(
            later.submission_id == Grading.submission_id,
            later.pipeline == Grading.pipeline,
            later.attempt > Grading.attempt,
        )
    )


def in_contest(contest: ContestId, observed: frozenset[TaskId] | None) -> ColumnElement[bool]:
    if observed is None:
        return Grading.task_id.startswith(f"{contest}/", autoescape=True)
    return Grading.task_id.in_(sorted(observed))


def _workspace(ctx: Context, contest: ContestId, owner: EntrantOwner) -> WorkspaceId:
    return ctx.forge.workspaces.workspace_of(contest, owner)


async def entries(
    ctx: Context, contest: ContestId, records: Sequence[GradingRecord]
) -> tuple[FeedEntry, ...]:
    """Each grading with whose it is and its task's name and label: the
    names and the entrants read first, and the usernames and the contest's
    settings from the forge once the connection is let go of.
    """
    tasks = await names.names_of(ctx, {found.task for found in records})
    by = await _submitters(ctx, records)
    settings = await _settings(ctx, contest) if records else None
    return tuple(
        FeedEntry(
            found,
            by[found.id],
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


async def _submitters(ctx: Context, records: Sequence[GradingRecord]) -> dict[uuid.UUID, Submitter]:
    """Whose each grading is, with the names an organiser knows them by: the
    entrants read first, and the usernames from the forge once the
    connection is let go of, a forge that does not say leaving the name out.
    """
    owners = {
        found.id: ctx.forge.workspaces.owner_of(found.workspace).entrant_id for found in records
    }
    ids = sorted(set(owners.values()))
    rows = (await ctx.db.scalars(select(Entrant).where(Entrant.id.in_(ids)))).all() if ids else []
    shown = await entrants.display_names(ctx, rows)
    users: dict[int, str | None] = {}
    for found in records:
        if found.submitted_by is not None and found.submitted_by not in users:
            users[found.submitted_by] = await entrants.username_of(ctx, found.submitted_by) or None
    return {
        found.id: Submitter(
            entrant=owners[found.id],
            entrant_name=shown.get(owners[found.id]),
            user_id=found.submitted_by,
            username=users.get(found.submitted_by) if found.submitted_by is not None else None,
        )
        for found in records
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
            f"The grading's log is larger than the {RUN_LOG_MAX} bytes Unicon shows.",
            limit=RUN_LOG_MAX,
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
    ctx: Context,
    row: Grading,
    lost: Collection[uuid.UUID] = (),
    *,
    latest: bool,
    last_good: int | None = None,
    retry_limit: int | None = None,
) -> GradingRecord:
    """The grading as an organiser reads it, `latest` saying whether it is
    its submission's latest attempt and `last_good` the latest earlier one
    that finished with a result, kept only on a latest attempt that has not
    finished with one itself, since that is when it counts; and
    `retry_limit` the `submissions.max` a retry of it now would end over.
    """
    late = overdue_of(ctx, row, lost)
    status = GradingStatus.SYSTEM_ERROR if late is not None else GradingStatus(row.status)
    return GradingRecord(
        id=row.id,
        task=TaskId(row.task_id),
        workspace=WorkspaceId(row.workspace_id),
        submission_number=row.submission_number,
        submitted_at=row.submitted_at,
        publication=PublicationId(row.publication_id),
        attempt=row.attempt,
        latest=latest,
        submitted_by=row.submitted_by,
        status=status,
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
        automatic=row.automatic,
        last_good=last_good if latest and not good(row) else None,
        limit=row.submission_limit if status is GradingStatus.OVER_LIMIT else None,
        retry_limit=retry_limit if latest else None,
        regrade=row.regrade_id,
        pipeline=cast(Listed, row.pipeline),
        early=row.early_id,
    )


async def latest_of(ctx: Context, rows: Iterable[Grading]) -> frozenset[uuid.UUID]:
    """The gradings among `rows`, none a save row, that are the latest
    attempt on their line, worked out over every attempt of it, read or not.
    """
    listed = builtins.list(rows)
    submissions = sorted({row.submission_id for row in listed})
    if not submissions:
        return frozenset()
    found = await ctx.db.execute(
        select(Grading.submission_id, Grading.pipeline, func.max(Grading.attempt))
        .where(Grading.submission_id.in_(submissions))
        .group_by(Grading.submission_id, Grading.pipeline)
    )
    last = {(submission, pipeline): attempt for submission, pipeline, attempt in found.all()}
    return frozenset(
        row.id for row in listed if row.attempt == last.get((row.submission_id, row.pipeline))
    )


async def last_goods(ctx: Context, rows: Iterable[Grading]) -> dict[uuid.UUID, int]:
    """For each grading among `rows`, none a save row, the latest earlier
    attempt on its line that finished with a result, worked out over every
    attempt of it, read or not; a grading with none is left out.
    """
    listed = builtins.list(rows)
    submissions = sorted({row.submission_id for row in listed})
    if not submissions:
        return {}
    found = await ctx.db.execute(
        select(Grading.submission_id, Grading.pipeline, Grading.attempt).where(
            Grading.submission_id.in_(submissions), GOOD
        )
    )
    done: dict[tuple[str, str], builtins.list[int]] = {}
    for submission, pipeline, attempt in found.all():
        done.setdefault((submission, pipeline), []).append(attempt)
    goods: dict[uuid.UUID, int] = {}
    for row in listed:
        line = (row.submission_id, row.pipeline)
        earlier = [attempt for attempt in done.get(line, ()) if attempt < row.attempt]
        if earlier:
            goods[row.id] = max(earlier)
    return goods


async def _managed(
    ctx: Context, organiser: Organiser, grading: uuid.UUID, *, lock: bool = True
) -> Grading:
    """The grading, held until the unit of work ends when `lock`, for an
    organiser who manages its task. `NotFound` for one whose task they do
    not observe, and for a save's row, which no organiser acts on yet.
    """
    row = await find(ctx, grading, lock=lock)
    if row is None or row.pipeline == SAVE:
        raise NotFound(NO_SUCH_GRADING)
    scope = task_scope(TaskId(row.task_id))
    if not holds(organiser.grants, scope, Role.OBSERVER):
        raise NotFound(NO_SUCH_GRADING)
    require(organiser, scope, Role.MANAGER)
    return row


async def _line(ctx: Context, row: Grading) -> builtins.list[Grading]:
    """Every attempt on `row`'s line, held until the unit of work ends and
    read afresh, in the order a rejudge takes them, so a retry and a rejudge
    never wait on each other in a circle.
    """
    return builtins.list(
        (
            await ctx.db.execute(
                select(Grading)
                .where(_on_line(row))
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _next_attempt(
    ctx: Context,
    row: Grading,
    publication: PublicationId,
    attempts: Sequence[Grading],
    *,
    automatic: bool = False,
    regrade: uuid.UUID | None = None,
    over_limit: int | None = None,
) -> Grading:
    """The next attempt on `row`'s line against `publication`, in the
    regrade `regrade` when given and otherwise in the one `row` belongs to,
    ended at once over the limit with `over_limit`.
    """
    return new_row(
        ctx,
        task=TaskId(row.task_id),
        workspace=row.workspace_id,
        submission=row.submission_id,
        number=row.submission_number,
        version=row.submission_version,
        submitted_at=row.submitted_at,
        publication=publication,
        attempt=max(other.attempt for other in attempts) + 1,
        key=None,
        submitted_by=row.submitted_by,
        automatic=automatic,
        regrade=regrade or row.regrade_id,
        over_limit=over_limit,
        pipeline=row.pipeline,
        early=row.early_id,
    )


async def follow(ctx: Context, early: Grading) -> Grading | None:
    """The late row of a run task's early row that has just ended `done`,
    none when a later early attempt of its submission is already made: the
    next attempt on the submission's late line, against the early row's
    publication and in its regrade, each late row still unfinished stopped
    first, and started in the background, so the report that calls this
    waits on no start. The late line is held after the early row the report
    holds, the order a regrade holds them in, so the two never wait on each
    other in a circle.
    """
    if await _superseded(ctx, early):
        return None
    lates = builtins.list(
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.submission_id == early.submission_id, Grading.pipeline == LATE)
                .order_by(Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    for row in lates:
        if GradingStatus(row.status) in UNFINISHED:
            stop(ctx, row)
    return new_row(
        ctx,
        task=TaskId(early.task_id),
        workspace=early.workspace_id,
        submission=early.submission_id,
        number=early.submission_number,
        version=early.submission_version,
        submitted_at=early.submitted_at,
        publication=PublicationId(early.publication_id),
        attempt=max((row.attempt for row in lates), default=0) + 1,
        key=None,
        submitted_by=early.submitted_by,
        regrade=early.regrade_id,
        pipeline=LATE,
        early=early.id,
        unwaited=True,
    )


def stop(ctx: Context, row: Grading) -> None:
    """Cancel an unfinished grading a rejudge, a later late row or a later
    save replaces, and its run at the CI once that has committed; a run the
    CI does not stop is refused its reports, since the grading is cancelled.
    """
    if GradingStatus(row.status) in AT_THE_CI and row.run_id is not None:
        _cancel_after_commit(ctx, RunId(row.run_id))
    finish(ctx, row, GradingStatus.CANCELLED)


def _regraded(rows: Sequence[Grading]) -> dict[str, Grading]:
    """The latest attempt of each submission `regrade` gives a new one, from
    every attempt to the task in submission and attempt order: all of them
    but one staff cancelled.
    """
    latest: dict[str, Grading] = {}
    for row in rows:
        latest[row.submission_id] = row
    return {submission: row for submission, row in latest.items() if not staff_cancelled(row)}


def _without_room(
    ctx: Context,
    regraded: Collection[str],
    attempts: Mapping[str, Sequence[Grading]],
    lost: Collection[uuid.UUID],
    limit: int,
) -> frozenset[str]:
    """Which of the `regraded` submissions, each with its `attempts`, wait
    for room under a `submissions.max` of `limit` (`waits_for_room`) and
    find none: each entrant's delayed and over-the-limit submissions are
    taken in the order they were made, after the places its others hold
    (`room`).
    """
    entrants: dict[str, dict[int, str]] = {}
    for submission, each in attempts.items():
        entrants.setdefault(each[0].workspace_id, {})[each[0].submission_number] = submission
    refused: set[str] = set()
    for numbered in entrants.values():
        asking = [
            number
            for number, submission in numbered.items()
            if submission in regraded and waits_for_room(ctx, attempts[submission], lost)
        ]
        if not asking:
            continue
        places = {
            number: place_of(ctx, attempts[submission], lost)
            for number, submission in numbered.items()
        }
        admitted = room(places, limit, asking)
        refused.update(numbered[number] for number in asking if number not in admitted)
    return frozenset(refused)


async def to_regrade(ctx: Context, task: TaskId, *, limit: int | None = None) -> int:
    """How many submissions `regrade` would grade again against a publication
    not made yet, chosen as it chooses them: each submission's latest
    attempt, but one staff cancelled, and but a delayed or over-the-limit
    one whose entrant has no room left under a `submissions.max` of `limit`,
    the task's latest publication's when none is given. What a save's
    confirmation says it will cost.
    """
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task, FRONT)
                .order_by(Grading.submission_id, Grading.attempt)
            )
        )
        .scalars()
        .all()
    )
    regraded = _regraded(rows)
    if limit is None:
        limit = await task_limit(ctx, task)
    if limit is None:
        return len(regraded)
    attempts: dict[str, builtins.list[Grading]] = {}
    for row in rows:
        attempts.setdefault(row.submission_id, []).append(row)
    gone = await lost(ctx, rows)
    return len(regraded) - len(_without_room(ctx, regraded, attempts, gone, limit))


async def regrade(
    ctx: Context,
    task: TaskId,
    publication: PublicationId,
    *,
    cause: RegradeCause,
    by: int | None,
    limit: int,
) -> Rejudged:
    """A new attempt of every submission's latest attempt to the task,
    against `publication`, for a rejudge and for a save that publishes a
    change to how the task grades, `cause` saying which and `by` naming the
    organiser who asked. A latest attempt still being graded
    against another publication is cancelled first, and one being graded
    against `publication` is left to finish; one that reads as finished only
    because it is overdue or lost is ended first, its old run cancelled at
    the CI once this has committed. A submission staff cancelled, its latest
    attempt `cancelled` with a sentence, is left as it is.

    A submission with no result, delayed or over the limit, is graded again
    only while its entrant has room under a `submissions.max` of `limit`,
    each entrant's taken in the order they were made: a delayed one with
    no room gets an attempt ended `over_limit` at once, and one already
    over the limit with none is left as it is. Every attempt made belongs to
    one regrade of many recorded for it, none when it makes none.

    A run task is graded again on its early rows alone: each new early row
    makes its late row when it ends `done`, and the store runs again only
    what changed (11.6, 11.8). A late row still unfinished is stopped when
    its submission is graded again or it grades against another
    publication, so no late row of an old grading runs on.
    """
    # Which runs the CI has lost is asked before any row is locked, since
    # asking takes the CI's time; the task itself is held by the caller.
    gone = await lost(
        ctx,
        (await ctx.db.execute(select(Grading).where(Grading.task_id == task, FRONT)))
        .scalars()
        .all(),
    )
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task, FRONT)
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # A run task's late rows are held after its early rows, as `follow` holds
    # them after the early row it follows.
    lates = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(
                    Grading.task_id == task,
                    Grading.pipeline == LATE,
                    Grading.status.in_([status.value for status in UNFINISHED]),
                )
                .order_by(Grading.submission_id, Grading.attempt)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    attempts: dict[str, builtins.list[Grading]] = {}
    for row in rows:
        attempts.setdefault(row.submission_id, []).append(row)
    regraded = _regraded(rows)
    refused = _without_room(ctx, regraded, attempts, gone, limit)
    made: builtins.list[tuple[str, Grading]] = []
    left_running = 0
    for submission, row in regraded.items():
        status = status_of(ctx, row, gone)
        if status in UNFINISHED and row.publication_id == publication:
            left_running += 1
        elif not (submission in refused and status is GradingStatus.OVER_LIMIT):
            made.append((submission, row))
    again = {submission for submission, _ in made}
    for late in lates:
        if late.submission_id in again or late.publication_id != publication:
            stop(ctx, late)
    if not made:
        log.info("gradings.rejudged", task=task, publication=publication, queued=0)
        return Rejudged(task, publication, 0, 0, left_running)
    recorded = Regrade(
        id=new_id(),
        task_id=task,
        publication_id=publication,
        cause=cause,
        started_by=by,
        started_at=ctx.now,
    )
    ctx.db.add(recorded)
    queued = cancelled = over_limit = 0
    for submission, row in made:
        if status_of(ctx, row, gone) in UNFINISHED:
            stop(ctx, row)
            cancelled += 1
        elif GradingStatus(row.status) in UNFINISHED:
            finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=overdue_of(ctx, row, gone))
            if row.run_id is not None:
                _cancel_after_commit(ctx, RunId(row.run_id))
        over = limit if submission in refused else None
        _next_attempt(
            ctx, row, publication, attempts[submission], regrade=recorded.id, over_limit=over
        )
        if over is None:
            queued += 1
        else:
            over_limit += 1
    await ctx.db.flush()
    log.info(
        "gradings.rejudged",
        task=task,
        publication=publication,
        regrade=str(recorded.id),
        cause=cause.value,
        queued=queued,
        cancelled=cancelled,
        left_running=left_running,
        over_limit=over_limit,
    )
    return Rejudged(
        task, publication, queued, cancelled, left_running, over_limit, regrade=recorded.id
    )
