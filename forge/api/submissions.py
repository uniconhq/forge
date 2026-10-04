"""A contestant's submissions: submitting uploads and values to a task, and
reading their own submissions, each with its gradings, the files one was
made with, the door to download one, and the log of its run where the stage
shows it, and the types they take and come back as.
"""

from forge.domain.definitions import Show
from forge.domain.grading import GradingStatus
from forge.domain.submissions import SubmittedInput
from forge.services.submissions import (
    Result,
    Submission,
    SubmittedFiles,
    download,
    files,
    mine,
    one,
    run_log,
    submit,
)

__all__ = [
    "GradingStatus",
    "Result",
    "Show",
    "Submission",
    "SubmittedFiles",
    "SubmittedInput",
    "download",
    "files",
    "mine",
    "one",
    "run_log",
    "submit",
]
