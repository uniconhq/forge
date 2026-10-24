"""Names a plan of either format uses: its path, its images, its limits and its report caps."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, PlainValidator

from forge.domain.blocks.evaluate import Column
from forge.domain.blocks.numbers import Better, exact
from forge.domain.primitives import IMAGE
from forge.domain.yaml_models import Model, Number

PLANS_FOLDER = "plans/"
PLAN_PATH = "plans/plan.json"
STEP_ID = r"^[a-z0-9][a-z0-9_-]*$"
# A step's name inside a plan: a written id, or the path of written ids
# joined by `/` once a workflow holds another as a box.
FLAT_STEP_ID = r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$"
PRIMITIVE = r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"
SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
RESULT_MAX = 1024 * 1024
ERROR_LIMIT = 500


def _image(value: object) -> str:
    if not isinstance(value, str) or not IMAGE.match(value):
        raise ValueError("must be an image by digest")
    return value


Image = Annotated[str, PlainValidator(_image)]


class StepLimits(Model):
    time_ms: Annotated[int, Field(ge=1)]
    cpu_ms: Annotated[int, Field(ge=1)]
    memory_mb: Annotated[int, Field(ge=1)]
    pids: Annotated[int, Field(ge=1)]
    output_mb: Annotated[int, Field(ge=1)]
    gpus: Annotated[int, Field(ge=0)]


class ReportEntry(Model):
    """One name a plan reports: the step and output it reads, and, for a
    number of a run task's plan, which way is better and its bounds, so a
    missing report's worst bound is read from the plan alone.
    """

    step: Annotated[str, Field(pattern=STEP_ID)]
    output: Annotated[str, Field(min_length=1)]
    better: Better | None = None
    at_least: Number | None = None
    at_most: Number | None = None

    def column(self) -> Column:
        """The entry as the blocks read a report: which way is better and
        its bounds, exact.
        """
        return Column(self.better, exact(self.at_least), exact(self.at_most))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What a publication's grading depends on: its plan by path in the task
    repo, and a digest of each data file the plan names, by its path.
    """

    plans: Mapping[str, bytes]
    data: Mapping[str, str]
