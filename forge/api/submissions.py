"""A contestant's submissions: submitting uploads and values to a task, and
reading their own submissions, each with its grading as the task's test
groups show it, the files one was made with and the door to download one,
and the types they take and come back as.
"""

from forge.domain.definitions import Show
from forge.domain.grading import GradingStatus
from forge.domain.scoring import Points
from forge.domain.showing import GroupShown, SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.services.submissions import (
    Result,
    Submission,
    SubmittedFiles,
    download,
    files,
    mine,
    one,
    submit,
)

__all__ = [
    "GradingStatus",
    "GroupShown",
    "Points",
    "Result",
    "Show",
    "Submission",
    "SubmissionState",
    "SubmittedFiles",
    "SubmittedInput",
    "download",
    "files",
    "mine",
    "one",
    "submit",
]
