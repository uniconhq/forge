"""A contestant's submissions: submitting uploads and values to a task, and
reading their own submissions, each with its grading as the task's test
groups show it and the words each reported value is named by, the files
one was made with and the door to download one; any row's submission as
organisers read it, everything filled in; a text a run task's result shows,
to whoever is shown it, an entrant by where it is shown (`TextAt`); and the
types they take and come back as.
"""

from forge.domain.definitions import Show
from forge.domain.grading import GradingStatus
from forge.domain.meanings import ValueMeaning
from forge.domain.run_showing import TextAt, ValueShown
from forge.domain.showing import GroupShown, SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.domain.workflow_definition import Fold
from forge.services.submissions import (
    Result,
    Submission,
    SubmittedFiles,
    download,
    files,
    mine,
    one,
    organised,
    read_organised_text,
    read_text,
    submit,
)

__all__ = [
    "Fold",
    "GradingStatus",
    "GroupShown",
    "Result",
    "Show",
    "Submission",
    "SubmissionState",
    "SubmittedFiles",
    "SubmittedInput",
    "TextAt",
    "ValueMeaning",
    "ValueShown",
    "download",
    "files",
    "mine",
    "one",
    "organised",
    "read_organised_text",
    "read_text",
    "submit",
]
