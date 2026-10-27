import re
from collections.abc import Mapping

from forge.domain.content import FileSet, WrittenEdit
from forge.domain.definitions import (
    STATEMENT_FILE,
    TASK_FILE,
    State,
    TaskDefinition,
    parse_task,
    statement_written,
)
from forge.domain.errors import ConfirmationRequired, Forbidden, NotFound
from forge.domain.grading import run_machine
from forge.domain.identity import Identity
from forge.domain.ids import TaskId
from forge.domain.publication_record import Publication
from forge.domain.run_plan import PROGRAM_USES, files_named, plan_runs
from forge.domain.run_plan.models import PinnedWorkflow
from forge.domain.run_task import check_run_task, manifest_files
from forge.domain.run_workflow import check_run_workflow
from forge.domain.save_effect import ReadChanges, as_extra
from forge.domain.workflow_definition import RunWorkflow
from forge.domain.yaml_models import InvalidDefinition, Problem, path_text
from forge.runtime.context import Context
from forge.services import gradings, names
from forge.services.task_save import sources
from forge.services.task_save.effect import _programs_again, _save_effect
from forge.services.task_save.guards import _leaderboard_reads, _present, _shown_before_runs
from forge.services.task_save.records import PIN_MOVED, Checked
from forge.services.task_save.sources import (
    _content,
    _Found,
    _moved_workflows,
    _primitives,
    _unopened,
    _workflow,
)
from forge.services.task_save.stored_facts import (
    RUNS_UNREAD,
    _task_facts,
    _workflow_facts,
    _workflow_problem,
)
from forge.services.task_save.writing import _contest, _contest_running
from forge.services.workflow_boxes import Expanded, expand, files_at

_INNER_BAD = re.compile(r"^`([^`]+)`: (.*)$", re.DOTALL)


def _cannot_run(
    definition: TaskDefinition, workflow: RunWorkflow, expanded: Expanded
) -> list[Problem]:
    """A `use:` expand could not read, in the words a task's check has
    always given: the workflow, the step, and what was wrong. A box that
    did open is not one of these, and neither is a primitive that did. The
    same `use:` is named once, on the first step that writes it.
    """
    opened = {pin.use for pin in expanded.pins}
    tried: set[str] = set()
    problems: list[Problem] = []
    for index, step in enumerate(workflow.steps):
        use = str(step.use)
        if use in tried:
            continue
        tried.add(use)
        if use in expanded.primitives or use in opened:
            continue
        at = path_text(("steps", index, "use"))
        said = next((problem for problem in expanded.problems if problem["path"] == at), None)
        if said is None:
            continue
        problems.append(
            Problem(
                path="workflow",
                message=(
                    f"In the workflow {definition.workflow}, the step {step.id} cannot run: "
                    f"{said['message']}"
                ),
            )
        )
    # A primitive inside a box is named on the flat path, which is how expand
    # writes that problem. The outer step itself did open.
    said_inner: set[str] = set()
    for problem in expanded.problems:
        match = _INNER_BAD.match(problem["message"])
        if match is None or match.group(1) in said_inner:
            continue
        said_inner.add(match.group(1))
        problems.append(
            Problem(
                path="workflow",
                message=(
                    f"In the workflow {definition.workflow}, the step {match.group(1)} "
                    f"cannot run: {match.group(2)}"
                ),
            )
        )
    return problems


def _checked_first(checked: Checked) -> bool:
    """Whether a save is checked by a save row before it takes over, and its
    submissions are graded again only then."""
    return checked.run_plan is not None and bool(checked.run_plan.pipeline("save"))


async def _ask(
    ctx: Context,
    task: TaskId,
    checked: Checked,
    latest: Publication | None,
    question: str | None,
    changed: tuple[str, ...],
    again: tuple[str, ...] | None,
    read: ReadChanges,
    basis: str,
    *,
    anew: bool,
    pinned: bool = False,
) -> None:
    """Refuse a save not yet confirmed with `ConfirmationRequired` when it
    asks first: while its contest runs, before it publishes anything new
    (#449); when a column the entrant could set is new or
    set through other inputs than under the latest publication (#504); and
    when it moves the workflow's pin, `pinned`, with submissions to grade
    again (11.8). One question asks all of them, `self_reported` listing
    each such column with the entrant's inputs that let them set it, and
    `asked` being what a confirmation answers it with, `basis`.
    """
    assert checked.definition is not None
    regrades = (
        await gradings.to_regrade(ctx, task, limit=checked.definition.submissions.max)
        if changed
        else 0
    )
    asks: list[str] = []
    if anew and await _contest_running(ctx, task):
        asks.append("The contest is running, so this save asks before it publishes.")
    if pinned and regrades:
        asks.append(PIN_MOVED)
    if question is not None:
        asks.append(question)
    if not asks:
        return
    effect = await _save_effect(
        ctx,
        task,
        checked.definition,
        latest,
        changed,
        regrades,
        read,
        steps=again,
        checked_first=_checked_first(checked),
        programs=_programs_again(checked, again),
    )
    marks = checked.run_task.self_reported if question and checked.run_task else {}
    raise ConfirmationRequired(
        " ".join([*asks, "Publish it, or keep it as a draft."]),
        changes=[*changed],
        regrades=regrades,
        **as_extra(effect),
        **(
            {"self_reported": {name: sorted(inputs) for name, inputs in sorted(marks.items())}}
            if marks
            else {}
        ),
        asked=basis,
    )


async def _statement_ready(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, written: Mapping[str, WrittenEdit]
) -> list[Problem]:
    contest = await _contest(ctx, task)
    if contest is None or contest.state is State.DRAFT:
        return []
    name = (await names.names_of(ctx, [task])).get(task)
    entry = contest.entry(name) if name is not None else None
    if entry is None or entry.withdrawn or contest.release_of(entry) >= contest.end:
        return []
    if statement_written(await _content(ctx, as_, task, STATEMENT_FILE, head, written)):
        return []
    return [
        Problem(
            path=STATEMENT_FILE,
            message=f"{contest.label_of(entry.id)} · {entry.id}: its statement is not written yet.",
        )
    ]


async def check(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, written: Mapping[str, WrittenEdit]
) -> Checked:
    """Check the state a save leaves, the files at `head` with `written` over
    them, reading what it needs as `as_`, in the order of TASK-FORMAT.md
    section 2. Every problem carries its path in `task.yaml`, or the folder
    or file it is about.
    """
    text = await _content(ctx, as_, task, TASK_FILE, head, written)
    if text is None:
        return Checked(
            None,
            (
                Problem(
                    path="", message="The task's settings are missing. Add them and save again."
                ),
            ),
        )
    try:
        definition = parse_task(text)
    except InvalidDefinition as invalid:
        return Checked(None, tuple(invalid.errors))
    opened = await _workflow(ctx, as_, definition.workflow)
    found, pins, problems = opened.found, opened.pins, opened.problems
    problems.extend(await _moved_workflows(ctx, task, definition, pins))
    if found is None or problems:
        return Checked(definition, tuple(problems), pins)
    return await _check_runs(
        ctx, as_, task, definition, found.definition, found, pins, head, written
    )


async def _check_runs(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    definition: TaskDefinition,
    workflow: RunWorkflow,
    read: _Found,
    pins: dict[str, str],
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> Checked:
    """The task's check (SIMPLER-CORE.md 8 and 9): its workflow checked
    again at the commit its `workflow.yaml` was `read` at, as making the
    version did, then
    the task against it (`check_run_task`), each manifest the task gives
    read from the saved state, the ids of its runs kept from the latest
    publication's runs file; then what was shown is not hidden again, and
    the contest's entry asks nothing the task cannot give;
    then its plan (`plan_runs`, SIMPLER-CORE.md 11), pinned to that commit,
    with the size and hash of every file a step is given.
    """
    expanded = await expand(ctx, as_, workflow, own=read.workflow)
    if expanded.flat is None:
        return Checked(
            definition,
            tuple(_workflow_problem(definition.workflow, problem) for problem in expanded.problems),
            pins,
        )
    unrun = _cannot_run(definition, workflow, expanded)
    if unrun:
        return Checked(definition, tuple(unrun), pins)
    flat = expanded.flat
    for pin in expanded.pins:
        pins[pin.use.rsplit("@", 1)[0]] = pin.key
    moved = await sources._moved_workflows(ctx, task, definition, pins)
    if moved:
        return Checked(definition, tuple(moved), pins)
    programs, unresolved = await _primitives(ctx, as_, definition)
    if unresolved:
        return Checked(definition, unresolved, pins)
    primitives = {**expanded.primitives, **programs}
    ref = definition.workflow
    try:
        files = await files_at(ctx, as_, read.workflow, read.at, workflow)
    except NotFound, Forbidden:
        return Checked(definition, (_unopened(ref),), pins)
    resolved = check_run_workflow(
        flat.workflow,
        primitives,
        files,
        inner_files=expanded.inner_files,
        boxes=flat.boxes,
        flat=bool(flat.boxes),
    )
    workflow = flat.workflow
    problems = [_workflow_problem(ref, problem) for problem in resolved.problems]
    if problems:
        return Checked(definition, tuple(problems), pins)
    present = _present(head, written)
    manifests = {
        path: content
        for path in sorted(manifest_files(definition, resolved) & present)
        if (content := await _content(ctx, as_, task, path, head, written)) is not None
    }
    # Run ids continue from the latest publication, taken over or not, so no
    # id a publication gave is ever given to another key.
    publications = await ctx.forge.workspaces.list_publications(task)
    previous = await sources._runs_file_of(ctx, task, publications[-1]) if publications else None
    if previous is None and publications:
        before = await sources._definition_of(ctx, task, publications[-1])
        if before is not None:
            # Numbering the runs afresh would give a key another run's id.
            return Checked(definition, (Problem(path="", message=RUNS_UNREAD),), pins)
    found = check_run_task(
        definition,
        workflow,
        resolved,
        present,
        manifests,
        previous=previous,
        submitted=await sources._submitted(ctx, task),
        secrets=frozenset(),
        machine=run_machine(ctx.settings.slot_sizes),
    )
    problems = [
        *found.problems,
        *await _statement_ready(ctx, as_, task, head, written),
        *await _shown_before_runs(ctx, task, definition, present, found.task),
        *await _leaderboard_reads(ctx, task, definition),
    ]
    if problems:
        return Checked(definition, tuple(problems), pins)
    assert found.task is not None, "check_run_task gives a task whenever it finds nothing wrong"
    named = files_named(workflow, found.task)
    inners: list[PinnedWorkflow] = []
    for index, pin in enumerate(expanded.pins, start=1):
        inners.append(
            PinnedWorkflow(
                ref=pin.use,
                id=str(pin.workflow),
                commit=str(pin.at),
                files=await _workflow_facts(
                    ctx,
                    as_,
                    pin.workflow,
                    pin.at,
                    expanded.inner_files[index],
                    named.inner.get(index, ()),
                ),
            )
        )
    planned = plan_runs(
        definition,
        workflow,
        resolved,
        found.task,
        ref=str(ref),
        workflow_id=str(read.workflow),
        commit=str(read.at),
        task_files=await _task_facts(ctx, as_, task, head, written, present, named.task),
        workflow_files=await _workflow_facts(
            ctx, as_, read.workflow, read.at, files, named.workflow
        ),
        inner_workflows=tuple(inners),
        machine=run_machine(ctx.settings.slot_sizes),
        programs={use: primitives[use] for use in PROGRAM_USES if use in primitives},
    )
    if planned.plan is None:
        return Checked(definition, tuple(planned.problems), pins)
    return Checked(definition, (), pins, found.task, planned.plan)
