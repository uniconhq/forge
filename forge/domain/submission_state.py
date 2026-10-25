"""Where a submission stands as its contestant is told."""

from enum import StrEnum

from forge.domain.grading import GradingStatus


class SubmissionState(StrEnum):
    """Where a submission stands as its contestant is told, in TASK-FORMAT.md
    section 1.7's words, which say nothing of the CI's stages: `queued`
    until its run is started, `grading` while the CI holds it or runs it,
    `graded` once it has a result, `delayed` while it has none and its
    latest attempt failed for a reason of the platform's, `over_limit` once
    it was to be graded again and its entrant had no room left under the
    task's `submissions.max`, and `cancelled` once staff ended it. A run
    task's submission with no run shown during the contest is `submitted`
    once graded and until the task reveals, which a page words "Submitted,
    results after the contest", since `graded` would read as nothing run.
    """

    QUEUED = "queued"
    GRADING = "grading"
    GRADED = "graded"
    SUBMITTED = "submitted"
    DELAYED = "delayed"
    OVER_LIMIT = "over_limit"
    CANCELLED = "cancelled"


_TOLD = {
    GradingStatus.QUEUED: SubmissionState.QUEUED,
    GradingStatus.DISPATCHED: SubmissionState.GRADING,
    GradingStatus.RUNNING: SubmissionState.GRADING,
    GradingStatus.SYSTEM_ERROR: SubmissionState.DELAYED,
    GradingStatus.DONE: SubmissionState.GRADED,
    GradingStatus.CANCELLED: SubmissionState.CANCELLED,
    GradingStatus.OVER_LIMIT: SubmissionState.OVER_LIMIT,
}


def told(status: GradingStatus) -> SubmissionState:
    """Where the attempt a submission counts stands as its own row is told:
    the latest that finished with a result, `graded`, or, while none has,
    the latest. A run in `system_error` is `delayed`, with nothing else of
    it, neither its cause nor what it reached: it is a fault of the
    platform's or of setter code and never of the contestant's, and the
    word tells no more of a sealed step than the wait it stands for does.
    Organisers read the grading's own status.
    """
    return _TOLD[status]
