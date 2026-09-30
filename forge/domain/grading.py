"""Grading as the package and the port describe it: where one grading of one
submission at one stage stands, the runs and agents at the CI, and the
secret a grading run is handed.

A grading run proves who it is with a secret derived from
`UNICON_TOKEN_ENCRYPTION_KEY`, the grading's id and which of its runs it is,
so it is not stored: the callback token the harness reports back with,
whose SHA-256 the grading's row keeps. It is `base64url(HMAC-SHA256(k,
"<purpose>:" || grading id || ":" || run))`, the id written as its canonical
text, `run` which of the grading's runs it is, from 0, and the result
without padding, with `k` derived from the key by HKDF-SHA256 under an info
naming the purpose. A retry or a rejudge is a new row with a new id, so it
is handed a new secret.
"""

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from enum import StrEnum

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from forge.domain.ids import AgentId, RunId

CALLBACK_INFO = b"unicon-grading-callback"


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


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


def _secret(key: bytes, info: bytes, purpose: bytes, grading: uuid.UUID, run: int) -> str:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)
    message = b":".join((purpose, str(grading).encode(), str(run).encode()))
    mac = hmac.new(derived, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def callback_token(key: bytes, grading: uuid.UUID, *, run: int) -> str:
    """The token the grading's run numbered `run` reports back with."""
    return _secret(key, CALLBACK_INFO, b"callback", grading, run)


def token_hash(token: str) -> bytes:
    """What a grading's row keeps of its callback token."""
    return hashlib.sha256(token.encode()).digest()
