from dataclasses import dataclass, replace

from forge.domain.definitions import TASK_FILE
from forge.domain.errors import InvalidName, NotFound
from forge.domain.ids import TaskId
from forge.domain.roles import Role, task_scope
from forge.domain.workflow_definition import parse_workflow_ref
from forge.domain.yaml_models import InvalidDefinition, load_mapping
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import workflows
from forge.services.access import Organiser, require
from forge.services.task_save import sources
from forge.services.task_save.sources import _workflow


@dataclass(frozen=True, slots=True)
class DeclaredInput:
    """One input a workflow declares, as the task form shows it: its id, its
    type, its options on an enum, whether a task may leave it out, and the
    title, line of help and unit the workflow gives the organiser for it,
    each none when it gives none. Who gives it, and whether it repeats, are
    the task's (SIMPLER-CORE.md 8), so the form reads each as given once by
    the task until it says. A workflow gives no defaults: an entrant input's
    `default` is the task's, in `task.yaml`.
    """

    id: str
    type: str
    options: tuple[str, ...] | None
    optional: bool
    title: str | None = None
    help: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowForm:
    """What the task form is built from: the workflow the task's `task.yaml`
    names, as written, and the inputs it declares, in the order the
    workflow gives them, or `problem`, the reason there are none: no
    `task.yaml` or one that does not read as YAML, no workflow named, or one
    that cannot be read or is in an old form; `graded`, whether the task has
    a graded submission; and `newer`, the workflow's latest version when it
    comes after the one the task names, which the task keeps until someone
    saves it naming another (Task 10.6.5).
    """

    workflow: str | None
    inputs: tuple[DeclaredInput, ...] = ()
    problem: str | None = None
    graded: bool = False
    newer: str | None = None


@action
async def workflow_form(ctx: Context, organiser: Organiser, task: TaskId) -> WorkflowForm:
    """The inputs of the workflow the task's `task.yaml`
    names as it is saved now, a draft included, read at its version as the
    organiser, the way a save reads it. Needs the observer role at the task.
    A task that names no workflow, or one that cannot be read, is answered
    with the reason and nothing else, since the form is how it is mended.
    Either way it says whether the task has a graded submission.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    graded = bool(await sources._graded_under(ctx, task))
    return replace(await _workflow_form(ctx, organiser, task), graded=graded)


async def _workflow_form(ctx: Context, organiser: Organiser, task: TaskId) -> WorkflowForm:
    try:
        found = await ctx.forge.content.read_file(organiser.identity, task, TASK_FILE)
    except NotFound:
        return WorkflowForm(
            None, problem="The task's settings are missing. Add them and save again."
        )
    try:
        named = load_mapping(TASK_FILE, found.content).get("workflow")
    except InvalidDefinition as invalid:
        return WorkflowForm(None, problem=invalid.errors[0]["message"])
    if not isinstance(named, str):
        return WorkflowForm(None, problem="The task does not name a workflow yet. Choose one.")
    try:
        ref = parse_workflow_ref(named.strip())
    except InvalidName as invalid:
        return WorkflowForm(named, problem=invalid.detail)
    newer = await workflows.newer_version(ctx, organiser.identity, ref)
    opened = await _workflow(ctx, organiser.identity, ref)
    workflow = opened.found.definition if opened.found is not None else None
    problems = opened.problems
    if workflow is None:
        return WorkflowForm(
            str(ref), problem=problems[0]["message"] if problems else None, newer=newer
        )
    return WorkflowForm(
        str(ref),
        newer=newer,
        inputs=tuple(
            DeclaredInput(
                id=name,
                type=declared.type.value,
                options=declared.options,
                optional=declared.optional,
                title=declared.title,
                help=declared.help,
                unit=declared.unit,
            )
            for name, declared in workflow.inputs.items()
        ),
    )
