from collections.abc import Mapping

from forge.domain.content import FileSet, WrittenEdit
from forge.domain.definitions import RUN_FORMAT_KEYS, TASK_FILE, TaskDefinition, parse_task
from forge.domain.errors import ConfirmationRequired, Forbidden, InvalidName, NotFound
from forge.domain.grading import PLATFORM_MACHINE, run_machine
from forge.domain.identity import Identity
from forge.domain.ids import TaskId
from forge.domain.plans import (
    check_workflow,
    compile_plan,
    group_problems,
    read_tests,
    test_yaml_paths,
)
from forge.domain.publication_record import Publication
from forge.domain.run_plan import PROGRAM_USES, files_named, plan_runs
from forge.domain.run_task import check_run_task, manifest_files
from forge.domain.run_workflow import check_run_workflow
from forge.domain.save_effect import ReadChanges, as_extra
from forge.domain.workflow_definition import RunWorkflow, parse_workflow_ref
from forge.domain.yaml_models import InvalidDefinition, Problem, load_mapping
from forge.runtime.context import Context
from forge.services import gradings
from forge.services.task_save import sources
from forge.services.task_save.effect import _programs_again, _save_effect
from forge.services.task_save.guards import (
    _leaderboard_reads,
    _present,
    _shown_before,
    _shown_before_runs,
)
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
    _format_kept,
    _task_facts,
    _workflow_facts,
    _workflow_problem,
)
from forge.services.task_save.writing import _contest_running
from forge.services.workflow_boxes import files_at


def _checked_first(checked: Checked) -> bool:
    """Whether a save of a run task is checked by a save row before it takes
    over, and its submissions are graded again only then."""
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
    (#449); for a run task, when a column the entrant could set is new or
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
            None,
            (
                Problem(
                    path="", message="The task's settings are missing. Add them and save again."
                ),
            ),
        )
    try:
        definition = await _parsed(ctx, as_, text)
    except InvalidDefinition as invalid:
        return Checked(None, None, tuple(invalid.errors))
    present = _present(head, written)
    opened = await _workflow(ctx, as_, definition.workflow)
    found, pins, problems = opened.found, opened.pins, opened.problems
    problems.extend(await _moved_workflows(ctx, task, definition, pins))
    if found is None or problems:
        return Checked(definition, None, tuple(problems), pins)
    workflow = found.definition
    runs = isinstance(workflow, RunWorkflow)
    if runs != definition.runs:
        # The workflow says the format, so the keys of the other are refused
        # naming it.
        try:
            definition = parse_task(text, runs=runs)
        except InvalidDefinition as invalid:
            return Checked(None, None, tuple(invalid.errors), pins)
    kept = await _format_kept(ctx, task, definition)
    if kept:
        return Checked(definition, None, tuple(kept), pins)
    if isinstance(workflow, RunWorkflow):
        return await _check_runs(ctx, as_, task, definition, workflow, found, pins, head, written)
    primitives, unresolved = await _primitives(ctx, as_, definition, workflow)
    if unresolved:
        return Checked(definition, None, unresolved, pins)
    version = [
        _workflow_problem(definition.workflow, problem)
        for problem in check_workflow(workflow, primitives)
    ]
    if version:
        return Checked(definition, None, tuple(version), pins)
    yamls = {
        path: content
        for path in test_yaml_paths(present)
        if (content := await _content(ctx, as_, task, path, head, written)) is not None
    }
    tests, problems = read_tests(present, workflow.test, yamls)
    problems.extend(group_problems(definition, tests, present))
    problems.extend(await _shown_before(ctx, task, definition))
    if problems:
        return Checked(definition, None, tuple(problems), pins)
    try:
        compiled = compile_plan(
            definition,
            workflow,
            primitives,
            present,
            tests,
            secrets=frozenset(),
            machine=PLATFORM_MACHINE,
            harness_image=ctx.settings.harness_image,
        )
    except InvalidDefinition as invalid:
        return Checked(definition, None, tuple(invalid.errors), pins)
    return Checked(definition, compiled, (), pins)


async def _parsed(ctx: Context, as_: Identity, text: bytes) -> TaskDefinition:
    """The `task.yaml` in `text`, read in the format its keys say, or when
    that does not read, in the format of the workflow it names: a task of
    today's format with a stray `scoring:` is then refused for what its
    workflow's format refuses, not for missing `test_groups`.
    `InvalidDefinition` with the problems of the format that applies, or of
    the keys' when the workflow cannot be read.
    """
    try:
        return parse_task(text)
    except InvalidDefinition as invalid:
        guessed = invalid
    try:
        document = load_mapping(TASK_FILE, text)
        named = document.get("workflow")
        ref = parse_workflow_ref(named.strip()) if isinstance(named, str) else None
    except InvalidDefinition, InvalidName:
        raise guessed from None
    found = (await _workflow(ctx, as_, ref)).found if ref is not None else None
    if found is None:
        raise guessed
    runs = isinstance(found.definition, RunWorkflow)
    if runs == any(key in document for key in RUN_FORMAT_KEYS):
        raise guessed
    return parse_task(text, runs=runs)


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
    """A run task's check (SIMPLER-CORE.md 8 and 9): its workflow checked
    again at the commit its `workflow.yaml` was `read` at, as making the
    version did, then
    the task against it (`check_run_task`), each manifest the task gives
    read from the saved state, the ids of its runs kept from the latest
    publication's runs file; then what was shown is not hidden again, and
    the contest's entry asks nothing a run task cannot give;
    then its plan (`plan_runs`, SIMPLER-CORE.md 11), pinned to that commit,
    with the size and hash of every file a step is given.
    """
    primitives, unresolved = await _primitives(ctx, as_, definition, workflow)
    if unresolved:
        return Checked(definition, None, unresolved, pins)
    ref = definition.workflow
    try:
        files = await files_at(ctx, as_, read.workflow, read.at, workflow)
    except NotFound, Forbidden:
        return Checked(definition, None, (_unopened(ref),), pins)
    resolved = check_run_workflow(workflow, primitives, files)
    problems = [_workflow_problem(ref, problem) for problem in resolved.problems]
    if problems:
        return Checked(definition, None, tuple(problems), pins)
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
        if before is not None and before.runs:
            # Numbering the runs afresh would give a key another run's id.
            return Checked(definition, None, (Problem(path="", message=RUNS_UNREAD),), pins)
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
        *await _shown_before_runs(ctx, task, definition, present, found.task),
        *await _leaderboard_reads(ctx, task, definition),
    ]
    if problems:
        return Checked(definition, None, tuple(problems), pins)
    assert found.task is not None, "check_run_task gives a task whenever it finds nothing wrong"
    named = files_named(workflow, found.task)
    planned = plan_runs(
        definition,
        workflow,
        resolved,
        found.task,
        ref=str(ref),
        workflow_id=str(read.workflow),
        commit=str(read.at),
        task_files=await _task_facts(ctx, as_, task, head, written, present, named.task),
        workflow_files=await _workflow_facts(ctx, as_, read, files, named.workflow),
        machine=run_machine(ctx.settings.slot_sizes),
        programs={use: primitives[use] for use in PROGRAM_USES if use in primitives},
    )
    if planned.plan is None:
        return Checked(definition, None, tuple(planned.problems), pins)
    return Checked(definition, None, (), pins, found.task, planned.plan)
