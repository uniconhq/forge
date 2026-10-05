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
   the files of that state. A workflow is named `<owner>/<name>`, the owner
   an org by its name or a person by their username; when the latest
   publication used a workflow of that name and it is now another workflow,
   by the forge's own id for it, the state is refused at that line, since
   the owner may have been renamed and the name taken by someone else. A
   state that fails is kept as a draft: the organiser's files are written
   as one change, nothing is published,
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
   note saying whether it changed how the task grades and what, and which
   workflow each workflow name was. When another
   save landed between the check and the write, the change holds files this
   save never checked, so it is kept as a draft instead, saying so, and the
   next save checks and publishes the task as it then stands.
6. A task's first publication starts making its place to submit for every
   approved contestant, once the save has committed and without the save
   waiting for it (`places`).
"""

import builtins
import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from forge.domain.content import (
    ConflictToken,
    Edit,
    FileSet,
    Uploaded,
    WrittenEdit,
    check_path,
    has_path,
)
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
    InvalidInputs,
    NotFound,
    ReservedPath,
    UploadNotReady,
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
)
from forge.domain.uploads import (
    ATTRIBUTES_FILE,
    UploadStatus,
    pointer_text,
    read_pointer,
    refuse_pointer,
)
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    WorkflowDefinition,
    WorkflowRef,
    parse_workflow,
)
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.port.uploads import TaskPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, places, uploads
from forge.services.access import Organiser, require

log = get_logger(__name__)

SAVE_MESSAGE = "Save"
CHANGED_CONTENT = "new:"
LANDED_UNDER = (
    "Another save landed while this one was written, so the task as it now stands "
    "was not checked; save again to publish it."
)


@dataclass(frozen=True, slots=True)
class Published:
    """A save that published: the publication and its number, and whether it
    changed how the task grades and what.
    """

    publication: PublicationId
    number: int
    grading_changed: bool
    changes: tuple[str, ...]


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
    stage when it is valid, and every problem when it is not, with which
    workflow each workflow name it uses is, by the forge's own id for it.
    """

    definition: TaskDefinition | None
    plans: Mapping[str, Plan]
    errors: tuple[Problem, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)


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
    written = await _resolve(ctx, organiser, task, changes)
    head = await ctx.forge.content.list_files(as_, task)
    if not holds(organiser.grants, scope, Role.ADMIN):
        await _refuse_admin_only(ctx, organiser, task, head, written)
    said = message or SAVE_MESSAGE

    checked = await check(ctx, as_, task, head, written)
    if checked.errors:
        version = await _write(ctx, as_, task, head, written, {}, said)
        return _draft(organiser, task, version, checked.errors, ())

    publications = await ctx.forge.workspaces.list_publications(task)
    latest = publications[-1] if publications else None
    changed: tuple[str, ...] = ()
    if latest is not None:
        before, published_files = await _published_snapshot(ctx, as_, task, latest)
        after = await _saved_snapshot(ctx, as_, task, head, written, checked, published_files)
        changed = grading_changes(before, after)
    if keep_as_draft:
        version = await _write(ctx, as_, task, head, written, {}, said)
        return _draft(organiser, task, version, (), changed)
    if changed and not confirm and await _contest_running(ctx, task):
        raise ConfirmationRequired(
            "The contest is running and this save changes how the task grades. Send it "
            "again confirmed to publish it, or keep it as a draft.",
            changes=[*changed],
        )

    plans = await _plan_files(ctx, as_, task, head, checked)
    version = await _write(ctx, as_, task, head, written, plans, said)
    touched = {*written, *plans}
    if version != head.version and await _landed_under(ctx, as_, task, head, version, touched):
        return _draft(organiser, task, version, (Problem(path="", message=LANDED_UNDER),), ())
    if latest is not None and version == latest.version:
        return Published(latest.id, latest.number, latest.grading_changed, latest.changes)
    publication = await ctx.forge.workspaces.publish(
        task, version, write_note(bool(changed), changed, checked.workflows)
    )
    number = await _number_of(ctx, task, publication)
    if latest is None:
        places.ahead_at(ctx, task)
    log.info(
        "publications.published",
        task=task,
        publication=publication,
        number=number,
        version=version,
        grading_changed=bool(changed),
        user_id=organiser.user.id,
    )
    return Published(publication, number, bool(changed), changed)


async def check(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, written: Mapping[str, WrittenEdit]
) -> Checked:
    """Check the state a save leaves, the files at `head` with `written` over
    them, reading what it needs as `as_`: `task.yaml` validates, every file
    it names is in the state, every workflow it names and every primitive
    their steps use is read at its version, and the plans compile over the
    files of the state. Every problem carries its YAML path.
    """
    text = await _content(ctx, as_, task, TASK_FILE, head, written)
    if text is None:
        return Checked(None, {}, (Problem(path="", message="The task has no task.yaml."),))
    try:
        definition = parse_task(text)
    except InvalidDefinition as invalid:
        return Checked(None, {}, tuple(invalid.errors))
    present = {*head.tokens, *written}
    problems = definition.missing_files(lambda path: has_path(present, path))
    problems.extend(definition.oversized())
    workflows, pins, unreadable = await _workflows(ctx, as_, definition)
    problems.extend(unreadable)
    problems.extend(await _moved_workflows(ctx, task, definition, pins))
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
                return Checked(definition, plans, (), pins)
    return Checked(definition, {}, tuple(problems), pins)


async def _refuse_admin_only(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> None:
    as_ = organiser.identity
    keys = (
        admin_only_changes(
            "task",
            await _content(ctx, as_, task, TASK_FILE, head, {}),
            written[TASK_FILE].content,
        )
        if TASK_FILE in written
        else []
    )
    for path in ADMIN_ONLY_FILES:
        if path in written and written[path].content != await _content(
            ctx, as_, task, path, head, {}
        ):
            keys.append(path)
    if keys:
        log.info("publications.refused", task=task, keys=keys, user_id=organiser.user.id)
        scope = await names.labelled(ctx, task_scope(task))
        raise AdminOnly(f"Only an admin of {scope.name} may change {', '.join(keys)}.", keys=keys)


def _refuse_reserved(written: Mapping[str, Edit]) -> None:
    for path in written:
        check_path(path)
    reserved = sorted(path for path in written if is_reserved(path))
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
    written: Mapping[str, WrittenEdit],
) -> bytes | None:
    """A file's content in the state: the save's when it writes the file,
    otherwise the head's, or none when neither has it.
    """
    if path in written:
        return written[path].content
    if path not in head.tokens:
        return None
    return (await ctx.forge.content.read_file(as_, task, path, at=head.version)).content


async def _workflows(
    ctx: Context, as_: Identity, definition: TaskDefinition
) -> tuple[dict[str, WorkflowDefinition], dict[str, str], tuple[Problem, ...]]:
    """Every workflow the task needs, read at its version as `as_`, which
    workflow each name is, by the forge's own id for it, and a problem for
    each that cannot be read or does not validate.
    """
    found: dict[str, WorkflowDefinition] = {}
    pins: dict[str, str] = {}
    problems: tuple[Problem, ...] = ()
    for ref, at in definition.workflow_refs():
        workflow = await names.workflow_id(ctx, ref)
        try:
            file = await ctx.forge.workflows.read_workflow_file(
                as_, workflow, ref.version, WORKFLOW_FILE
            )
            pins[f"{ref.owner}/{ref.name}"] = await ctx.forge.workflows.workflow_key(workflow)
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
    return found, pins, problems


async def _moved_workflows(
    ctx: Context, task: TaskId, definition: TaskDefinition, pins: Mapping[str, str]
) -> builtins.list[Problem]:
    """A problem at each workflow name the latest publication used for
    another workflow than the one it names now.
    """
    publications = await ctx.forge.workspaces.list_publications(task)
    pinned = publications[-1].workflows if publications else {}
    problems: builtins.list[Problem] = []
    for ref, at in definition.workflow_refs():
        name = f"{ref.owner}/{ref.name}"
        if name in pinned and name in pins and pins[name] != pinned[name]:
            message = (
                f"{name} now names a different workflow than the one this task was "
                "published with, as happens when its owner is renamed and someone else "
                "takes the name. Name the workflow you mean by its owner's name now."
            )
            problems.append(Problem(path=at, message=message))
    return problems


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
            as_, await names.workflow_id(ctx, use), use.version, WORKFLOW_FILE
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
    written: Mapping[str, WrittenEdit],
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
            if path not in written:
                data[path] = str(token)
        for path, edit in written.items():
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
    written: Mapping[str, WrittenEdit],
    plans: Mapping[str, bytes | None],
    message: str,
) -> VersionId:
    """Write the organiser's files and the plans as one change as `as_`, and
    return its version; with nothing to write, the head is the version.
    """
    files: dict[str, bytes | None] = {path: edit.content for path, edit in written.items()}
    files.update(plans)
    if not files:
        return head.version
    expected: dict[str, ConflictToken | None] = {path: head.tokens.get(path) for path in plans}
    expected.update({path: edit.token for path, edit in written.items()})
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


async def _held_pointer(
    ctx: Context, organiser: Organiser, task: TaskId, path: str, content: bytes
) -> bytes | None:
    """The pointer `content` is, written afresh, when it is exactly one the
    platform writes and the task's store holds its object, asked as the
    organiser; none otherwise.
    """
    named = read_pointer(content)
    if named is None or path == ATTRIBUTES_FILE or path.endswith(f"/{ATTRIBUTES_FILE}"):
        return None
    digest, size = named
    if not await uploads.holds_object(ctx, TaskPlace(task), organiser.identity, digest, size):
        return None
    return pointer_text(digest, size)


async def _resolve(
    ctx: Context, organiser: Organiser, task: TaskId, changes: Mapping[str, Edit]
) -> Mapping[str, WrittenEdit]:
    """Every edit as bytes the write can carry: an edit naming an upload
    becomes the pointer to it, and the upload is marked as taken by this
    save, while typed content that would read as a pointer is refused,
    unless it is exactly a pointer to an object the task already holds, as
    a rollback or a file saved back unchanged writes.

    A pointer names bytes by their hash alone, and a grading machine serves
    one from the org's shared store without asking the forge, so the only
    pointers in any repository are the ones written here, for objects the
    forge has confirmed belong to it.
    """
    named = {
        path: edit.content.upload
        for path, edit in changes.items()
        if isinstance(edit.content, Uploaded)
    }
    rows = await uploads.for_save(ctx, organiser.identity.user_id, task, set(named.values()))
    resolved: dict[str, WrittenEdit] = {}
    for path, edit in changes.items():
        if isinstance(edit.content, Uploaded):
            row = rows.get(edit.content.upload)
            if row is None or row.status not in (UploadStatus.WAITING, UploadStatus.VERIFIED):
                raise InvalidInputs(
                    "That upload is not one of yours for this task to write.",
                    errors=[{"input": path, "message": "No such upload."}],
                )
            if row.repo_path != path:
                raise InvalidInputs(
                    f"That upload was asked for {row.repo_path}, not {path}.",
                    errors=[{"input": path, "message": "The upload is for another path."}],
                )
            if not await uploads.holds(ctx, TaskPlace(task), organiser.identity, row):
                raise UploadNotReady(
                    "The file has not arrived yet; it cannot be saved until it has.",
                    uploads=[str(row.id)],
                )
            row.status = UploadStatus.CONSUMED
            resolved[path] = WrittenEdit(pointer_text(row.digest, row.size), edit.token)
            continue
        held = await _held_pointer(ctx, organiser, task, path, edit.content)
        if held is None:
            refuse_pointer(path, edit.content)
        resolved[path] = WrittenEdit(held or edit.content, edit.token)
    if named:
        await ctx.db.flush()
    return resolved
