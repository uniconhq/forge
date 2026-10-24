"""What a grading run calls, with no session, each handed over as it arrived:
the CI asking what a pipeline is; a step asking for its job, its
upload links and the files the forge serves it, whether its token works
before a large body is read, saying it is done, and its pipeline's report;
where each is served; and the types they take and come back as.
"""

from forge.domain.grading import GradingStatus, InboundAnswer, InboundRequest
from forge.services.gradings import CI_CONFIG_PATH
from forge.services.runs import config
from forge.services.steps import (
    DONE_PATH,
    FILE_PATH,
    STEP_PATH,
    UPLOADS_PATH,
    step_admits,
    step_done,
    step_file,
    step_job,
    step_report,
    step_uploads,
)

__all__ = [
    "CI_CONFIG_PATH",
    "DONE_PATH",
    "FILE_PATH",
    "STEP_PATH",
    "UPLOADS_PATH",
    "GradingStatus",
    "InboundAnswer",
    "InboundRequest",
    "config",
    "step_admits",
    "step_done",
    "step_file",
    "step_job",
    "step_report",
    "step_uploads",
]
