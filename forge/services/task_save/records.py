import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from forge.domain.definitions import Show, TaskDefinition
from forge.domain.ids import PublicationId, VersionId
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.plans import Compiled
from forge.domain.publication_record import Check
from forge.domain.run_plan import RunPlan
from forge.domain.run_task import RUNS_PATH, RunTask
from forge.domain.save_effect import SaveEffect
from forge.domain.yaml_models import Problem

SAVE_MESSAGE = "Save"
CHANGED_CONTENT = "new:"
LANDED_UNDER = (
    "Someone else saved this task while yours was being saved, so the task as it now stands "
    "was not checked. Save again to publish it."
)
PIN_ANSWER = "pin"
"""What the answer to a save's question carries when the save moves the
workflow's pin, so only an answer to that question confirms it."""
PIN_MOVED = (
    "This save moves the workflow's pin, so its submissions are graded again on the new "
    "version. Once that regrade runs, its summary lists each value and verdict it changed."
)
SHOWN_WORDS = {
    Show.ALWAYS: "as soon as a grading ends",
    Show.VERDICT: "as an outcome before the reveal",
    Show.AFTER_CLOSE: "at the reveal",
}
GROUPS_KEPT = timedelta(hours=1)
"""How long this process keeps how a publication shows each test group: a
publication never changes, so the time only bounds what is held."""
OPENNESS = {Show.AFTER_CLOSE: 0, Show.VERDICT: 1, Show.ALWAYS: 2}
"""How much of a group each `show` lets contestants see, least first."""


@dataclass(frozen=True, slots=True)
class Published:
    """A save that published: the publication and its number, whether it
    changed how the task grades and what, what the save says of the task
    beside publishing it, its sealed steps and a bounded value its `credit`
    does not name, how many submissions it queued to be graded again, and
    what it did, in the words a running contest asks with before a save;
    `check`, where a run task's publication stands while its save row
    has not let it take over (`pipelines.checks`), none otherwise; and
    `regrade`, the regrade it started, for its summary, none when it
    started none now.
    """

    publication: PublicationId
    number: int
    grading_changed: bool
    changes: tuple[str, ...]
    notes: tuple[str, ...]
    regraded: int
    effect: SaveEffect
    check: Check | None = None
    regrade: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Draft:
    """A save kept as a draft: the version its files were written as, and
    either the errors that kept it from being published, each at its YAML
    path, or, for a valid save kept as a draft, what it would have changed
    about how the task grades.
    """

    version: VersionId
    errors: tuple[Problem, ...]
    held_back: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Checked:
    """What checking a state found: the task's settings and its compiled
    plan, or for a run task its runs (`run_task`) and its plan (`run_plan`),
    when it is valid, and every problem when it is not, with which workflow
    each workflow name it uses is, by the forge's own id for it.
    """

    definition: TaskDefinition | None
    compiled: Compiled | None
    errors: tuple[Problem, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)
    run_task: RunTask | None = None
    run_plan: RunPlan | None = None

    def plan_files(self) -> dict[str, bytes]:
        """What a save of the valid state writes under `plans/`: the plan, and
        for a run task its runs file.
        """
        if self.run_task is not None:
            assert self.run_plan is not None
            return {
                RUNS_PATH: self.run_task.runs_file().to_bytes(),
                PLAN_PATH: self.run_plan.to_bytes(),
            }
        assert self.compiled is not None
        return {PLAN_PATH: self.compiled.plan.to_bytes()}
