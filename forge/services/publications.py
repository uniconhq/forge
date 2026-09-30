"""The save of a task, which is also how it is published. There is no publish
button: every save of a task, from a form or from the file editor, runs these
steps in order, and refuses before anything is written.

1. A save that writes inside `plans/`, where only the compiler writes, is
   refused with `ReservedPath`. A manager's save that changes an admin-only
   key of `task.yaml` or touches `statement.md` is refused with `AdminOnly`,
   naming each.
2. The state being saved, the files at the head with the save's files over
   them, is checked: `task.yaml` validates, every file it names is there,
   every workflow it names and every primitive their steps use is read at
   its version as the organiser saving, and one plan per stage compiles over
   the files of that state. A state that fails is kept as a draft:
   the organiser's files are written as one change, nothing is published,
   and the last publication keeps grading. The errors are not stored;
   `check` works them out again whenever the task's state is asked for.
3. What the save changes about how the task grades is worked out against
   the latest publication: its plans, the data files they name and the
   limits. A save asked to be kept as a draft is written as one here, saying
   what it held back, and publishes nothing, whatever else holds. While the
   contest runs, a save that changes any of them is refused with
   `ConfirmationRequired`, listing the changes, unless the caller confirms
   it.
4. The organiser's files and every `plans/<stage>.json` are written as one
   change, as the organiser, so the history is theirs and a publication
   never catches a task half saved.
5. That change is named as the next publication, as the platform, with a
   note saying whether it changed how the task grades and what. When another
   save landed between the check and the write, the change holds files this
   save never checked, so it is kept as a draft instead, saying so, and the
   next save checks and publishes the task as it then stands.
6. The task is activated at the CI as the org's account, once
   (`activations`): at its first publication, or at the next one when the
   record of it was lost. An activation that fails does not undo the
   publication, and the result says it is pending.
7. Every approved contestant of the contest who has no place to submit the
   task yet is given one (`workspaces`), by the poller, so a task published
   after its contestants were approved reaches them too.
"""

import builtins
import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from forge.domain.content import ConflictToken, Edit, FileSet, check_path, has_path
from forge.domain.definitions import (
    ADMIN_ONLY_FILES,
    CONTEST_FILE,
    TASK_FILE,
    TaskDefinition,
    admin_only_changes,
    parse_contest,
    parse_task,
)
from forge.domain.errors import (
    AdminOnly,
    ConfirmationRequired,
    Forbidden,
    NotFound,
    ReservedPath,
)
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import PublicationId, TaskId, VersionId
from forge.domain.plans import (
    Plan,
    Snapshot,
    compile_plans,
    grading_changes,
    is_reserved,
    plan_path,
)
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.publications import Publication, write_note
from forge.domain.release import is_running
from forge.domain.roles import (
    Role,
    contest_id_of,
    holds,
    primitive_id_of,
    task_scope,
    workflow_id_of,
)
from forge.domain.workflow_definition import WorkflowDefinition, WorkflowRef, parse_workflow
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import activations, workspaces
from forge.services.access import Organiser, require
from forge.services.activations import Activation

log = get_logger(__name__)

WORKFLOW_FILE = "workflow.yaml"
SAVE_MESSAGE = "Save"
CHANGED_CONTENT = "new:"
LANDED_UNDER = (
    "Another save landed while this one was written, so the task as it now stands "
    "was not checked; save again to publish it."
)


@dataclass(frozen=True, slots=True)
class Published:
    """A save that published: the publication and its number, whether it
    changed how the task grades and what, and where the task's activation at
    the CI stands: done by this save, pending with the poller, or not needed
    because an earlier publication did it.
    """

    publication: PublicationId
    number: int
    grading_changed: bool
    changes: tuple[str, ...]
    activation: Activation


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
    """What checking a state found: the task's settings and one plan per
    stage when it is valid, and every problem when it is not.
    """

    definition: TaskDefinition | None
    plans: Mapping[str, Plan]
    errors: tuple[Problem, ...]


@action
async def save(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    changes: Mapping[str, Edit],
    *,
    confirm: bool = False,
    keep_as_draft: bool = False,
    message: str | None = None,
) -> Published | Draft:
    """Save the task's files, each path with its new content and the token
    it was read with, and publish the result when it is valid. Needs the
    manager role at the task. `keep_as_draft` writes the files and publishes
    nothing, on any save; otherwise `confirm` publishes a change to how the
    task grades while its contest runs. A token that has moved is
    `Conflict`, with nothing written.
    """
    scope = task_scope(task)
    require(organiser, scope, Role.MANAGER)
    _refuse_reserved(changes)
    as_ = organiser.identity
    head = await ctx.forge.content.list_files(as_, task)
    if not holds(organiser.grants, scope, Role.ADMIN):
        await _refuse_admin_only(ctx, organiser, task, head, changes)
    said = message or SAVE_MESSAGE

    checked = await check(ctx, as_, task, head, changes)
    if checked.errors:
        version = await _write(ctx, as_, task, head, changes, {}, said)
        return _draft(organiser, task, version, checked.errors, ())

    publications = await ctx.forge.workspaces.list_publications(task)
    latest = publications[-1] if publications else None
    changed: tuple[str, ...] = ()
    if latest is not None:
        before, published_files = await _published_snapshot(ctx, as_, task, latest)
        after = await _saved_snapshot(ctx, as_, task, head, changes, checked, published_files)
        changed = grading_changes(before, after)
    if keep_as_draft:
        version = await _write(ctx, as_, task, head, changes, {}, said)
        return _draft(organiser, task, version, (), changed)
    if changed and not confirm and await _contest_running(ctx, task):
        raise ConfirmationRequired(
            "The contest is running and this save changes how the task grades. Send it "
            "again confirmed to publish it, or keep it as a draft.",
            changes=[*changed],
        )

    plans = await _plan_files(ctx, as_, task, head, checked)
    version = await _write(ctx, as_, task, head, changes, plans, said)
    written = {*changes, *plans}
    if version != head.version and await _landed_under(ctx, as_, task, head, version, written):
        return _draft(organiser, task, version, (Problem(path="", message=LANDED_UNDER),), ())
    if latest is not None and version == latest.version:
        return await _reached(
            ctx, task, latest.id, latest.number, latest.grading_changed, latest.changes
        )
    publication = await ctx.forge.workspaces.publish(
        task, version, write_note(bool(changed), changed)
    )
    number = await _number_of(ctx, task, publication)
    log.info(
        "publications.published",
        task=task,
        publication=publication,
        number=number,
        version=version,
        grading_changed=bool(changed),
        user_id=organiser.user.id,
    )
    return await _reached(ctx, task, publication, number, bool(changed), changed)


async def _reached(
    ctx: Context,
    task: TaskId,
    publication: PublicationId,
    number: int,
    grading_changed: bool,
    changes: tuple[str, ...],
) -> Published:
    """The latest publication, once what every publication leads to is made
    sure of: the task's activation at the CI, and a place to submit it for
    every approved contestant.
    """
    activation = await activations.ensure(ctx, task)
    await workspaces.place_for_everyone(ctx, task)
    return Published(publication, number, grading_changed, changes, activation)


async def check(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, changes: Mapping[str, Edit]
) -> Checked:
    """Check the state a save leaves, the files at `head` with `changes` over
    them, reading what it needs as `as_`: `task.yaml` validates, every file
    it names is in the state, every workflow it names and every primitive
    their steps use is read at its version, and the plans compile over the
    files of the state. Every problem carries its YAML path.
    """
    text = await _content(ctx, as_, task, TASK_FILE, head, changes)
    if text is None:
        return Checked(None, {}, (Problem(path="", message="The task has no task.yaml."),))
    try:
        definition = parse_task(text)
    except InvalidDefinition as invalid:
        return Checked(None, {}, tuple(invalid.errors))
    present = {*head.tokens, *changes}
    problems = definition.missing_files(lambda path: has_path(present, path))
    workflows, unreadable = await _workflows(ctx, as_, definition)
    problems.extend(unreadable)
    if not unreadable:
        primitives, unresolved = await _primitives(ctx, as_, definition, workflows)
        problems.extend(unresolved)
    if not unreadable and not unresolved:
        try:
            plans = compile_plans(
                definition,
                workflows,
                primitives,
                present,
                harness_image=ctx.settings.harness_image,
            )
        except InvalidDefinition as invalid:
            reported = {problem["path"] for problem in problems}
            problems.extend(error for error in invalid.errors if error["path"] not in reported)
        else:
            if not problems:
                return Checked(definition, plans, ())
    return Checked(definition, {}, tuple(problems))


async def _refuse_admin_only(
    ctx: Context, organiser: Organiser, task: TaskId, head: FileSet, changes: Mapping[str, Edit]
) -> None:
    as_ = organiser.identity
    keys = (
        admin_only_changes(
            "task",
            await _content(ctx, as_, task, TASK_FILE, head, {}),
            changes[TASK_FILE].content,
        )
        if TASK_FILE in changes
        else []
    )
    for path in ADMIN_ONLY_FILES:
        if path in changes and changes[path].content != await _content(
            ctx, as_, task, path, head, {}
        ):
            keys.append(path)
    if keys:
        log.info("publications.refused", task=task, keys=keys, user_id=organiser.user.id)
        raise AdminOnly(
            f"Only an admin of {task_scope(task).name} may change {', '.join(keys)}.", keys=keys
        )


def _refuse_reserved(changes: Mapping[str, Edit]) -> None:
    for path in changes:
        check_path(path)
    reserved = sorted(path for path in changes if is_reserved(path))
    if reserved:
        raise ReservedPath(
            "Only the compiler writes inside plans/; save the task's other files.",
            paths=reserved,
        )


async def _content(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    path: str,
    head: FileSet,
    changes: Mapping[str, Edit],
) -> bytes | None:
    """A file's content in the state: the save's when it writes the file,
    otherwise the head's, or none when neither has it.
    """
    if path in changes:
        return changes[path].content
    if path not in head.tokens:
        return None
    return (await ctx.forge.content.read_file(as_, task, path, at=head.version)).content


async def _workflows(
    ctx: Context, as_: Identity, definition: TaskDefinition
) -> tuple[dict[str, WorkflowDefinition], tuple[Problem, ...]]:
    """Every workflow the task needs, read at its version as `as_`, and a
    problem for each that cannot be read or does not validate.
    """
    found: dict[str, WorkflowDefinition] = {}
    problems: tuple[Problem, ...] = ()
    for ref, at in definition.workflow_refs():
        try:
            file = await ctx.forge.workflows.read_workflow_file(
                as_, workflow_id_of(ref), ref.version, WORKFLOW_FILE
            )
        except NotFound, Forbidden:
            message = (
                f"The workflow {ref} cannot be read: it is not there at that version, "
                "or it is not shared with you."
            )
            problems = (*problems, Problem(path=at, message=message))
            continue
        try:
            found[str(ref)] = parse_workflow(file.content)
        except InvalidDefinition as invalid:
            message = f"The workflow {ref} is not valid: {invalid.detail}"
            problems = (*problems, Problem(path=at, message=message))
    return found, problems


async def _primitives(
    ctx: Context,
    as_: Identity,
    definition: TaskDefinition,
    workflows: Mapping[str, WorkflowDefinition],
) -> tuple[dict[str, PrimitiveDeclaration], tuple[Problem, ...]]:
    """The declaration of every primitive a step of the task's workflows
    uses, read at its version as `as_`, and a problem at the workflow's path
    for each `use:` that cannot be read, is not a primitive, or does not
    validate.
    """
    found: dict[str, PrimitiveDeclaration] = {}
    problems: builtins.list[Problem] = []
    tried: set[str] = set()
    for ref, at in definition.workflow_refs():
        for index, step in enumerate(workflows[str(ref)].steps):
            use = str(step.use)
            if use in tried:
                continue
            tried.add(use)
            where = f"In {ref}, steps[{index}].use: "
            declaration, problem = await _primitive(ctx, as_, step.use)
            if problem is not None:
                problems.append(Problem(path=at, message=where + problem))
            elif declaration is not None:
                found[use] = declaration
    return found, tuple(problems)


async def _primitive(
    ctx: Context, as_: Identity, use: WorkflowRef
) -> tuple[PrimitiveDeclaration | None, str | None]:
    """The declaration a `use:` names, or what is wrong with it. A `use:` that
    names a workflow the organiser can read is refused as not supported yet,
    and one they cannot read is refused naming it, the same whether it is not
    there or not shared with them.
    """
    primitive = primitive_id_of(use)
    if primitive is not None:
        try:
            text = await ctx.forge.primitives.read_declaration(as_, primitive, use.version)
        except NotFound, Forbidden:
            pass
        else:
            try:
                declaration = parse_primitive(text)
            except InvalidDefinition as invalid:
                return None, f"The primitive {use} is not valid: {invalid.detail}"
            if (declaration.name, declaration.version) != (f"{use.owner}/{use.name}", use.version):
                return (
                    None,
                    f"The primitive {use} declares itself as "
                    f"{declaration.name}@{declaration.version}.",
                )
            return declaration, None
    try:
        await ctx.forge.workflows.read_workflow_file(
            as_, workflow_id_of(use), use.version, WORKFLOW_FILE
        )
    except NotFound, Forbidden:
        return None, (
            f"{use} cannot be read: there is no such primitive or workflow at that version, "
            "or it is not shared with you."
        )
    return None, f"{use} is a workflow; using a workflow as a step comes with feature 10."


async def _published_snapshot(
    ctx: Context, as_: Identity, task: TaskId, latest: Publication
) -> tuple[Snapshot, FileSet]:
    """What the latest publication's grading depends on, and its files."""
    files = await ctx.forge.content.list_files(as_, task, at=latest.version)
    definition: TaskDefinition | None = None
    if TASK_FILE in files.tokens:
        text = (await ctx.forge.content.read_file(as_, task, TASK_FILE, at=latest.version)).content
        try:
            definition = parse_task(text)
        except InvalidDefinition:
            definition = None
    plans = {
        path: (await ctx.forge.content.read_file(as_, task, path, at=latest.version)).content
        for path in sorted(files.tokens)
        if is_reserved(path)
    }
    named = definition.named_files() if definition is not None else ()
    data = {path: str(token) for folder in named for path, token in files.under(folder).items()}
    limits = definition.limits.as_mapping() if definition is not None else {}
    return Snapshot(plans=plans, data=data, limits=limits), files


async def _saved_snapshot(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    changes: Mapping[str, Edit],
    checked: Checked,
    published: FileSet,
) -> Snapshot:
    """What the saved state's grading would depend on. A data file the save
    does not write compares by its token at the head; one it writes compares
    by the token it had at the publication when its content is the same, and
    otherwise by a digest of its content that no token equals.
    """
    assert checked.definition is not None
    data: dict[str, str] = {}
    for named in checked.definition.named_files():
        for path, token in head.under(named).items():
            if path not in changes:
                data[path] = str(token)
        for path, edit in changes.items():
            if has_path((path,), named):
                data[path] = await _digest(ctx, as_, task, path, edit.content, published)
    return Snapshot(
        plans={plan_path(stage): plan.to_bytes() for stage, plan in checked.plans.items()},
        data=data,
        limits=checked.definition.limits.as_mapping(),
    )


async def _digest(
    ctx: Context, as_: Identity, task: TaskId, path: str, content: bytes, published: FileSet
) -> str:
    if path in published.tokens:
        before = await ctx.forge.content.read_file(as_, task, path, at=published.version)
        if before.content == content:
            return str(before.token)
    return f"{CHANGED_CONTENT}{hashlib.sha256(content).hexdigest()}"


async def _contest_running(ctx: Context, task: TaskId) -> bool:
    """Whether the task's contest is running, read as the platform, since an
    organiser of the task alone may not read the contest.
    """
    contest = contest_id_of(task_scope(task))
    try:
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
        return is_running(parse_contest(found.content), ctx.now)
    except NotFound, InvalidDefinition:
        return False


async def _plan_files(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, checked: Checked
) -> dict[str, bytes | None]:
    """The plans the save writes: each one that is new or differs from the
    head's, and none for each plan at the head of a stage the task no longer
    has, which the save removes.
    """
    compiled = {plan_path(stage): plan.to_bytes() for stage, plan in checked.plans.items()}
    files: dict[str, bytes | None] = {}
    for path, content in compiled.items():
        if await _content(ctx, as_, task, path, head, {}) != content:
            files[path] = content
    for path in head.tokens:
        if is_reserved(path) and path not in compiled:
            files[path] = None
    return files


async def _write(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    changes: Mapping[str, Edit],
    plans: Mapping[str, bytes | None],
    message: str,
) -> VersionId:
    """Write the organiser's files and the plans as one change as `as_`, and
    return its version; with nothing to write, the head is the version.
    """
    files: dict[str, bytes | None] = {path: edit.content for path, edit in changes.items()}
    files.update(plans)
    if not files:
        return head.version
    expected: dict[str, ConflictToken | None] = {path: head.tokens.get(path) for path in plans}
    expected.update({path: edit.token for path, edit in changes.items()})
    return await ctx.forge.content.save_files(as_, task, files, expected=expected, message=message)


async def _landed_under(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    version: VersionId,
    written: Collection[str],
) -> bool:
    """Whether the change at `version` sits on something other than `head`:
    a file this save did not write differs from the head it checked, so
    another save landed between the check and the write.
    """
    after = await ctx.forge.content.list_files(as_, task, at=version)
    return any(
        head.tokens.get(path) != after.tokens.get(path)
        for path in head.tokens.keys() | after.tokens.keys()
        if path not in written
    )


def _draft(
    organiser: Organiser,
    task: TaskId,
    version: VersionId,
    errors: tuple[Problem, ...],
    held_back: tuple[str, ...],
) -> Draft:
    log.info(
        "publications.draft",
        task=task,
        version=version,
        errors=len(errors),
        held_back=len(held_back),
        user_id=organiser.user.id,
    )
    return Draft(version, errors, held_back)


async def _number_of(ctx: Context, task: TaskId, publication: PublicationId) -> int:
    for found in await ctx.forge.workspaces.list_publications(task):
        if found.id == publication:
            return found.number
    raise NotFound(f"the publication {publication} of {task} is not listed")


@action
async def list(ctx: Context, organiser: Organiser, task: TaskId) -> tuple[Publication, ...]:
    """Every publication of the task, oldest first, each saying whether it
    changed how the task grades. Needs the observer role at the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    return await ctx.forge.workspaces.list_publications(task)
