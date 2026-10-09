"""What a grading run asks of the platform, with no session: the CI asking
what the run is, the harness fetching its envelope, and the harness
reporting. Each call is handed over as it arrived and authenticated here,
since a grading's id alone proves nothing.

`config` answers a CI that asks what a run is as it starts one. What runs
is decided by the platform, never by the request or the repository, and
that takes two checks, kept apart. The implementation proves the request
the CI's own, reads which grading and task it is about, and asks `lookup`
here, which answers with the run and what it runs only for a grading of
that task whose run is being started, `queued`: the CI asks while the
platform's start is under way, before its answer comes back. The
implementation then refuses a run started with any variable other than
those it starts that run with, and answers with the run's steps, from the
plan of the publication it grades against, which names the harness image
by digest. Nothing is written, so the answer never waits on the start that
is holding the row. Anything else is refused with an error, never an empty
answer, since a CI could take an empty answer as leave to run what it found
in the repository, and the reason goes to the log. A CI that is handed every
run whole never asks, and `config` is then `NotFound`.

`envelope` is the one document the harness downloads, the runner's
`envelope.schema.json` version 5, served once: only with the envelope key of
the grading's run, and only while the grading is `dispatched`, the CI
holding a run that has not begun. That fetch is the run beginning: the
grading becomes `running` and its deadline is written, the envelope's wall
clock and the time kept for reporting from now. Any later fetch is refused,
since the envelope's URL is a variable of the run that anyone who reads the
task's runs at the CI can see, and the envelope hands out the callback
token. A harness whose fetch lost its answer ends its run without a
report, and the grading reads as a system error once its deadline passes,
for an organiser to retry. The envelope carries the callback token, the
callback URL, a URL the harness writes its log with, signed for the
machine URL until the deadline, and the value of every secret the plan
names. No org holds a secret yet, so a plan names none and the envelope's
`secrets` is empty.

`callback` takes a report under the callback token of the grading's run,
compared by its SHA-256 with the row's in constant time, so another
grading's token is refused like a wrong one, and the token is what says
which grading a report is for. A report comes only from a `running` grading
before its deadline. `started` confirms the run began, `progress` is kept on
the row, and `finished` carries the result: one that matches the runner's
`result.schema.json` and is within `RESULT_MAX` is kept with its log key and
the grading is `done`, or `system_error` when the result says the run
stopped on one, with its `error`; any other leaves the grading in
`system_error` with the reason, and is taken, since sending it again would
not mend it. A kept result sent again, because its answer was lost, is
answered the same.

Neither the envelope's key nor a report's token is checked while the row is
held: the grading is read, the secret checked, and only then is the row
locked and the secret checked again, so a caller that proves nothing holds
nothing up.
"""

import hmac
import uuid
from collections.abc import Callable
from datetime import UTC, timedelta
from typing import Any

from pydantic import ValidationError

from forge.db.tables import Grading
from forge.domain.errors import (
    CiRequestRefused,
    Forbidden,
    GradingClosed,
    InvalidToken,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
)
from forge.domain.grading import (
    GradingRun,
    GradingStatus,
    InboundAnswer,
    InboundRequest,
    RunSpec,
    log_key,
    run_deadline,
    token_hash,
    wall_seconds,
)
from forge.domain.ids import TaskId
from forge.domain.plans import Plan
from forge.domain.reports import ERROR_LIMIT, Event, read_report, result_problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import gradings

log = get_logger(__name__)

SCHEMA_VERSION = 5
REFUSED = "The platform does not answer this request."
NOT_TAKING = "The grading takes no reports or envelope now."
WRONG_TOKEN = "The report's token is not this grading's."
SHORTEST_URL = timedelta(seconds=1)


class _ForgeDown(Exception):
    """The forge did not answer `lookup`, carried past the implementation so
    it is not taken for the CI's own trouble.
    """

    def __init__(self, error: Unavailable) -> None:
        self.error = error


@action
async def config(ctx: Context, request: InboundRequest) -> InboundAnswer:
    """The answer to a CI asking what the run it is starting is.
    `CiRequestRefused` for a request that does not verify, names no grading,
    names one of another task or one not being started, or one started with
    other variables; `NotFound` from a CI that never asks.
    """
    looked_up: list[GradingRun] = []

    async def lookup(grading: str | None, task: TaskId) -> tuple[GradingRun, RunSpec]:
        run, spec = await _starting(ctx, grading, task)
        looked_up.append(run)
        return run, spec

    try:
        answer = await ctx.forge.grading.answer(request, lookup, now=ctx.now)
    except _ForgeDown as down:
        raise down.error from None
    except NotFound:
        log.info("runs.config_unasked")
        raise NotFound("This CI does not ask what a run is.") from None
    except (Forbidden, Rejected) as exc:
        reason = "unverified" if isinstance(exc, Forbidden) else "rejected"
        grading = str(looked_up[0].grading) if looked_up else None
        log.warning("runs.config_refused", reason=reason, grading=grading, detail=exc.detail)
        raise CiRequestRefused(REFUSED) from None
    except PortError as exc:
        log.warning("runs.config_key_unread", error=type(exc).__name__, detail=exc.detail)
        raise Unavailable("The CI's signing key could not be read.") from None
    [run] = looked_up
    log.info("runs.configured", grading=str(run.grading), task=run.task)
    return answer


async def _starting(ctx: Context, grading: str | None, task: TaskId) -> tuple[GradingRun, RunSpec]:
    """The platform's half of what decides a run: the run and what it runs,
    for a grading of `task` whose run is being started. `CiRequestRefused`
    for any other.
    """
    row = await _named(ctx, grading)
    if row is None or row.task_id != task:
        log.warning("runs.config_refused", reason="no_grading", task=task)
        raise CiRequestRefused(REFUSED)
    if row.status != GradingStatus.QUEUED:
        log.warning("runs.config_refused", reason="not_starting", grading=str(row.id))
        raise CiRequestRefused(REFUSED)
    try:
        run = await _run(ctx, row, CiRequestRefused(REFUSED))
        plan = await _plan(ctx, row, run, CiRequestRefused(REFUSED))
    except Unavailable as exc:
        raise _ForgeDown(exc) from None
    return run, gradings.spec_of(ctx, plan)


@action
async def envelope(ctx: Context, grading: uuid.UUID, key: str) -> dict[str, Any]:
    """The grading's envelope, for the key its URL carries, once. `NotFound`
    for a wrong key, and `GradingClosed` for a grading that is not
    `dispatched`: one whose run the CI does not hold, or whose run fetched
    its envelope already.
    """
    given = key.encode() if isinstance(key, str) else b""
    row = await _proven(
        ctx,
        grading,
        lambda row: hmac.compare_digest(gradings.envelope_key_of(ctx, row).encode(), given),
    )
    if row is None:
        log.info("runs.envelope_refused", grading=str(grading), reason="key")
        raise NotFound(gradings.NO_SUCH_GRADING)
    _refuse_closed(ctx, row)
    run = await _run(ctx, row, GradingClosed(NOT_TAKING))
    wall = wall_seconds(await _plan(ctx, row, run, GradingClosed(NOT_TAKING)))
    row.status = GradingStatus.RUNNING
    row.started_at = ctx.now
    row.deadline_at = run_deadline(ctx.now, wall)
    gradings.changed(ctx, row)
    await ctx.db.flush()
    log.info("runs.started", grading=str(row.id), run=row.run_id)
    places = ctx.forge.grading.run_places(run)
    log_put = ctx.forge.objects.put_url(
        log_key(row.id, row.attempt),
        expires_in=max(row.deadline_at - ctx.now, SHORTEST_URL),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "grading_id": str(row.id),
        "submission": dict(places.submission),
        "attempt": row.attempt,
        "checkouts": dict(places.checkouts),
        "callback": {
            "url": gradings.callback_url(ctx, row.id),
            "token": gradings.callback_token_of(ctx, row),
        },
        "log_put": log_put,
        "deadline": row.deadline_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limits": {"wall_seconds": wall},
        "secrets": {},
    }


@action
async def callback(
    ctx: Context, grading: uuid.UUID, authorization: str | None, body: bytes
) -> GradingStatus:
    """Take one report of the grading's run, under the bearer token in
    `authorization`, and answer where the grading stands after it.
    `InvalidToken` for a missing or wrong token, `InvalidCallback` for a body
    that is no report, and `GradingClosed` for a grading that takes no
    reports now.
    """
    token = _bearer(authorization)
    row = None
    if token is not None:
        given = token_hash(token)
        row = await _proven(ctx, grading, lambda row: _carries(row, given))
    if row is None:
        log.info("runs.callback_refused", grading=str(grading), reason="token")
        raise InvalidToken(WRONG_TOKEN)
    report = read_report(body)
    status = GradingStatus(row.status)
    if (
        report.event is Event.FINISHED
        and status in (GradingStatus.DONE, GradingStatus.SYSTEM_ERROR)
        and row.result is not None
        and row.result == report.result
    ):
        return status
    if status is not GradingStatus.RUNNING or (
        row.deadline_at is not None and ctx.now >= row.deadline_at
    ):
        log.info("runs.callback_refused", grading=str(row.id), reason="closed", status=status)
        raise GradingClosed(NOT_TAKING)
    match report.event:
        case Event.STARTED:
            row.started_at = row.started_at or ctx.now
        case Event.PROGRESS:
            row.progress = report.progress
            gradings.changed(ctx, row)
        case Event.FINISHED:
            _finished(ctx, row, report.result)
    await ctx.db.flush()
    return GradingStatus(row.status)


def _finished(ctx: Context, row: Grading, result: Any) -> None:
    problem = result_problem(result)
    if problem is not None:
        log.warning("runs.result_refused", grading=str(row.id), problem=problem)
        gradings.finish(ctx, row, GradingStatus.SYSTEM_ERROR, error=problem)
        return
    row.result = result
    row.log_key = log_key(row.id, row.attempt) if result["run_log"] is not None else None
    if result["stopped"] == GradingStatus.SYSTEM_ERROR.value:
        gradings.finish(
            ctx, row, GradingStatus.SYSTEM_ERROR, error=str(result["error"])[:ERROR_LIMIT]
        )
    else:
        gradings.finish(ctx, row, GradingStatus.DONE)
    log.info("runs.finished", grading=str(row.id), stopped=result["stopped"])


def _carries(row: Grading, given: bytes) -> bool:
    """Whether `given` is the SHA-256 of the callback token the row keeps."""
    kept = row.callback_token_hash
    return kept is not None and hmac.compare_digest(given, kept)


def _refuse_closed(ctx: Context, row: Grading) -> None:
    """Refuse the envelope of a grading that is not `dispatched`: the CI holds
    no run of it, its run fetched the envelope already, or no machine took
    it in time (`grading.overdue`).
    """
    status = gradings.status_of(ctx, row)
    if status is not GradingStatus.DISPATCHED:
        log.info("runs.envelope_refused", grading=str(row.id), reason="closed", status=status)
        raise GradingClosed(NOT_TAKING)


async def _proven(
    ctx: Context, grading: uuid.UUID, proves: Callable[[Grading], bool]
) -> Grading | None:
    """The grading, held until the unit of work ends, once the secret its
    caller gave `proves` it theirs: checked first on the row as read, so a
    caller who proves nothing locks nothing, and again once it is held, in
    case the grading moved on to another run meanwhile. None otherwise.
    """
    row = await gradings.find(ctx, grading)
    if row is None or not proves(row):
        return None
    held = await gradings.find(ctx, grading, lock=True)
    return held if held is not None and proves(held) else None


async def _named(ctx: Context, grading: str | None) -> Grading | None:
    if grading is None:
        return None
    try:
        return await gradings.find(ctx, uuid.UUID(grading))
    except ValueError:
        return None


async def _run(ctx: Context, row: Grading, refusal: Exception) -> GradingRun:
    try:
        return await gradings.run_of(ctx, row)
    except NotFound:
        log.warning("runs.publication_gone", grading=str(row.id), task=row.task_id)
        raise refusal from None
    except PortError as exc:
        log.warning("runs.forge_unavailable", grading=str(row.id), error=type(exc).__name__)
        raise Unavailable("The forge did not answer.") from None


async def _plan(ctx: Context, row: Grading, run: GradingRun, refusal: Exception) -> Plan:
    """The plan of the publication the grading grades against."""
    try:
        return await gradings.plan_of(ctx, run)
    except (NotFound, Forbidden, ValidationError) as exc:
        log.warning("runs.plan_unreadable", grading=str(row.id), error=type(exc).__name__)
        raise refusal from None
    except PortError as exc:
        log.warning("runs.forge_unavailable", grading=str(row.id), error=type(exc).__name__)
        raise Unavailable("The forge did not answer.") from None


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
