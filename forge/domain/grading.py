"""Grading as the package and the port describe it: where one grading of one
submission at one stage stands, the runs and agents at the CI, what one
grading run is, the requests the CI makes about one and the answer it is
given, the two secrets a run is handed, and the clock a run keeps to.

A grading run proves who it is with two secrets derived from
`UNICON_TOKEN_ENCRYPTION_KEY`, the grading's id and which of its runs it is,
so neither is stored: the callback token the harness reports back with,
whose SHA-256 the grading's row keeps, and the envelope key the envelope's
URL carries. Each is `base64url(HMAC-SHA256(k, "<purpose>:" || grading id))`,
the id written as its canonical text and the result without padding, with
`k` derived from the key by HKDF-SHA256 under an info naming the purpose. A
grading has one run: a retry or a rejudge is a new row with a new id, so it
is handed new secrets.

The times agree with each other and with the CI. A machine gives one run
`RUN_TIMEOUT`, the CI's pipeline timeout, which the deployment sets to 30
minutes (`WOODPECKER_DEFAULT_PIPELINE_TIMEOUT`). Of that, the two checkouts
are allowed `CHECKOUT_ALLOWANCE` and the reports `REPORT_ALLOWANCE`, which
leaves `WALL_CEILING` for the harness: an envelope's `limits.wall_seconds` is
what its plan's steps may take, their time limits summed with a margin for
each container and one for the run, and never more than that. A run's
deadline is written when its harness first fetches the envelope, the wall
clock and the reporting allowance after it, so a run that waited for a
machine loses none of its time.

A grading that is past what each of its states may take reads as a system
error, whatever its row still says, so nothing waits for ever and an
organiser can retry it (`overdue`): one still `queued` `START_WAIT` after it
was made, since its run is started as soon as it is committed; one
`dispatched` `MACHINE_WAIT` after its run was started, a run no machine took
or that never reached the harness; and one `running` past its deadline,
whose token is refused from then on.
"""

import base64
import hashlib
import hmac
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from forge.domain.ids import AgentId, PublicationId, SubmissionId, TaskId, VersionId
from forge.domain.plans import Plan

CALLBACK_INFO = b"unicon-grading-callback"
ENVELOPE_INFO = b"unicon-grading-envelope"

CLONE_IMAGE = (
    "ghcr.io/uniconhq/clone@sha256:8968497186f0e9d578a8a5202688cfb644161fcf6fb3365192ac627b031578a4"
)
"""The image the CI checks a task and a submission out with, of runner release
v0.3.0, from that release's `images.json`: what `UNICON_CLONE_IMAGE` is
unless a deployment sets it."""

PLATFORM_POOL = "pool:platform"
"""The label every run is pinned to until orgs bring machines of their own:
only a machine the platform controls declares it."""

RUN_TIMEOUT = timedelta(minutes=30)
CHECKOUT_ALLOWANCE = timedelta(minutes=4)
REPORT_ALLOWANCE = timedelta(minutes=1)
WALL_CEILING = RUN_TIMEOUT - CHECKOUT_ALLOWANCE - REPORT_ALLOWANCE
BASE_WALL = timedelta(seconds=60)
STEP_OVERHEAD = timedelta(seconds=15)
START_WAIT = timedelta(minutes=5)
MACHINE_WAIT = timedelta(hours=2)
NEVER_STARTED = "Its run was never started."
NEVER_BEGAN = "Its run did not begin within two hours of being started."
OVERDUE = "Its run did not report before its deadline."


class GradingStatus(StrEnum):
    """Where one grading stands. `queued` waits for its run to be started,
    which happens as soon as the request that made it commits; `dispatched`
    is held by the CI, waiting for a machine or checking out; `running` has
    had its envelope fetched by the harness. It ends `done` with a verdict,
    `cancelled` by an organiser, or `system_error`, a grading that failed for
    a reason of the platform's, never a grade: its run could not be started,
    or did not report before its deadline.
    """

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"


UNFINISHED = (GradingStatus.QUEUED, GradingStatus.DISPATCHED, GradingStatus.RUNNING)
AT_THE_CI = (GradingStatus.DISPATCHED, GradingStatus.RUNNING)
"""The statuses of a grading whose run the CI holds."""
FINISHED = (GradingStatus.DONE, GradingStatus.CANCELLED, GradingStatus.SYSTEM_ERROR)


def overdue(
    status: GradingStatus,
    *,
    created_at: datetime,
    dispatched_at: datetime | None,
    deadline: datetime | None,
    now: datetime,
) -> str | None:
    """Why a grading is past what its state may take at `now`, or none while
    it is not.
    """
    match status:
        case GradingStatus.QUEUED if now >= created_at + START_WAIT:
            return NEVER_STARTED
        case GradingStatus.DISPATCHED if (
            dispatched_at is not None and now >= dispatched_at + MACHINE_WAIT
        ):
            return NEVER_BEGAN
        case GradingStatus.RUNNING if deadline is not None and now >= deadline:
            return OVERDUE
    return None


@dataclass(frozen=True, slots=True)
class Enrolment:
    """A newly enrolled agent and the token it identifies itself with. The
    token exists only in this value.
    """

    agent: AgentId
    token: str


@dataclass(frozen=True, slots=True)
class GradingRun:
    """What one run of one grading is, in the platform's words: the grading,
    the task and the publication it grades against with the version that
    publication froze, the submission with the version its files went in
    with, where the run fetches its envelope, and the label of the machines
    that may take it. The port turns it into the CI's own terms: the
    variables a run is started with and the steps it is answered with.
    """

    grading: uuid.UUID
    task: TaskId
    publication: PublicationId
    publication_version: VersionId
    submission: SubmissionId
    submission_version: VersionId
    envelope_url: str
    compute: str


@dataclass(frozen=True, slots=True)
class CiRequest:
    """A request the CI made to the platform, as it arrived: its method, its
    target, the path and query exactly as sent, its headers, looked up
    whatever their case, and its body, byte for byte, since the signature
    covers them.
    """

    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class ConfigAsk:
    """The CI asking what a run is, once its signature is checked: the task
    whose run it is, the grading id the run was started with as it was
    given, every variable it was started with, and where the CI clones the
    task from.
    """

    task: TaskId
    grading: str | None
    variables: Mapping[str, str]
    clone_url: str


@dataclass(frozen=True, slots=True)
class CiAnswer:
    """What the platform answers the CI with: the body and its media type."""

    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class RunPlaces:
    """Where a run is at the forge and on the machine: the task, the
    publication by name and version, the submission by place, name and
    version, and where the run's two checkouts are. The CI's answer clones
    from them, and the envelope carries the submission and the checkouts.
    """

    task: Mapping[str, str]
    publication: Mapping[str, str]
    submission: Mapping[str, str]
    checkouts: Mapping[str, str]


def _secret(key: bytes, info: bytes, purpose: bytes, grading: uuid.UUID) -> str:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)
    message = b":".join((purpose, str(grading).encode()))
    mac = hmac.new(derived, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def callback_token(key: bytes, grading: uuid.UUID) -> str:
    """The token the grading's run reports back with."""
    return _secret(key, CALLBACK_INFO, b"callback", grading)


def envelope_key(key: bytes, grading: uuid.UUID) -> str:
    """The key the URL of the envelope of the grading's run carries."""
    return _secret(key, ENVELOPE_INFO, b"envelope", grading)


def token_hash(token: str) -> bytes:
    """What a grading's row keeps of its callback token."""
    return hashlib.sha256(token.encode()).digest()


def log_key(grading: uuid.UUID, attempt: int) -> str:
    """Where a grading's run log is kept in the results store."""
    return f"logs/{grading}/{attempt}.log"


RUN_LOG_MAX = 9 * 1024 * 1024
"""The largest run log read back for a contestant. The harness cuts the log
it writes to 8 MiB and a line saying what it left out; the URL it writes
with takes any length, so the read is where the bound holds."""


def wall_seconds(plan: Plan) -> int:
    """How long the harness may take over the plan: every step's time limit
    summed, which for a batch is already its tests' together, with a margin
    for each container and one for the run, and never more than
    `WALL_CEILING`.
    """
    steps = sum(step.limits.time_ms for step in plan.steps) / 1000
    margin = BASE_WALL + STEP_OVERHEAD * len(plan.steps)
    wanted = math.ceil(steps + margin.total_seconds())
    return max(1, min(wanted, int(WALL_CEILING.total_seconds())))


def run_deadline(fetched_at: datetime, wall: int) -> datetime:
    """The deadline of a run whose harness fetched its envelope at
    `fetched_at` with `wall` seconds to grade: its callback token is refused
    after it.
    """
    return fetched_at + timedelta(seconds=wall) + REPORT_ALLOWANCE
