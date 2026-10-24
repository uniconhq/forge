"""What a grading's pipeline asks of the platform with no session before
its steps do: the CI asking what the pipeline is. The call is handed over as
it arrived and authenticated here, since a grading's id alone proves
nothing.

`config` answers a CI that asks what a pipeline is as it starts one
(`pipelines.lookup`, phase 4.9). What runs is decided by the platform, never
by the request or the repository, and that takes two checks, kept apart.
The implementation proves the request the CI's own, reads which grading and
org it is about, and asks `pipelines.lookup`, which answers with the
pipeline and its steps only for a grading of that org whose pipeline is
being started. The implementation then refuses a pipeline started with any
variable other than those it starts that pipeline with, and answers with
the pipeline's steps. Nothing is written, so the answer never waits on the
start that is holding the row. Anything else is refused with an error, never
an empty answer, since a CI could take an empty answer as leave to run what
it found in the repository, and the reason goes to the log. A CI that is
handed every pipeline whole never asks, and `config` is then `NotFound`.
"""

import uuid

from forge.domain.errors import (
    CiRequestRefused,
    Forbidden,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
    VariablesDiffer,
)
from forge.domain.grading import InboundAnswer, InboundRequest, PipelineRun, PipelineSpec
from forge.domain.ids import OrgId
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import pipelines

log = get_logger(__name__)

REFUSED = "The platform does not answer this request."


class _ForgeDown(Exception):
    """The forge did not answer `lookup`, carried past the implementation so
    it is not taken for the CI's own trouble.
    """

    def __init__(self, error: Unavailable) -> None:
        self.error = error


@action
async def config(ctx: Context, request: InboundRequest) -> InboundAnswer:
    """The answer to a CI asking what the pipeline it is starting is.
    `CiRequestRefused` for a request that does not verify, names no grading,
    names one of another org or one not being started, or one started with
    other variables; `NotFound` from a CI that never asks.
    """
    looked_up: list[uuid.UUID] = []

    async def lookup(grading: str | None, org: OrgId) -> tuple[PipelineRun, PipelineSpec]:
        try:
            run, spec = await pipelines.lookup(ctx, grading, org)
        except Unavailable as exc:
            raise _ForgeDown(exc) from None
        looked_up.append(run.grading)
        return run, spec

    try:
        answer = await ctx.forge.grading.answer(request, lookup, now=ctx.now)
    except _ForgeDown as down:
        raise down.error from None
    except NotFound:
        log.info("runs.config_unasked")
        raise NotFound("This CI does not ask what a run is.") from None
    except (Forbidden, Rejected) as exc:
        if isinstance(exc, VariablesDiffer):
            reason = "variables"
        elif looked_up:
            reason = "answer"
        else:
            reason = "unverified"
        grading = str(looked_up[0]) if looked_up else None
        log.warning("runs.config_refused", reason=reason, grading=grading, detail=exc.detail)
        raise CiRequestRefused(REFUSED) from None
    except PortError as exc:
        log.warning("runs.config_key_unread", error=type(exc).__name__, detail=exc.detail)
        raise Unavailable("The CI's signing key could not be read.") from None
    [configured] = looked_up
    log.info("runs.configured", grading=str(configured))
    return answer


def bearer(authorization: str | None) -> str | None:
    """The token an `Authorization: Bearer <token>` header carries, none for
    any other header or none."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
