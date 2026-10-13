"""A contestant's submissions: submitting uploads and values to a task, and
reading their own submissions, each with its grading as the task's test
groups show it and the words each reported value is named by, the files
one was made with and the door to download one; any row's submission as
organisers read it, everything filled in; and the types they take and
come back as.
"""

from forge.domain.definitions import Fold, Show
from forge.domain.grading import GradingStatus
from forge.domain.meanings import ValueMeaning
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
    organised,
    submit,
)

__all__ = [
    "Fold",
    "GradingStatus",
    "GroupShown",
    "Points",
    "Result",
    "Show",
    "Submission",
    "SubmissionState",
    "SubmittedFiles",
    "SubmittedInput",
    "ValueMeaning",
    "download",
    "files",
    "mine",
    "one",
    "organised",
    "submit",
]
