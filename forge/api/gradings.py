"""An organiser's controls over the gradings of a task they manage:
cancelling one in system_error with a sentence, retrying a finished one,
counting a broken one's submission as its last good result and taking that
back, rejudging every submission against the current publication, and reading the
task's gradings and a grading's run log; a contest's gradings as one feed
and its queue depth; the task a grading is of, for the guard of a route that
names only the grading; and the types they come back as.
"""

from forge.domain.grading import Fallback, GradingStatus
from forge.services.gradings import (
    FeedEntry,
    GradingRecord,
    QueueDepth,
    Rejudged,
    Submitter,
    cancel,
    clear_fallback,
    fall_back,
    feed,
    list,
    queue_depth,
    rejudge,
    retry,
    run_log,
    task_of,
)

__all__ = [
    "Fallback",
    "FeedEntry",
    "GradingRecord",
    "GradingStatus",
    "QueueDepth",
    "Rejudged",
    "Submitter",
    "cancel",
    "clear_fallback",
    "fall_back",
    "feed",
    "list",
    "queue_depth",
    "rejudge",
    "retry",
    "run_log",
    "task_of",
]
