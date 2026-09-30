"""What a grading run asks of the platform, with no session: the CI asking
what the run is, and the harness fetching its envelope. Each call is handed
over as it arrived and authenticated here, since a grading's id alone
proves nothing.

`config` is the CI's configuration extension. Its request is signed by the
CI (the port checks the signature), names the grading in the run's
variables, and every other variable must be the one the platform starts
that grading's run with. The grading must be one whose run is being
started, `queued` or `dispatching`: the CI asks while the platform's start
is under way, before its answer comes back. The answer is the three steps
every run has, from the plan of the grading's stage in the publication it
grades against, which names the harness image by digest; it writes
nothing, so it never waits on the start that is holding the row. Anything
else is refused with an error, never an empty answer, since the CI would
take an empty answer as leave to run what it found in the repository, and
the reason goes to the log.

`envelope` is the one document the harness downloads, the runner's
`envelope.schema.json` version 3, served once: only with the envelope key of
the grading's current run, and only while the grading is `dispatched`, the CI
holding a run that has not begun. That fetch is the run beginning: the
grading becomes `running` and its deadline is written, the envelope's wall
clock and the time kept for reporting from now. Any later fetch is refused,
since the envelope's URL is a variable of the run that anyone who reads the
task's runs at the CI can see, and the envelope hands out the callback
token. A harness whose fetch lost its answer ends its run without a
report, and the overdue pass finds the run dead once its deadline passes.
The envelope carries the callback token, the callback URL, and a URL the
harness writes its log with, signed for the machine URL until the deadline.

The envelope's key is not checked while the row is held: the grading is
read, the key checked, and only then is the row locked and the key checked
again, so a caller that proves nothing holds nothing up.
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
    NotFound,
    PortError,
    Rejected,
    Unavailable,
)
from forge.domain.grading import (
    WAITING,
    CiAnswer,
    CiRequest,
    GradingRun,
    GradingStatus,
    log_key,
    run_deadline,
    wall_seconds,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.plans import Plan, plan_path
from forge.log import get_logger
from forge.port.objects import Store
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import gradings

log = get_logger(__name__)

SCHEMA_VERSION = 3
REFUSED = "The platform does not answer this request."
NOT_TAKING = "The grading takes no reports or envelope now."
SHORTEST_URL = timedelta(seconds=1)


@action
async def config(ctx: Context, request: CiRequest) -> CiAnswer:
    """The CI's configuration extension: the steps of the run the request
    names. `CiRequestRefused` for a request that does not verify, names no
    grading, or names one that is not being started with these variables.
    """
    try:
        ask = await ctx.forge.grading.read_config_request(request, now=ctx.now)
    except (Forbidden, Rejected) as exc:
        log.warning("runs.config_refused", reason="unverified", detail=exc.detail)
        raise CiRequestRefused(REFUSED) from None
    except PortError as exc:
        log.warning("runs.config_key_unread", error=type(exc).__name__, detail=exc.detail)
        raise Unavailable("The CI's signing key could not be read.") from None
    row = await _named(ctx, ask.grading)
    if row is None or row.task_id != ask.task:
        log.warning("runs.config_refused", reason="no_grading", task=ask.task)
        raise CiRequestRefused(REFUSED)
    if GradingStatus(row.status) not in WAITING:
        log.warning("runs.config_refused", reason="not_starting", grading=str(row.id))
        raise CiRequestRefused(REFUSED)
    run = await _run(ctx, row, CiRequestRefused(REFUSED))
    if dict(ask.variables) != dict(ctx.forge.grading.run_variables(run)):
        log.warning("runs.config_refused", reason="variables", grading=str(row.id))
        raise CiRequestRefused(REFUSED)
    plan = await _plan(ctx, row, run, CiRequestRefused(REFUSED))
    try:
        answer = ctx.forge.grading.config_answer(
            run, ask, harness_image=plan.harness_image, clone_image=ctx.settings.clone_image
        )
    except Rejected as exc:
        log.warning("runs.config_refused", reason="answer", grading=str(row.id), detail=exc.detail)
        raise CiRequestRefused(REFUSED) from None
    log.info("runs.configured", grading=str(row.id), task=row.task_id)
    return answer


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
    _refuse_closed(row)
    run = await _run(ctx, row, GradingClosed(NOT_TAKING))
    wall = wall_seconds(await _plan(ctx, row, run, GradingClosed(NOT_TAKING)))
    row.status = GradingStatus.RUNNING
    row.started_at = ctx.now
    row.deadline_at = run_deadline(ctx.now, wall)
    row.wait_reason = None
    await ctx.db.flush()
    log.info("runs.started", grading=str(row.id), run=row.run_id)
    places = ctx.forge.grading.run_places(run)
    log_put = ctx.forge.objects.put_url(
        Store.RESULTS,
        log_key(row.id, row.attempt),
        expires_in=max(row.deadline_at - ctx.now, SHORTEST_URL),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "grading_id": str(row.id),
        "submission": dict(places.submission),
        "stage": row.stage,
        "attempt": row.attempt,
        "task": dict(places.task),
        "publication": dict(places.publication),
        "checkouts": dict(places.checkouts),
        "callback": {
            "url": gradings.callback_url(ctx, row.id),
            "token": gradings.callback_token_of(ctx, row),
        },
        "log_put": log_put,
        "deadline": row.deadline_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limits": {"wall_seconds": wall},
    }


def _refuse_closed(row: Grading) -> None:
    """Refuse the envelope of a grading that is not `dispatched`: the CI holds
    no run of it, or its run fetched the envelope already. A run that waited
    for a machine past the deadline its start was given has not begun, and
    is served.
    """
    status = GradingStatus(row.status)
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
    """The plan of the grading's stage in the publication it grades against."""
    try:
        found = await ctx.forge.content.read_file(
            PLATFORM, TaskId(row.task_id), plan_path(row.stage), at=run.publication_version
        )
        return Plan.from_bytes(found.content)
    except (NotFound, Forbidden, ValidationError) as exc:
        log.warning("runs.plan_unreadable", grading=str(row.id), error=type(exc).__name__)
        raise refusal from None
    except PortError as exc:
        log.warning("runs.forge_unavailable", grading=str(row.id), error=type(exc).__name__)
        raise Unavailable("The forge did not answer.") from None
