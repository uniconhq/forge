"""A contestant's submissions: submitting uploads and values to a task, and
reading their own submissions, each with its gradings and the files one was
made with, and the types they take and come back as.
"""

from forge.domain.definitions import Show
from forge.domain.grading import GradingStatus
from forge.domain.submissions import SubmittedInput
from forge.services.submissions import (
    Result,
    Submission,
    SubmittedFiles,
    file,
    files,
    mine,
    one,
    submit,
)

__all__ = [
    "GradingStatus",
    "Result",
    "Show",
    "Submission",
    "SubmittedFiles",
    "SubmittedInput",
    "file",
    "files",
    "mine",
    "one",
    "submit",
]
