import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import exists, select

from forge.db.tables import Grading
from forge.db.tables.gradings import FRONT
from forge.domain.blocks.catalogue import EXEC
from forge.domain.blocks.evaluate import blocks_in
from forge.domain.content import ConflictToken, FileSet, WrittenEdit, has_path
from forge.domain.definitions import TASK_FILE, Group, TaskDefinition, parse_task
from forge.domain.errors import Forbidden, NotFound
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import PublicationId, TaskId, VersionId, WorkflowId
from forge.domain.plan_basics import PLAN_PATH, Snapshot
from forge.domain.plans import Plan
from forge.domain.port_values import is_reserved
from forge.domain.primitives import PrimitiveDeclaration
from forge.domain.publication_record import Publication
from forge.domain.run_plan import PROGRAM_USES, RunPlan
from forge.domain.run_task import RUNS_PATH, RunsFile
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    RunWorkflow,
    WorkflowDefinition,
    WorkflowRef,
    parse_workflow_ref,
    read_workflow,
)
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.runtime.context import Context
from forge.services import entrant_view, names, primitives
from forge.services.task_save.records import CHANGED_CONTENT, GROUPS_KEPT, Checked


def _unopened(ref: WorkflowRef) -> Problem:
    return Problem(
        path="workflow",
        message=f"Unicon cannot open the workflow {ref}: there is no such version, or it is not "
        "shared with you. Check its name and version, or ask its owner to share it.",
    )


async def _runs_file_of(ctx: Context, task: TaskId, publication: Publication) -> RunsFile | None:
    """The publication's runs file, or none when it has none that reads."""
    try:
        found = await ctx.forge.content.read_file(PLATFORM, task, RUNS_PATH, at=publication.version)
    except NotFound:
        return None
    return _runs_file(found.content)


def _runs_file(content: bytes | None) -> RunsFile | None:
    if content is None:
        return None
    try:
        return RunsFile.from_bytes(content)
    except ValueError:
        return None


async def _submitted(ctx: Context, task: TaskId) -> bool:
    """Whether anyone has submitted to the task."""
    return bool(await ctx.db.scalar(select(exists().where(Grading.task_id == task, FRONT))))


async def _graded_under(ctx: Context, task: TaskId) -> set[str]:
    """The publications a graded submission of the task ran under: once
    there is one, T7 and T10 hold.
    """
    return set(
        (
            await ctx.db.scalars(
                select(Grading.publication_id)
                .where(Grading.task_id == task, Grading.status == GradingStatus.DONE, FRONT)
                .distinct()
            )
        ).all()
    )


async def _groups_of(ctx: Context, task: TaskId, publication: Publication) -> Mapping[str, Group]:
    """The test groups of the publication's `task.yaml`, or none when it
    does not read, which a save never publishes.
    """
    found = await _definition_of(ctx, task, publication)
    return found.test_groups if found is not None else {}


async def _definition_of(
    ctx: Context, task: TaskId, publication: Publication
) -> TaskDefinition | None:
    """The publication's `task.yaml`, read once per process, or none when
    it does not read.
    """

    async def read() -> TaskDefinition | None:
        try:
            found = await ctx.forge.content.read_file(
                PLATFORM, task, TASK_FILE, at=publication.version
            )
            return parse_task(found.content)
        except NotFound, InvalidDefinition:
            return None

    key = f"publications.definition.{task}.{publication.version}"
    return await ctx.memo.remembered(key, GROUPS_KEPT, read)


async def _content(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    path: str,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> bytes | None:
    """A file's content in the state: the save's when it writes the file,
    otherwise the head's, or none when neither has it or the save removes
    it.
    """
    if path in written:
        return written[path].content
    if path not in head.tokens:
        return None
    return (await ctx.forge.content.read_file(as_, task, path, at=head.version)).content


@dataclass(frozen=True, slots=True)
class _Found:
    """A workflow as read: with or without a test block, the forge's id for
    it, and the commit its version stood at when it was read, which every
    later read of its files in the same check is pinned to.
    """

    definition: WorkflowDefinition | RunWorkflow
    workflow: WorkflowId
    at: VersionId


@dataclass(frozen=True, slots=True)
class _Opened:
    """The workflow a task names, none when it cannot be read or is in an
    old format, with the problem saying so, and which workflow its name is,
    by the forge's own id for it.
    """

    found: _Found | None
    pins: dict[str, str]
    problems: list[Problem]


async def _workflow(ctx: Context, as_: Identity, ref: WorkflowRef) -> _Opened:
    """The workflow `ref` names, its version resolved once to a commit and
    `workflow.yaml` read there as `as_`.
    """
    workflow = await names.workflow_id(ctx, ref)
    try:
        at = await ctx.forge.workflows.workflow_version(as_, workflow, ref.version)
        file = await ctx.forge.workflows.read_workflow_at(as_, workflow, at, WORKFLOW_FILE)
        pins = {f"{ref.owner}/{ref.name}": await ctx.forge.workflows.workflow_key(workflow)}
    except NotFound, Forbidden:
        return _Opened(None, {}, [_unopened(ref)])
    try:
        definition = read_workflow(file.content)
    except InvalidDefinition as invalid:
        message = (
            f"The workflow {ref} is written in an older form that Unicon no longer reads, so "
            f"its owner needs to make a new version of it. {invalid.detail}"
        )
        return _Opened(None, pins, [Problem(path="workflow", message=message)])
    return _Opened(_Found(definition, workflow, at), pins, [])


async def _moved_workflows(
    ctx: Context, task: TaskId, definition: TaskDefinition, pins: Mapping[str, str]
) -> list[Problem]:
    """A problem at each workflow name the publication the task is graded on
    used for another workflow than the one it names now.
    """
    publications = await entrant_view.live(ctx, task)
    pinned = publications[-1].workflows if publications else {}
    ref = definition.workflow
    name = f"{ref.owner}/{ref.name}"
    if name in pinned and name in pins and pins[name] != pinned[name]:
        message = (
            f"{name} is now a different workflow from the one this task was published "
            "with, as happens when its owner is renamed and someone else takes the name. "
            "Give the workflow you mean under its owner's current name."
        )
        return [Problem(path="workflow", message=message)]
    return []


async def _primitives(
    ctx: Context,
    as_: Identity,
    definition: TaskDefinition,
    workflow: WorkflowDefinition | RunWorkflow,
) -> tuple[dict[str, PrimitiveDeclaration], tuple[Problem, ...]]:
    """The declaration of every primitive a step of the task's workflow
    uses, read at its version as `as_`, and those a value's `exec` program
    is compiled and run with (`PROGRAM_USES`) when a value has one, and a
    problem at the `workflow` line for each `use:` that cannot be read, is
    not a primitive, or does not validate, or at `scoring` for one of those.
    """
    found: dict[str, PrimitiveDeclaration] = {}
    problems: list[Problem] = []
    tried: set[str] = set()
    for step in workflow.steps:
        use = str(step.use)
        if use in tried:
            continue
        tried.add(use)
        where = f"In the workflow {definition.workflow}, the step {step.id} cannot run: "
        declaration, problem = await primitives.declaration(ctx, as_, step.use)
        if problem is not None:
            problems.append(Problem(path="workflow", message=where + problem))
        elif declaration is not None:
            found[use] = declaration
    values = definition.scoring.values if definition.scoring is not None else {}
    if any(blocks_in(tree, EXEC) for tree in values.values()):
        for use in PROGRAM_USES:
            if use in found or use in tried:
                continue
            declaration, problem = await primitives.declaration(ctx, as_, parse_workflow_ref(use))
            if problem is not None:
                problems.append(
                    Problem(
                        path="scoring",
                        message=f"A value's exec program cannot run: {problem}",
                    )
                )
            elif declaration is not None:
                found[use] = declaration
    return found, tuple(problems)


async def _published_snapshot(
    ctx: Context, as_: Identity, task: TaskId, latest: Publication
) -> tuple[Snapshot, FileSet]:
    """What the latest publication's grading depends on, and its files."""
    files = await ctx.forge.content.list_files(as_, task, at=latest.version)
    plans = {
        path: (await ctx.forge.content.read_file(as_, task, path, at=latest.version)).content
        for path in sorted(files.tokens)
        if is_reserved(path)
    }
    named: tuple[str, ...] = ()
    if PLAN_PATH in plans:
        reader = RunPlan if RUNS_PATH in plans else Plan
        try:
            named = reader.from_bytes(plans[PLAN_PATH]).task_paths()
        except ValueError:
            named = ()
    data = {path: str(token) for each in named for path, token in _under(files, each).items()}
    return Snapshot(plans=plans, data=data), files


def _under(files: FileSet, path: str) -> Mapping[str, ConflictToken]:
    """The files `path` names in `files`: the one file, or every file under a
    folder.
    """
    if path.endswith("/"):
        return files.under(path)
    return {path: files.tokens[path]} if path in files.tokens else {}


async def _saved_snapshot(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    checked: Checked,
    published: FileSet,
) -> Snapshot:
    """What the saved state's grading would depend on. A data file the save
    does not write compares by its token at the head; one it writes compares
    by the token it had at the publication when its content is the same, and
    otherwise by a digest of its content that no token equals; one it
    removes is not there, so the comparison names it removed.
    """
    plan = checked.run_plan or (checked.compiled.plan if checked.compiled is not None else None)
    assert plan is not None
    data: dict[str, str] = {}
    for named in plan.task_paths():
        for path, token in _under(head, named).items():
            if path not in written:
                data[path] = str(token)
        for path, edit in written.items():
            if edit.content is not None and has_path((path,), named):
                data[path] = await _digest(ctx, as_, task, path, edit.content, published)
    return Snapshot(plans={PLAN_PATH: plan.to_bytes()}, data=data)


async def _digest(
    ctx: Context, as_: Identity, task: TaskId, path: str, content: bytes, published: FileSet
) -> str:
    if path in published.tokens:
        before = await ctx.forge.content.read_file(as_, task, path, at=published.version)
        if before.content == content:
            return str(before.token)
    return f"{CHANGED_CONTENT}{hashlib.sha256(content).hexdigest()}"


async def _number_of(ctx: Context, task: TaskId, publication: PublicationId) -> int:
    for found in await ctx.forge.workspaces.list_publications(task):
        if found.id == publication:
            return found.number
    raise NotFound(f"the publication {publication} of {task} is not listed")
