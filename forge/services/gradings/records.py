import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from forge.db.tables.gradings import EARLY
from forge.domain.grading import GradingStatus
from forge.domain.ids import PublicationId, TaskId, WorkspaceId
from forge.domain.regrades import Compared

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
"""Where a CI that asks what a pipeline is asks it, under the platform's
internal URL."""


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
