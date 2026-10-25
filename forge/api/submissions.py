"""A contestant's submissions: submitting uploads and values to a task, and
reading their own submissions, each with its grading as the task's scoring
shows it, the files one was made with and the door to download one; any
row's submission as organisers read it, everything filled in; a text a
result shows, to whoever is shown it, an entrant by where it is shown
(`TextAt`); and the types they take and come back as.
"""

from forge.domain.grading import GradingStatus
from forge.domain.run_showing import TextAt, ValueShown
from forge.domain.submission_state import SubmissionState
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
    organised_download,
    organised_files,
    read_organised_text,
    read_text,
    submit,
)

__all__ = [
    "GradingStatus",
    "Result",
    "Submission",
    "SubmissionState",
    "SubmittedFiles",
    "SubmittedInput",
    "TextAt",
    "ValueShown",
    "download",
    "files",
    "mine",
    "one",
    "organised",
    "organised_download",
    "organised_files",
    "read_organised_text",
    "read_text",
    "submit",
]
