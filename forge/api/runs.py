"""What a grading run calls, with no session, each handed over as it arrived:
the CI's configuration extension and the envelope the harness fetches; where
each is served; and the types they take and come back as.
"""

from forge.domain.grading import CiAnswer, CiRequest
from forge.services.gradings import CI_CONFIG_PATH, ENVELOPE_PATH
from forge.services.runs import config, envelope

__all__ = [
    "CI_CONFIG_PATH",
    "ENVELOPE_PATH",
    "CiAnswer",
    "CiRequest",
    "config",
    "envelope",
]
