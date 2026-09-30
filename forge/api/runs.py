"""What a grading run calls, with no session, each handed over as it arrived:
the CI's configuration extension, the envelope the harness fetches, and the
callback it reports through; where each is served; and the types they take
and come back as.
"""

from forge.domain.grading import CiAnswer, CiRequest, GradingStatus
from forge.services.gradings import CALLBACK_PATH, CI_CONFIG_PATH, ENVELOPE_PATH
from forge.services.runs import callback, config, envelope

__all__ = [
    "CALLBACK_PATH",
    "CI_CONFIG_PATH",
    "ENVELOPE_PATH",
    "CiAnswer",
    "CiRequest",
    "GradingStatus",
    "callback",
    "config",
    "envelope",
]
