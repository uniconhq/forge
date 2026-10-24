"""Grading as the package and the port describe it: where one grading of one
submission stands, the runs and agents at the CI, what one pipeline of a
grading is, the requests the CI makes about one and the answer it is
given, the token each of its steps is handed, and the clock a run keeps to.

A step of a grading proves who it is with a token derived from
`UNICON_TOKEN_ENCRYPTION_KEY`, the grading's id, its attempt and the step's
name (`step_token`), so the token is not stored: the step's row keeps its
SHA-256. A retry or a rejudge is a new row with a new id, so it is handed
new tokens.

The times agree with each other and with the CI. A machine gives one run
`RUN_TIMEOUT`, the longest the CI lets a run take, which the deployment
sets to 30 minutes. Of that, the checkout is allowed `CHECKOUT_ALLOWANCE`
and the report `REPORT_ALLOWANCE`, which leaves `WALL_CEILING` for the
steps: what a pipeline's steps may take is their time limits summed with a
margin for each container and one for the run (`wall_of`), which a save
refuses above the ceiling (`forge.domain.run_plan`).

A plan must also fit a machine: every step's memory and GPUs within what the
machines a task may run on have, and no step reaching the network on a
machine that gives none. Until computes advertise their machines (feature
12), every task runs on the platform's pool (`run_machine`), no GPU and no
network. A run's deadline is written when its first step asks for its job,
the wall clock and the reporting allowance after it, so a run that waited
for a machine loses none of its time.

A grading that is past what each of its states may take reads as a system
error, whatever its row still says, so nothing waits for ever and it is
tried again (`overdue`): one still `queued` `START_WAIT` after it
was made, since its run is started as soon as it is committed; one
`dispatched` `MACHINE_WAIT` after its run was started, a run no machine took
or whose first step never asked for its job; and one `running` past its
deadline, whose tokens are refused from then on.

A `dispatched` grading may also have a run the CI has lost: one it dropped
for good after the machine it handed the run to went quiet, while still
saying the run waits (the CI's implementation names the case it knows).
Waiting long is not being lost: at a contest's start a run can wait a
quarter of an hour behind others. So the CI is asked where the run is
(`RunState`) once the grading has been `dispatched` `LOST_CHECK_AFTER`; a
run the CI no longer holds reads as a system error, `LOST`, like an
overdue one.

A grading that ends in `system_error` is tried again once by itself, by
whatever finds it so: the CI's report, a start that fails, or a read that
finds it overdue or lost. The attempt that makes is marked `automatic`, and
one so marked is not tried again by itself, so a second failure waits for
staff. Staff end it by retrying it, as often as they like, or, when a retry
would only repeat the fault, by cancelling it with a sentence its contestant
reads (`cancel_reason`, at most `CANCEL_REASON_MAX` characters), which ends
its submission for good: it is graded again by nothing.

Of a submission's attempts, the latest that finished with a result counts,
until a newer one finishes with one: a failure or a cancel never takes a
result away, and a cancel voids only a submission that never had one.

A delayed submission, one with no result whose latest attempt failed, is
graded again only while its entrant has room under the task's
`submissions.max` (`room`): its submissions with a result, those waiting
that are not delayed, and the delayed ones made before it, fewer than the
most, so the first `max` to get a result are the ones that count. Without
room the new attempt ends `over_limit` at once, run by nothing.
"""

import base64
import hashlib
import hmac
import math
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from forge.domain.errors import InvalidReason
from forge.domain.ids import AgentId, OrgId, SubmissionId, VersionId

STEP_INFO = b"unicon-grading-step"

PLATFORM_POOL = "pool:platform"
"""The label every run is pinned to until orgs bring machines of their own:
only a machine the platform controls declares it."""
BOXED_SHAPE = "shape:rootless"
"""The label a run task's pipeline holding an `exec` step
carries beside its pool's, so only a machine of the shape phase 4.1 picked
takes it: Woodpecker's labels pick a machine for a whole pipeline."""
SIZE_LABEL = "size"
"""The label naming the slot a run task's pipeline needs, `size:<slot>`."""

RUN_TIMEOUT = timedelta(minutes=30)
CHECKOUT_ALLOWANCE = timedelta(minutes=4)
REPORT_ALLOWANCE = timedelta(minutes=1)
WALL_CEILING = RUN_TIMEOUT - CHECKOUT_ALLOWANCE - REPORT_ALLOWANCE
BASE_WALL = timedelta(seconds=60)
STEP_OVERHEAD = timedelta(seconds=15)
START_WAIT = timedelta(minutes=5)
MACHINE_WAIT = timedelta(hours=2)
LOST_CHECK_AFTER = timedelta(minutes=2)
NEVER_STARTED = "The grading never started."
NEVER_BEGAN = "The grading did not begin within two hours of being started."
OVERDUE = "The grading did not finish before its deadline."
LOST = "The machine that grades lost this grading before it began."


@dataclass(frozen=True, slots=True)
class Slot:
    """One size of a grading machine's slots: its name, the value of the
    `size` label its agents and the pipelines it fits carry, its cores, the
    most boxes a stage of a pipeline in it runs at once, and its memory in
    megabytes. The sizes are one table in deploy's settings
    (`machine/slots.json`), which a machine's setup reads to share its
    cores and memory out among agents, one per slot, and the forge reads
    as `UNICON_SLOT_SIZES`.
    """

    name: str
    cores: int
    memory_mb: int


@dataclass(frozen=True, slots=True)
class Machine:
    """The most one step may ask of a machine: memory in megabytes, GPU
    devices, and whether it may reach the network; and for a run task, the
    sizes of the slots its pipelines run in, smallest first.
    """

    memory_mb: int
    gpus: int
    network: bool
    slots: tuple[Slot, ...] = ()


PLATFORM_MACHINE = Machine(memory_mb=16384, gpus=0, network=False)
"""What a machine of the platform's pool gives one step where no slot sizes
are known: 16 GB of memory, no GPU and no network."""


def run_machine(slots: Collection[Slot]) -> Machine:
    """What a run task's step may take on the platform's machines: the
    biggest slot's memory, no GPU and no network, with every slot, smallest
    first, for the planner to fit each pipeline in one."""
    ordered = tuple(sorted(slots, key=lambda slot: slot.memory_mb))
    return Machine(memory_mb=ordered[-1].memory_mb, gpus=0, network=False, slots=ordered)


class RunState(StrEnum):
    """Where the CI has a run: `queued`, waiting for a machine; `taken` by a
    machine; `finished`; or `lost`, a run the CI says is unfinished and holds
    in no queue, or does not know at all.
    """

    QUEUED = "queued"
    TAKEN = "taken"
    FINISHED = "finished"
    LOST = "lost"


class GradingStatus(StrEnum):
    """Where one grading stands. `queued` waits for its run to be started,
    which happens as soon as the request that made it commits; `dispatched`
    is held by the CI, waiting for a machine or checking out; `running` has
    had a step ask for its job. It ends `done` with a result,
    `system_error`, a grading that failed for a reason of the platform's,
    never a grade: its run could not be started, or did not report before
    its deadline; `cancelled`, by staff ending one in `system_error`, or
    by a rejudge replacing one still being graded; or `over_limit`, made to
    grade a delayed submission again and ended at once, run by nothing,
    since its entrant had no room left under the task's `submissions.max`.
    """

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"
    OVER_LIMIT = "over_limit"


UNFINISHED = (GradingStatus.QUEUED, GradingStatus.DISPATCHED, GradingStatus.RUNNING)
AT_THE_CI = (GradingStatus.DISPATCHED, GradingStatus.RUNNING)
"""The statuses of a grading whose run the CI holds."""
FINISHED = (
    GradingStatus.DONE,
    GradingStatus.CANCELLED,
    GradingStatus.SYSTEM_ERROR,
    GradingStatus.OVER_LIMIT,
)
WAITING = (GradingStatus.QUEUED, GradingStatus.DISPATCHED)
"""The statuses of a grading waiting for a machine to begin it, which a
contest's queue depth counts."""


def overdue(
    status: GradingStatus,
    *,
    created_at: datetime,
    dispatched_at: datetime | None,
    deadline: datetime | None,
    now: datetime,
    lost: bool = False,
) -> str | None:
    """Why a grading is past what its state may take at `now`, or none while
    it is not. `lost` says the CI was asked and holds the run no more.
    """
    match status:
        case GradingStatus.QUEUED if now >= created_at + START_WAIT:
            return NEVER_STARTED
        case GradingStatus.DISPATCHED if (
            dispatched_at is not None and now >= dispatched_at + MACHINE_WAIT
        ):
            return NEVER_BEGAN
        case GradingStatus.DISPATCHED if lost:
            return LOST
        case GradingStatus.RUNNING if deadline is not None and now >= deadline:
            return OVERDUE
    return None


CANCEL_REASON_MAX = 500
"""The longest sentence staff cancel a grading with: a few lines its
contestant reads."""


def cancel_reason(reason: str) -> str:
    """The sentence a grading is cancelled with, trimmed. `InvalidReason`
    when it is empty or longer than `CANCEL_REASON_MAX`.
    """
    trimmed = reason.strip() if isinstance(reason, str) else ""
    if not trimmed:
        raise InvalidReason("Give a reason, so the entrant knows why.")
    if len(trimmed) > CANCEL_REASON_MAX:
        raise InvalidReason(f"A reason can be at most {CANCEL_REASON_MAX} characters. Shorten it.")
    return trimmed


class Place(StrEnum):
    """Where one of an entrant's submissions to a task stands against the
    task's `submissions.max`: it `holds` a place, having a result or
    waiting for one while not delayed; it is `delayed`, with no result and
    its latest attempt failed, holding none while it waits; or it holds
    `none`, void or over the limit.
    """

    HOLDS = "holds"
    DELAYED = "delayed"
    NONE = "none"


def room(places: Mapping[int, Place], limit: int, asking: Collection[int]) -> frozenset[int]:
    """Which of `asking`, an entrant's submissions to a task by number, each
    delayed or over the limit, may be graded again under a `submissions.max`
    of `limit`, `places` saying where each of the entrant's submissions to
    the task stands. The places held count first; then the delayed ones,
    those asked for among them, and those over the limit asked for, take
    what is left in the order they were made, so the earliest count: one
    delayed and not asked for keeps its place for its own retry.
    """
    asked = set(asking)
    holding = sum(
        1 for number, place in places.items() if place is Place.HOLDS and number not in asked
    )
    found: set[int] = set()
    for number in sorted(
        number for number, place in places.items() if number in asked or place is Place.DELAYED
    ):
        if holding >= limit:
            break
        holding += 1
        if number in asked:
            found.add(number)
    return frozenset(found)


def worth_asking(status: GradingStatus, dispatched_at: datetime | None, now: datetime) -> bool:
    """Whether to ask the CI whether it still holds a grading's run: one
    `dispatched` for `LOST_CHECK_AFTER`, long enough for the CI to have
    queued it and a machine's claim on it to have lapsed, and not yet past
    `MACHINE_WAIT`, which says it is overdue without asking.
    """
    return (
        status is GradingStatus.DISPATCHED
        and dispatched_at is not None
        and dispatched_at + LOST_CHECK_AFTER <= now < dispatched_at + MACHINE_WAIT
    )


@dataclass(frozen=True, slots=True)
class Enrolment:
    """A newly enrolled agent and the token it identifies itself with. The
    token exists only in this value.
    """

    agent: AgentId
    token: str


@dataclass(frozen=True, slots=True)
class InboundRequest:
    """A request the CI made to the platform, as it arrived: its method, its
    target, the path and query exactly as sent, its headers, looked up
    whatever their case, and its body, byte for byte, since what proves it
    the CI's covers them.
    """

    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class InboundAnswer:
    """What the platform answers the CI with: the body and its media type."""

    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """One pipeline of a run task's grading (phase 4.9), in the platform's
    words: the grading's row and which of its pipelines, `save`, `early` or
    `late`, the org on whose dispatch repository it runs, the commit the
    publication froze, the submission with the version its files went in
    with, none for a save, and the label of the machines that may take it.
    """

    grading: uuid.UUID
    pipeline: str
    org: OrgId
    publication_version: VersionId
    submission: SubmissionId | None
    submission_version: VersionId | None
    compute: str


@dataclass(frozen=True, slots=True)
class PipelineStepSpec:
    """One Woodpecker step of a pipeline: its name, its image by digest, its
    settings, the steps it waits for, whether it takes the org's cache,
    the user its container runs as, none for a step that runs as its image
    says, and whether it runs whatever the steps it waits for ended with,
    as the report step does.
    """

    name: str
    image: str
    settings: Mapping[str, Any]
    depends_on: tuple[str, ...] = ()
    cache: bool = False
    user: int | None = None
    always: bool = False


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    """What a pipeline runs, read from the platform's records as it is
    started: the image its checkout of the submission is made with, its
    steps in order, the report's last, the labels of the machines that may
    take it, and whether it checks the submission out at all, which it
    does only when one of its steps reads an entrant input.
    """

    clone_image: str
    steps: tuple[PipelineStepSpec, ...]
    labels: tuple[str, ...]
    clones: bool


PipelineLookup = Callable[[str | None, OrgId], Awaitable[tuple[PipelineRun, PipelineSpec]]]
"""How an implementation whose CI asks what a pipeline is finds it: by the
grading id the pipeline names, as it was given, and the org whose dispatch
repository the CI asks about. It answers only for a grading whose pipeline
is being started, and refuses anything else, so what runs comes from the
platform's records and never from the request."""


def step_token(key: bytes, grading: uuid.UUID, attempt: int, step: str) -> str:
    """The token one Woodpecker step of a run task's grading asks its door
    with: `base64url(HMAC-SHA256(k, "step:<grading>:<attempt>:<step>"))`,
    `k` derived from the key by HKDF-SHA256 under `STEP_INFO`, so it names
    the row, the attempt and the step, and no other step's door takes it.
    """
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=STEP_INFO).derive(key)
    message = b":".join((b"step", str(grading).encode(), str(attempt).encode(), step.encode()))
    mac = hmac.new(derived, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def token_hash(token: str) -> bytes:
    """What a step's row keeps of its step token."""
    return hashlib.sha256(token.encode()).digest()


def log_key(grading: uuid.UUID, attempt: int) -> str:
    """Where a grading's run log is kept in the results store."""
    return f"logs/{grading}/{attempt}.log"


RUN_LOG_MAX = 9 * 1024 * 1024
"""The largest run log read back for an organiser: the read is where the
bound holds."""


def wall_of(time_ms: Collection[int]) -> int:
    """How long a run of containers with these time limits may take, in
    seconds: the limits summed, with a margin for each container and one for
    the run. A run task's plan works it out per pipeline.
    """
    margin = BASE_WALL + STEP_OVERHEAD * len(time_ms)
    return max(1, math.ceil(sum(time_ms) / 1000 + margin.total_seconds()))


def run_deadline(began_at: datetime, wall: int) -> datetime:
    """The deadline of a pipeline whose first step asked for its job at
    `began_at` with `wall` seconds to grade.
    """
    return began_at + timedelta(seconds=wall) + REPORT_ALLOWANCE
