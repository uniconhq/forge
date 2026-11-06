from collections.abc import Iterable, Mapping, Sequence

from forge.domain.checks import check_paths
from forge.domain.grading import Slot
from forge.domain.run_plan.models import FilesNamed, PipelineStep
from forge.domain.run_plan.plan_helpers import _references_in
from forge.domain.run_plan.records import _READS, BOXED, CLASS_WORDS, StepClass
from forge.domain.run_task import RunTask
from forge.domain.run_workflow import RunWorkflowCheck
from forge.domain.types import FILES
from forge.domain.workflow_definition import RunWorkflow
from forge.domain.yaml_models import Problem


def files_named(workflow: RunWorkflow, task: RunTask) -> FilesNamed:
    """What the plan of `task` names, which a save reads the facts of before
    it makes the plan.
    """
    named_task: set[str] = {program.folder for program in task.programs.values()}
    named_task |= check_paths(task.checks)
    named_workflow: set[str] = set()
    inner: dict[int, set[str]] = {}
    for step in workflow.steps:
        for found in _references_in(step.with_.values()):
            if found.kind == "workflow":
                if found.pin:
                    inner.setdefault(found.pin, set()).add(found.name)
                else:
                    named_workflow.add(found.name)
                continue
            declared = workflow.inputs.get(found.name) if found.kind == "inputs" else None
            if declared is None or declared.type not in FILES:
                continue
            given = task.settings.get(found.name)
            if isinstance(given, str):
                named_task.add(given)
            for run in task.runs:
                value = run.key.get(found.name)
                if isinstance(value, str):
                    named_task.add(value)
    return FilesNamed(
        frozenset(named_task),
        frozenset(named_workflow),
        {pin: frozenset(paths) for pin, paths in inner.items()},
    )


def order_problems(
    classes: Mapping[str, StepClass], reads: Mapping[str, Sequence[str]]
) -> list[Problem]:
    """A problem for each step that reads one done more often, which would
    run before its input exists. A step's class follows from what it reads,
    so none is found unless that is wrong; the planner checks it anyway.
    """
    problems: list[Problem] = []
    for reader, read in reads.items():
        wants = _READS[classes[reader]]
        for each in read:
            gives = _READS[classes[each]]
            if gives[0] > wants[0] or gives[1] > wants[1]:
                problems.append(
                    Problem(
                        path="workflow",
                        message=f"The step {reader} runs {CLASS_WORDS[classes[reader]]}, and "
                        f"reads {each}, which runs {CLASS_WORDS[classes[each]]}, so {reader} "
                        f"would run before what it reads exists.",
                    )
                )
    return problems


def cache_problems(checked: RunWorkflowCheck) -> list[Problem]:
    """A problem for each step whose primitive reads the task's files
    through the machine's cache but makes no isolate boxes: the cache is
    root's, and only `exec` runs as root, every other step as
    the grading's own user, which cannot read it.
    """
    return [
        Problem(
            path="workflow",
            message=f"The step {step.id} uses {step.use}, which reads the task's files "
            "through the machine's cache, but only primitives that run programs in boxes "
            "may, such as unicon/exec.",
        )
        for step in checked.steps.values()
        if step.declaration.cache and step.use.partition("@")[0] not in BOXED
    ]


def slot_of(steps: Sequence[PipelineStep], slots: Iterable[Slot]) -> Slot | None:
    """The smallest slot that holds the largest of the steps, which run one
    after another (#630), none when no slot does."""
    needed = max((step.limits.memory_mb for step in steps), default=0)
    return min(
        (slot for slot in slots if slot.memory_mb >= needed),
        key=lambda slot: slot.memory_mb,
        default=None,
    )
