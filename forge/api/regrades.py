"""What a regrade of many changed, for the organisers of a contest: each
task's latest regrade summed up, taking one off the contest's page, and a
regrade's gradings each with what it did to its submission, a run task's
regrade by the values and verdicts it changed; and the types they come
back as.
"""

from forge.domain.regrades import (
    BoardDiff,
    ChangeKind,
    Compared,
    RegradeCause,
    Side,
    ValueChange,
    VerdictChange,
)
from forge.services.regrades import (
    RunSummary,
    SubmissionChange,
    Summary,
    dismiss,
    gradings_of,
    latest,
    run_summary,
)

__all__ = [
    "BoardDiff",
    "ChangeKind",
    "Compared",
    "RegradeCause",
    "RunSummary",
    "Side",
    "SubmissionChange",
    "Summary",
    "ValueChange",
    "VerdictChange",
    "dismiss",
    "gradings_of",
    "latest",
    "run_summary",
]
