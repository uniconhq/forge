"""Releasing a task's results and taking a release back, for its
managers and admins, where they stand for its observers, and the types
they come back as.
"""

from forge.services.results import (
    TAKE_BACK_WARNING,
    ResultsAction,
    ResultsState,
    hide_again,
    release,
    state,
)

__all__ = ["TAKE_BACK_WARNING", "ResultsAction", "ResultsState", "hide_again", "release", "state"]
