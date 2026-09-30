"""Grading as the package and the port describe it: where one grading of one
submission at one stage stands, the runs and agents at the CI, what one
grading run is, the two secrets a run is handed, and the clock a run keeps
to.

A grading run proves who it is with two secrets derived from
`UNICON_TOKEN_ENCRYPTION_KEY`, the grading's id and which of its runs it is,
so neither is stored: the callback token the harness reports back with,
whose SHA-256 the grading's row keeps, and the envelope key the envelope's
URL carries. Each is `base64url(HMAC-SHA256(k, "<purpose>:" || grading id ||
":" || run))`, the id written as its canonical text, `run` which of the
grading's runs it is, from 0, and the result without padding, with `k`
derived from the key by HKDF-SHA256 under an info naming the purpose. A retry
or a rejudge is a new row with a new id, so each is handed new secrets.

A machine gives one run `RUN_TIMEOUT`, the CI's pipeline timeout, which the
deployment sets to 30 minutes (`WOODPECKER_DEFAULT_PIPELINE_TIMEOUT`). A
run's deadline is one run timeout after its start, and a run the CI still
holds unstarted by then is looked at again every `PENDING_RECHECK`.
"""

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from forge.domain.ids import AgentId, PublicationId, RunId, SubmissionId, TaskId, VersionId

CALLBACK_INFO = b"unicon-grading-callback"
ENVELOPE_INFO = b"unicon-grading-envelope"

PLATFORM_POOL = "pool:platform"
"""The label every run is pinned to until orgs bring machines of their own:
only a machine the platform controls declares it."""

RUN_TIMEOUT = timedelta(minutes=30)
PENDING_RECHECK = timedelta(minutes=5)
FIND_MARGIN = timedelta(minutes=2)
"""How much earlier than a grading entered the queue a run of it is looked
for, since the CI's clock and the platform's need not agree."""
FIRST_START_RETRY = timedelta(seconds=5)
LONGEST_START_RETRY = timedelta(minutes=5)


class GradingStatus(StrEnum):
    """Where one grading stands. `queued` waits for its run to be started;
    `dispatching` sent a start whose answer never came back, so the next try
    looks for the run before it starts another; `dispatched` is held by the
    CI, waiting for a machine or checking out; `running` has had its envelope
    fetched by the harness. It ends `done` with a verdict, `cancelled` by an
    organiser, or `system_error`, a grading that failed for a reason of the
    platform's, never a grade. `failed` is a status the table keeps for a
    grading that ends without a verdict for a reason of the task's; no path
    in this version ends in it.
    """

    QUEUED = "queued"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"


UNFINISHED = (
    GradingStatus.QUEUED,
    GradingStatus.DISPATCHING,
    GradingStatus.DISPATCHED,
    GradingStatus.RUNNING,
)
WAITING = (GradingStatus.QUEUED, GradingStatus.DISPATCHING)
"""The statuses of a grading whose run is still to be started."""
AT_THE_CI = (GradingStatus.DISPATCHED, GradingStatus.RUNNING)
"""The statuses of a grading whose run the CI holds."""


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ENDED = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)
"""The statuses of a run the CI has finished with, whatever became of it."""


@dataclass(frozen=True, slots=True)
class Run:
    id: RunId
    status: RunStatus


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
    variables a run is started with.
    """

    grading: uuid.UUID
    task: TaskId
    publication: PublicationId
    publication_version: VersionId
    submission: SubmissionId
    submission_version: VersionId
    envelope_url: str
    compute: str


def _secret(key: bytes, info: bytes, purpose: bytes, grading: uuid.UUID, run: int) -> str:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)
    message = b":".join((purpose, str(grading).encode(), str(run).encode()))
    mac = hmac.new(derived, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def callback_token(key: bytes, grading: uuid.UUID, *, run: int) -> str:
    """The token the grading's run numbered `run` reports back with."""
    return _secret(key, CALLBACK_INFO, b"callback", grading, run)


def envelope_key(key: bytes, grading: uuid.UUID, *, run: int) -> str:
    """The key the URL of the envelope of the grading's run numbered `run`
    carries.
    """
    return _secret(key, ENVELOPE_INFO, b"envelope", grading, run)


def token_hash(token: str) -> bytes:
    """What a grading's row keeps of its callback token."""
    return hashlib.sha256(token.encode()).digest()


def start_retry_wait(failures: int) -> timedelta:
    """How long a grading waits after its start failed `failures` times: five
    seconds after the first, doubling each time, up to five minutes, so a CI
    that is down is asked less and less often.
    """
    doublings = min(max(failures - 1, 0), 16)
    wait: timedelta = FIRST_START_RETRY * 2**doublings
    return min(wait, LONGEST_START_RETRY)
