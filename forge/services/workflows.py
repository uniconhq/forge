"""Workflows as things at the forge: making one, reading it, editing its
`workflow.yaml`, freezing it under a version, who reads it, checking a
definition on its own, and copying one.

Who may change a workflow. Anyone signed in may make one under their own
name, or under an org where they hold the manager role or above; an
observer of the org may not, and neither may anyone for another person. The
same rule says who may edit one, version it and change who reads it: the
person it is named for, or a manager or admin of its org. The forge keeps
its own check underneath, since every change is made as that person, or,
for who reads it, as the platform once the forge has said the person may
write it.

The owner is given by the name people call it. An org's name is looked up
first, the way a `workflow.yaml` reference reads an owner
(`names.workflow_id`), so `acme/tuned` names the same workflow in both. An
owner that is neither the caller nor an org where they are a manager is
refused with `Forbidden`, the same words whether there is such an org or
not, so nobody learns which orgs exist. A person's own workflows sit under
their username, which has to follow the name rules once in lower case for
the workflow to be named by it.

Who may read a workflow is the forge's to say, asked as the person: its
owner, the people it is shared with, and anyone when it is public. A
workflow someone may not read is answered as one that is not there, in the
same words, so its name tells them nothing.

A workflow has one of three visibilities. Private is its owner alone, and
public everyone. Shared is private with a list of named readers, so it
reads back as shared once the list holds someone; making it private or
public empties the list, and the list is edited only while it is not
public.

The forge makes the place and the person writes its first commit,
an empty `workflow.yaml` draft
(`forge.domain.workflow_definition.empty_workflow`). A new workflow and a
copy are private. A copy is the source's files at a version, with nothing
written about where they came from. A draft with problems saves, so work can
stop half done; a version is made only of a draft that passes every check a
version must (TASK-FORMAT.md section 1.3), and a version never changes.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal

from forge.domain.content import (
    ConflictToken,
    Edit,
    EntryKind,
    File,
    TreeEntry,
    Uploaded,
    WrittenEdit,
    check_path,
)
from forge.domain.errors import (
    Conflict,
    Forbidden,
    InvalidInputs,
    InvalidName,
    NotFound,
    PortError,
    Rejected,
    UploadNotReady,
)
from forge.domain.identity import AsUser, Identity, User
from forge.domain.ids import OrgId, VersionId, WorkflowId
from forge.domain.names import validate_name
from forge.domain.port_values import natural, stray_units
from forge.domain.primitives import Port
from forge.domain.roles import Role, Scope, holds
from forge.domain.run_workflow import WorkflowFiles, check_run_workflow
from forge.domain.sessions import Session
from forge.domain.types import Type
from forge.domain.uploads import (
    Door,
    UploadStatus,
    filename_problem,
    may_be_pointer,
    pointer_text,
    refuse_pointer,
    upload_info,
)
from forge.domain.workflow_boxes import placed
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    RunInput,
    RunOutput,
    RunWorkflow,
    WorkflowRef,
    empty_workflow,
    parse_run_workflow,
    parse_workflow_ref,
    validate_version,
    version_number,
)
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.port.uploads import WorkflowPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import access, making, names, sessions, uploads, workflow_boxes

log = get_logger(__name__)

MAX_DEFINITION = 256 * 1024
"""The most a `workflow.yaml` holds, in bytes: a definition is a few dozen
lines, and a page sends it after every change."""

EDIT_MESSAGE = "Edit workflow.yaml"


@dataclass(frozen=True, slots=True)
class NewWorkflow:
    """A workflow just made: its id at the forge, and the owner and name a
    person calls it by, `<owner>/<name>`. An org's id is built from its key,
    so the id is for the package and the owner and the name are for showing.
    """

    id: WorkflowId
    owner: str
    name: str


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    """A workflow a person may read: by the owner and name they call it,
    its visibility, its versions in natural order, and whether they may
    edit it.
    """

    owner: str
    name: str
    visibility: Visibility
    versions: tuple[str, ...]
    editable: bool


@dataclass(frozen=True, slots=True)
class Draft:
    """`workflow.yaml` as the workflow holds it now, and the token a save of
    it carries; none when there is no such file to replace.
    """

    text: str
    token: ConflictToken | None
    revision: VersionId | None = None


@dataclass(frozen=True, slots=True)
class WorkflowView:
    """A workflow as its page shows it. For someone who may edit it, the
    draft and the people it is shared with; for anyone else, neither.
    """

    summary: WorkflowSummary
    draft: Draft | None = None
    readers: tuple[User, ...] = ()


@dataclass(frozen=True, slots=True)
class BoxInput:
    """One input of a run workflow drawn as a box: its name, its type, the
    options an enum takes, whether a task may leave it out, and the form's
    title and unit.
    """

    name: str
    type: str
    options: tuple[str, ...] | None
    optional: bool
    title: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class BoxOutput:
    """One output of a run workflow drawn as a box, the port it resolves to.
    `type` and the number's meaning are none while that port is not known,
    as an output of a manifest the task gives is not. `title` and `unit` are
    the words the workflow gave the output, and a unit only on a number.
    """

    name: str
    type: str | None
    title: str | None
    unit: str | None
    better: str | None
    at_least: int | Decimal | None
    at_most: int | Decimal | None


@dataclass(frozen=True, slots=True)
class Box:
    """A workflow version as a box: whether it is a run workflow, always so
    now, its inputs and outputs, a later numbered version when it has one,
    and what it does, as its `description` says.
    """

    run: bool
    inputs: tuple[BoxInput, ...]
    outputs: tuple[BoxOutput, ...]
    newer_version: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class _Writer:
    """Someone who may change a workflow under an owner: the owner as people
    call it, the identity calls are made as, and the owner as the forge
    files it, an org's key or the username.
    """

    label: str
    identity: AsUser
    at_forge: str


@action
async def create(ctx: Context, session: Session, owner: str, name: str) -> NewWorkflow:
    """Make the workflow `name` under `owner`, the caller's own username or
    an org's name, private, with its first commit written as the caller.
    `Forbidden` unless the owner is the caller or an org where they hold the
    manager role or above; `InvalidName` for a name that breaks the rules,
    or a username that cannot name a workflow; `Conflict` when the owner
    has a workflow of that name.
    """
    writer = await _writer(ctx, session, owner)
    validate_name(name)
    files = empty_workflow(writer.label, name)
    workflow = await _make(ctx, writer, name, files)
    log.info("workflows.created", workflow=workflow, user_id=session.user_id)
    return NewWorkflow(workflow, writer.label, name)


@action
async def listing(ctx: Context, session: Session) -> tuple[WorkflowSummary, ...]:
    """Every workflow the person may read, by owner and then name: their
    own, their orgs', those shared with them and the public ones, each
    saying whether they may edit it.
    """
    as_ = await _as_user(ctx, session)
    found = await ctx.forge.workflows.workflows_readable_by(as_)
    labels = await names.names_of(ctx, {workflow.owner for workflow in found})
    grants = await ctx.forge.orgs.roles_of(as_)
    username = (await _username_now(ctx, session)).lower()
    summaries = [
        WorkflowSummary(
            owner=labels.get(workflow.owner, workflow.owner.lower()),
            name=workflow.name,
            visibility=workflow.visibility,
            versions=tuple(sorted(workflow.versions, key=natural)),
            editable=(
                holds(grants, Scope(OrgId(workflow.owner)), Role.MANAGER)
                if workflow.owner in labels
                else workflow.owner.lower() == username
            ),
        )
        for workflow in found
    ]
    return tuple(sorted(summaries, key=lambda summary: (summary.owner, summary.name)))


@action
async def view(ctx: Context, session: Session, owner: str, name: str) -> WorkflowView:
    """The workflow `<owner>/<name>` as its page shows it to the person.
    `NotFound` when there is none they may read.
    """
    as_ = await _as_user(ctx, session)
    workflow = await _place(ctx, owner, name)
    try:
        described = await ctx.forge.workflows.describe_workflow(as_, workflow)
    except NotFound, Forbidden:
        raise _not_readable(owner, name) from None
    writer = await _writer_or_none(ctx, session, owner)
    summary = WorkflowSummary(
        owner=writer.label if writer is not None else owner,
        name=name,
        visibility=described.visibility,
        versions=tuple(sorted(described.versions, key=natural)),
        editable=writer is not None,
    )
    if writer is None:
        return WorkflowView(summary)
    try:
        head, file = await ctx.forge.workflows.read_workflow_draft(
            writer.identity, workflow, WORKFLOW_FILE
        )
        draft = Draft(file.content.decode("utf-8", errors="replace"), file.token, head)
    except NotFound:
        draft = Draft("", None)
    readers = await ctx.forge.workflows.workflow_readers(writer.identity, workflow)
    return WorkflowView(summary, draft, readers)


@action
async def read_version(ctx: Context, session: Session, ref: str) -> str:
    """The `workflow.yaml` of `<owner>/<name>@<version>`, read as the person
    signed in, so the forge says whether they may: the owner, someone it is
    shared with, or anyone when it is public. `NotFound`, in the same words,
    when there is no such version or they may not read it.
    """
    found = parse_workflow_ref(ref)
    text = await _read_at(ctx, await _as_user(ctx, session), found)
    return text


@action
async def save(
    ctx: Context,
    session: Session,
    owner: str,
    name: str,
    text: str,
    token: ConflictToken | None,
    *,
    changes: Mapping[str, Edit] | None = None,
) -> Draft:
    """Write `text` as the workflow's `workflow.yaml`, as the caller, with
    the token it was read with. Whatever it holds saves, problems and all,
    since a draft may stop half done. `Conflict` when the file has changed
    since it was read; `Rejected` for one over `MAX_DEFINITION`.
    """
    writer = await _writer(ctx, session, owner)
    content = _sized(text)
    workflow = await _place(ctx, owner, name)
    try:
        resolved = await _file_edits(ctx, writer.identity, workflow, changes or {})
        resolved[WORKFLOW_FILE] = WrittenEdit(content, token)
        revision = await ctx.forge.workflows.write_workflow_files(
            writer.identity,
            workflow,
            resolved,
            message="Edit workflow files" if changes else EDIT_MESSAGE,
        )
        file = await ctx.forge.workflows.read_workflow_at(
            writer.identity, workflow, revision, WORKFLOW_FILE
        )
    except Conflict:
        raise Conflict(
            f"The workflow {writer.label}/{name} has changed since you opened it. Reload it, "
            "then make your changes again."
        ) from None
    log.info("workflows.saved", workflow=workflow, user_id=session.user_id)
    return Draft(text, file.token, revision)


@action
async def create_version(
    ctx: Context,
    session: Session,
    owner: str,
    name: str,
    version: str,
    token: ConflictToken | None = None,
    *,
    revision: VersionId | None = None,
) -> str:
    """Freeze the workflow's saved `workflow.yaml` under `version`, once it
    passes every check a version must, against the primitives its steps use
    read as the caller. Given the `token` the caller saved it with, the
    version is of that save or not made: `Conflict` when someone has saved
    since. `InvalidDefinition` with every problem at its YAML path, and no
    version made, when it does not check; `Conflict` when the version is
    there already; `InvalidName` for a name a version cannot have.
    """
    writer = await _writer(ctx, session, owner)
    validate_version(version)
    workflow = await _place(ctx, owner, name)
    head, file = await ctx.forge.workflows.read_workflow_draft(
        writer.identity, workflow, WORKFLOW_FILE
    )
    if (token is not None and file.token != token) or (revision is not None and head != revision):
        raise Conflict(
            f"The workflow {writer.label}/{name} has been saved again since your save. Reload "
            "it before making a version of it."
        )
    definition = parse_run_workflow(file.content)
    files = await workflow_boxes.files_at(ctx, writer.identity, workflow, head, definition)
    problems = await _problems(ctx, writer.identity, definition, files, own=workflow)
    if problems:
        raise InvalidDefinition(WORKFLOW_FILE, problems)
    try:
        await ctx.forge.workflows.create_workflow_version(
            writer.identity, workflow, version, at=head
        )
    except Conflict:
        raise Conflict(
            f"{writer.label}/{name} already has a version {version}. Choose another version."
        ) from None
    log.info("workflows.versioned", workflow=workflow, version=version, user_id=session.user_id)
    return version


@action
async def check(
    ctx: Context,
    session: Session,
    text: str,
    *,
    workflow: str | None = None,
    owner: str | None = None,
    name: str | None = None,
) -> tuple[Problem, ...]:
    """Every problem a version of `text` would be refused for, each at its
    YAML path, checked against the primitives its steps use as the person
    reads them. Nothing is written, no version is made and no plan is
    produced; none means it would make a version. A workflow's own files
    are the version's, so here a `${{ workflow.<path> }}` is checked
    only for its form, and the reports of a `program.yaml` among them only
    when a version is made. `workflow`, the id of the workflow this text
    is a draft of, is put first on the path a box is refused for holding.
    `owner` and `name` name that workflow when the id is not given.
    """
    definition_text = _sized(text)
    try:
        definition = parse_run_workflow(definition_text)
    except InvalidDefinition as invalid:
        return tuple(invalid.errors)
    own = WorkflowId(workflow) if workflow is not None else None
    if own is None and owner is not None and name is not None:
        own = await names.workflow_place(ctx, owner, name)
    return tuple(await _problems(ctx, await _as_user(ctx, session), definition, own=own))


@action
async def set_visibility(
    ctx: Context, session: Session, owner: str, name: str, visibility: Visibility
) -> None:
    """Make the workflow private, shared or public. Private and public empty
    its list of readers; shared keeps it, and the workflow reads back as
    shared once the list holds someone.
    """
    writer = await _writer(ctx, session, owner)
    workflow = await _place(ctx, owner, name)
    if visibility is not Visibility.SHARED:
        for reader in await ctx.forge.workflows.workflow_readers(writer.identity, workflow):
            await ctx.forge.workflows.unshare_workflow(writer.identity, workflow, reader.id)
    await ctx.forge.workflows.set_workflow_visibility(writer.identity, workflow, visibility)
    log.info(
        "workflows.visibility",
        workflow=workflow,
        visibility=visibility.value,
        user_id=session.user_id,
    )


@action
async def share(ctx: Context, session: Session, owner: str, name: str, username: str) -> User:
    """Let the user named `username` read the workflow. `Conflict` while it
    is public, which everyone reads; `NotFound` when there is no such user.
    """
    writer = await _writer(ctx, session, owner)
    workflow = await _place(ctx, owner, name)
    described = await ctx.forge.workflows.describe_workflow(writer.identity, workflow)
    if described.visibility is Visibility.PUBLIC:
        raise Conflict(
            f"{writer.label}/{name} is public, so everyone can read it. Make it shared to "
            "choose who can."
        )
    user = await _user(ctx, username)
    await ctx.forge.workflows.share_workflow(writer.identity, workflow, user.id)
    log.info("workflows.shared", workflow=workflow, reader=user.id, user_id=session.user_id)
    return user


@action
async def unshare(ctx: Context, session: Session, owner: str, name: str, username: str) -> None:
    """Take the read of the workflow from the user named `username`."""
    writer = await _writer(ctx, session, owner)
    workflow = await _place(ctx, owner, name)
    user = await _user(ctx, username)
    await ctx.forge.workflows.unshare_workflow(writer.identity, workflow, user.id)
    log.info("workflows.unshared", workflow=workflow, reader=user.id, user_id=session.user_id)


@action
async def copy(ctx: Context, session: Session, source: str, owner: str, name: str) -> NewWorkflow:
    """A new private workflow `name` under `owner` holding the files of
    `source`, `<owner>/<name>@<version>`, read as the caller, with nothing
    written about where they came from. `NotFound` when the caller may not
    read the source at that version; otherwise refused as `create` refuses.
    """
    ref = parse_workflow_ref(source)
    writer = await _writer(ctx, session, owner)
    validate_name(name)
    origin = await names.workflow_id(ctx, ref)
    try:
        workflow = await ctx.forge.workflows.copy_workflow(
            writer.identity, origin, ref.version, writer.at_forge, name
        )
    except Conflict:
        raise Conflict(
            f"There is already a workflow called {writer.label}/{name}. Choose another name."
        ) from None
    except NotFound, Forbidden:
        raise _not_readable(f"{ref.owner}", f"{ref.name}@{ref.version}") from None
    except PortError as exc:
        raise making.failure(
            exc, "workflows.step_failed", owner=writer.at_forge, name=name
        ) from None
    log.info("workflows.copied", workflow=workflow, source=str(ref), user_id=session.user_id)
    return NewWorkflow(workflow, writer.label, name)


async def newer_version(ctx: Context, as_: Identity, ref: WorkflowRef) -> str | None:
    """The workflow's version with the largest number when it is larger than
    the one `ref` names, read as `as_`, or none: when there is no larger
    one, or the workflow cannot be read. A tag under a name that is not `v`
    and a number has no number, so it is never newer, and nothing is newer
    than it.
    """
    pinned = version_number(ref.version)
    if pinned is None:
        return None
    try:
        described = await ctx.forge.workflows.describe_workflow(
            as_, await names.workflow_id(ctx, ref)
        )
    except NotFound, Forbidden:
        return None
    numbered = {
        number: version
        for version in described.versions
        if (number := version_number(version)) is not None
    }
    if not numbered or max(numbered) <= pinned:
        return None
    return numbered[max(numbered)]


@action
async def box(ctx: Context, session: Session, ref: str) -> Box:
    """The version `ref`, `<owner>/<name>@<version>`, as a box the editor
    draws: its inputs, and each output as the port it resolves to, checked
    as the caller. `NotFound`, in the same words as reading the version,
    when there is no such version or the caller may not read it.
    """
    found = parse_workflow_ref(ref)
    as_ = await _as_user(ctx, session)
    definition = parse_run_workflow(await _read_at(ctx, as_, found))
    newer = await newer_version(ctx, as_, found)
    workflow = await names.workflow_id(ctx, found)
    expanded = await workflow_boxes.expand(ctx, as_, definition, own=workflow)
    outputs: dict[str, BoxOutput] = {}
    if expanded.flat is not None:
        at = await ctx.forge.workflows.workflow_version(as_, workflow, found.version)
        files = await workflow_boxes.files_at(ctx, as_, workflow, at, definition)
        checked = check_run_workflow(
            expanded.flat.workflow,
            expanded.primitives,
            files,
            inner_files=expanded.inner_files,
            boxes=expanded.flat.boxes,
            flat=bool(expanded.flat.boxes),
        )
        resolved = placed(checked, expanded.flat) if expanded.flat.boxes else checked
        outputs = {
            name: _box_output(name, output.port, output.title, output.unit)
            for name, output in resolved.outputs.items()
        }
    return Box(
        True,
        tuple(_box_input(name, declared) for name, declared in definition.inputs.items()),
        tuple(
            outputs.get(name, _box_output(name, None, *_output_words(entry)))
            for name, entry in definition.outputs.items()
        ),
        newer,
        definition.description,
    )


def _box_input(name: str, declared: RunInput) -> BoxInput:
    options = declared.options
    return BoxInput(
        name,
        declared.type.value,
        None if options is None else tuple(options),
        declared.optional,
        declared.title,
        declared.unit,
    )


def _output_words(entry: str | RunOutput) -> tuple[str | None, str | None]:
    if isinstance(entry, RunOutput):
        return entry.title, entry.unit
    return None, None


def _box_output(name: str, port: Port | None, title: str | None, unit: str | None) -> BoxOutput:
    if port is None:
        return BoxOutput(name, None, title, unit, None, None, None)
    number = port.type is Type.NUMBER
    return BoxOutput(
        name,
        port.type.value,
        title,
        unit if number else None,
        None if port.better is None else port.better.value,
        port.at_least,
        port.at_most,
    )


# Building blocks


async def _problems(
    ctx: Context,
    as_: Identity,
    definition: RunWorkflow,
    files: WorkflowFiles | None = None,
    *,
    own: WorkflowId | None = None,
) -> list[Problem]:
    """Every problem a version of the definition would be refused for: a
    unit on an input that is not a number (`stray_units`), then what the
    workflow expanded (`workflow_boxes.expand`), boxes and all, finds, and
    what `check_run_workflow` finds of the flat list against `files`, its
    own at the version's commit. `own` is that workflow, first on the path
    a box is refused for holding.
    """
    expanded = await workflow_boxes.expand(ctx, as_, definition, own=own)
    if expanded.flat is None:
        return [*stray_units(definition), *expanded.problems]
    checked = check_run_workflow(
        expanded.flat.workflow,
        expanded.primitives,
        files,
        inner_files=expanded.inner_files,
        boxes=expanded.flat.boxes,
        flat=bool(expanded.flat.boxes),
    )
    found = placed(checked, expanded.flat).problems if expanded.flat.boxes else checked.problems
    said = {problem["path"] for problem in expanded.problems}
    return [
        *stray_units(definition),
        *expanded.problems,
        *(problem for problem in found if problem["path"] not in said),
    ]


async def _make(ctx: Context, writer: _Writer, name: str, files: dict[str, bytes]) -> WorkflowId:
    try:
        return await ctx.forge.workflows.create_workflow(
            writer.identity, writer.at_forge, name, files, Visibility.PRIVATE
        )
    except Conflict:
        raise Conflict(
            f"There is already a workflow called {writer.label}/{name}. Choose another name."
        ) from None
    except PortError as exc:
        raise making.failure(
            exc, "workflows.step_failed", owner=writer.at_forge, name=name
        ) from None


async def _read_at(ctx: Context, as_: Identity, ref: WorkflowRef) -> str:
    try:
        file = await ctx.forge.workflows.read_workflow_file(
            as_, await names.workflow_id(ctx, ref), ref.version, WORKFLOW_FILE
        )
    except NotFound, Forbidden:
        raise _not_readable(ref.owner, f"{ref.name}@{ref.version}") from None
    return file.content.decode("utf-8", errors="replace")


def _not_readable(owner: str, name: str) -> NotFound:
    return NotFound(f"There is no workflow {owner}/{name} that you can read.")


async def _place(ctx: Context, owner: str, name: str) -> WorkflowId:
    validate_name(owner)
    validate_name(name)
    return await names.workflow_place(ctx, owner, name)


def _sized(text: str) -> bytes:
    content = text.encode()
    if len(content) > MAX_DEFINITION:
        raise Rejected(f"A workflow can be at most {MAX_DEFINITION // 1024} KB. Make it smaller.")
    return content


async def _user(ctx: Context, username: str) -> User:
    try:
        return await ctx.forge.identity.find_user_by_username(username)
    except NotFound:
        raise NotFound(f"There is no user called {username}.") from None


async def _as_user(ctx: Context, session: Session) -> AsUser:
    return AsUser(session.user_id, await sessions.credential_for(ctx, session.id))


async def _writer(ctx: Context, session: Session, owner: str) -> _Writer:
    """The caller as someone who may change workflows under `owner`, or
    `Forbidden` in the same words whether or not there is such an org.
    """
    org = await names.org_id(ctx, owner)
    if org is not None:
        organiser = await access.organiser(ctx, session, Scope(org, label=owner), Role.MANAGER)
        return _Writer(owner, organiser.identity, org)
    username = await _username_now(ctx, session)
    if owner.lower() != username.lower():
        log.info("access.refused", user_id=session.user_id, owner=owner, role=Role.MANAGER.value)
        raise Forbidden(
            f"This needs the {Role.MANAGER.value} role at {owner}. Ask an admin of {owner} if "
            "you need it."
        )
    return _Writer(_own_label(username), await _as_user(ctx, session), username)


async def _writer_or_none(ctx: Context, session: Session, owner: str) -> _Writer | None:
    try:
        return await _writer(ctx, session, owner)
    except Forbidden, InvalidName:
        return None


async def _username_now(ctx: Context, session: Session) -> str:
    """The caller's username as the forge has it now. The session keeps the
    one they signed in with, which a rename on the account page leaves
    behind, and someone else may since have taken it.
    """
    try:
        return (await ctx.forge.identity.find_user(session.user_id)).username
    except PortError as exc:
        raise making.failure(exc, "workflows.step_failed", user_id=str(session.user_id)) from None


def _own_label(username: str) -> str:
    """The owner a person's own workflows are named by: their username in
    lower case. `InvalidName` when that breaks the name rules, as a username
    with a dot does, since a workflow named by it could not be referred to.
    """
    label = username.lower()
    try:
        return validate_name(label)
    except InvalidName:
        raise InvalidName(
            f"Your username {username!r} cannot be used to name a workflow, since it has "
            "characters other than lower case letters, digits, hyphens and underscores. Make "
            "the workflow under an org instead."
        ) from None


@action
async def file_slot(
    ctx: Context,
    session: Session,
    owner: str,
    name: str,
    *,
    path: str,
    size: int,
    sha256: str,
    content_type: str | None = None,
) -> uploads.Slot:
    writer = await _writer(ctx, session, owner)
    workflow = await _place(ctx, owner, name)
    await ctx.forge.workflows.describe_workflow(writer.identity, workflow)
    _file_path(path)
    return await uploads.workflow_file_slot(
        ctx,
        writer.identity,
        workflow,
        path=path,
        size=size,
        sha256=sha256,
        content_type=content_type,
    )


@action
async def complete_file(
    ctx: Context, session: Session, owner: str, name: str, upload: uuid.UUID
) -> uploads.Upload:
    return await uploads.complete_workflow(ctx, session, await _place(ctx, owner, name), upload)


@action
async def read_file(ctx: Context, session: Session, owner: str, name: str, path: str) -> File:
    as_, workflow, head = await _files_head(ctx, session, owner, name)
    file = await ctx.forge.workflows.read_workflow_at(as_, workflow, head, check_path(path))
    return replace(file, upload=upload_info(file.content), version=head)


async def _files_head(
    ctx: Context, session: Session, owner: str, name: str
) -> tuple[AsUser, WorkflowId, VersionId]:
    as_ = await _as_user(ctx, session)
    workflow = await _place(ctx, owner, name)
    try:
        await ctx.forge.workflows.describe_workflow(as_, workflow)
    except NotFound, Forbidden:
        raise _not_readable(owner, name) from None
    writer = await _writer(ctx, session, owner)
    head, _ = await ctx.forge.workflows.read_workflow_draft(
        writer.identity, workflow, WORKFLOW_FILE
    )
    return writer.identity, workflow, head


@action
async def files(
    ctx: Context, session: Session, owner: str, name: str, path: str = ""
) -> tuple[TreeEntry, ...]:
    as_, workflow, head = await _files_head(ctx, session, owner, name)
    return await _listing(ctx, as_, workflow, head, path)


@action
async def version_files(
    ctx: Context, session: Session, ref: str, path: str = ""
) -> tuple[TreeEntry, ...]:
    """The files and folders directly under `path` that
    `<owner>/<name>@<version>` was frozen with, as the draft's are listed,
    read as the person signed in: anyone who may read the version may see
    what it ships with. `NotFound`, in the same words as the version
    itself, when there is no such version or they may not read it.
    """
    as_, workflow, at = await _version_at(ctx, session, ref)
    return await _listing(ctx, as_, workflow, at, path)


@action
async def version_download(ctx: Context, session: Session, ref: str, path: str) -> Door:
    """Where one file of `<owner>/<name>@<version>` is read whole, and what
    to present there as the person signed in, for the proxy to fetch it and
    stream it to them: a typed file and an upload alike, whatever its size.
    Read as they are, as `version_files` lists it, at the commit the
    version names and never at a commit given, so no draft is reached
    through it. `NotFound` as for `version_files`; `InvalidPath` for a path
    that is not plain. Whether the file is there is the forge's to answer
    when the proxy asks.
    """
    as_, workflow, at = await _version_at(ctx, session, ref)
    door = await ctx.forge.workflows.workflow_media_door(as_, workflow, at, check_path(path))
    log.info("workflows.download_opened", workflow=workflow, user_id=session.user_id)
    return door


@action
async def draft_download(ctx: Context, session: Session, owner: str, name: str, path: str) -> Door:
    """Where one file of `<owner>/<name>`'s draft is read whole, and what to
    present there, for the proxy to fetch it and stream it, as `files` lists
    it: at the draft's head, read by whoever may change the workflow.
    `NotFound` for someone who may not read the workflow and `Forbidden` for
    one who may read it but not change it, as for `files`; `InvalidPath`
    for a path that is not plain.
    """
    as_, workflow, head = await _files_head(ctx, session, owner, name)
    door = await ctx.forge.workflows.workflow_media_door(as_, workflow, head, check_path(path))
    log.info("workflows.download_opened", workflow=workflow, user_id=session.user_id)
    return door


async def _version_at(
    ctx: Context, session: Session, ref: str
) -> tuple[AsUser, WorkflowId, VersionId]:
    """The caller, the workflow and the commit its version stands at."""
    found = parse_workflow_ref(ref)
    as_ = await _as_user(ctx, session)
    try:
        workflow = await names.workflow_id(ctx, found)
        at = await ctx.forge.workflows.workflow_version(as_, workflow, found.version)
    except NotFound, Forbidden:
        raise _not_readable(found.owner, f"{found.name}@{found.version}") from None
    return as_, workflow, at


async def _listing(
    ctx: Context, as_: Identity, workflow: WorkflowId, at: VersionId, path: str
) -> tuple[TreeEntry, ...]:
    """The entries directly under `path` at the commit `at`, each file that
    may be an upload read to say what it holds.
    """
    folder = check_path(path) + "/" if path else ""
    listed = await ctx.forge.workflows.workflow_files(as_, workflow, at)
    entries: dict[str, TreeEntry] = {}
    for held, token in sorted(listed.tokens.items()):
        if not held.startswith(folder):
            continue
        rest = held[len(folder) :]
        if "/" in rest:
            directory = folder + rest.split("/", 1)[0]
            entries[directory] = TreeEntry(directory, EntryKind.DIRECTORY)
        else:
            info = None
            if may_be_pointer(listed.sizes.get(held)):
                read = await ctx.forge.workflows.read_workflow_at(as_, workflow, at, held)
                info = upload_info(read.content)
            entries[held] = TreeEntry(
                held,
                EntryKind.FILE,
                size=listed.sizes.get(held),
                token=str(token),
                upload=info,
                version=at,
            )
    return tuple(entries.values())


def _file_path(path: str) -> None:
    check_path(path)
    if path == WORKFLOW_FILE:
        raise InvalidInputs("Change workflow.yaml in the workflow editor.")
    for part in path.split("/"):
        if problem := filename_problem(part):
            raise InvalidInputs(problem, errors=[{"input": path, "message": problem}])


async def _file_edits(
    ctx: Context, as_: AsUser, workflow: WorkflowId, changes: Mapping[str, Edit]
) -> dict[str, WrittenEdit]:
    rows = await uploads.workflow_uploads(
        ctx,
        as_.user_id,
        workflow,
        {edit.content.upload for edit in changes.values() if isinstance(edit.content, Uploaded)},
    )
    resolved: dict[str, WrittenEdit] = {}
    for path, edit in changes.items():
        _file_path(path)
        content = edit.content
        if content is None and edit.token is None:
            raise InvalidInputs("Removing a file needs the token it was read with.")
        if isinstance(content, Uploaded):
            row = rows.get(content.upload)
            if (
                row is None
                or row.repo_path != path
                or row.status not in (UploadStatus.WAITING, UploadStatus.VERIFIED)
            ):
                raise InvalidInputs(
                    "There is no such upload for this workflow and path.",
                    errors=[{"input": path, "message": "There is no such upload."}],
                )
            if not await uploads.holds(ctx, WorkflowPlace(workflow), as_, row):
                raise UploadNotReady(
                    "The file has not finished uploading. Save again once it has.",
                    uploads=[str(row.id)],
                )
            row.status = UploadStatus.CONSUMED
            content = pointer_text(row.digest, row.size)
        elif content is not None:
            refuse_pointer(path, content)
        resolved[path] = WrittenEdit(content, edit.token)
    await ctx.db.flush()
    return resolved
