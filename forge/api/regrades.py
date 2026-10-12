"""What a regrade of many changed, for the organisers of a contest: each
task's latest regrade summed up, taking one off the contest's page, and a
regrade's gradings each with what it did to its submission; and the types
they come back as.
"""

from forge.domain.regrades import Change, Compared, RegradeCause, Side
from forge.services.regrades import Summary, dismiss, gradings_of, latest

__all__ = [
    "Change",
    "Compared",
    "RegradeCause",
    "Side",
    "Summary",
    "dismiss",
    "gradings_of",
    "latest",
]
