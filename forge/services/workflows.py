"""Workflows as things at the forge: making one, reading it, editing its
`workflow.yaml`, freezing it under a version, who reads it, checking a
definition on its own, copying one and combining several.

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

The forge makes the place and the person writes its first commit, a
`workflow.yaml` that is valid as written
(`forge.domain.workflow_definition.starter_workflow`). A new workflow, a
copy and a combination are private. A copy is the source's files at a
version, with nothing written about where they came from; a combination is
its sources inlined into one definition
(`forge.domain.workflow_combine`). A draft with problems saves, so work can
stop half done; a version is made only of a draft that passes every check a
version must (TASK-FORMAT.md section 1.3), and a version never changes.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from forge.domain.content import ConflictToken
from forge.domain.errors import Conflict, Forbidden, InvalidName, NotFound, PortError, Rejected
from forge.domain.identity import AsUser, Identity, User
from forge.domain.ids import OrgId, WorkflowId
from forge.domain.names import validate_name
from forge.domain.plans import check_workflow, natural
from forge.domain.primitives import PrimitiveDeclaration
from forge.domain.roles import Role, Scope, holds
from forge.domain.sessions import Session
from forge.domain.workflow_combine import combine_workflows
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    WorkflowDefinition,
    WorkflowRef,
    parse_workflow,
    parse_workflow_ref,
    starter_workflow,
    validate_version,
)
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition, Problem, path_text
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import access, making, names, primitives, sessions

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


@dataclass(frozen=True, slots=True)
class WorkflowView:
    """A workflow as its page shows it. For someone who may edit it, the
    draft and the people it is shared with; for anyone else, neither.
    """

    summary: WorkflowSummary
    draft: Draft | None = None
    readers: tuple[User, ...] = ()


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
    files = starter_workflow(writer.label, name)
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
            owner=labels.get(workflow.owner, workflow.owner),
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
        _, file = await ctx.forge.workflows.read_workflow_draft(
            writer.identity, workflow, WORKFLOW_FILE
        )
        draft = Draft(file.content.decode("utf-8", errors="replace"), file.token)
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
    ctx: Context, session: Session, owner: str, name: str, text: str, token: ConflictToken | None
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
        file = await ctx.forge.workflows.write_workflow_file(
            writer.identity, workflow, WORKFLOW_FILE, content, expected=token, message=EDIT_MESSAGE
        )
    except Conflict:
        raise Conflict(
            f"{WORKFLOW_FILE} of {writer.label}/{name} has changed since you read it."
        ) from None
    log.info("workflows.saved", workflow=workflow, user_id=session.user_id)
    return Draft(text, file.token)


@action
async def create_version(
    ctx: Context, session: Session, owner: str, name: str, version: str
) -> str:
    """Freeze the workflow's saved `workflow.yaml` under `version`, once it
    passes every check a version must, against the primitives its steps use
    read as the caller. `InvalidDefinition` with every problem at its YAML
    path, and no version made, when it does not; `Conflict` when the version
    is there already; `InvalidName` for a name a version cannot have.
    """
    writer = await _writer(ctx, session, owner)
    validate_version(version)
    workflow = await _place(ctx, owner, name)
    head, file = await ctx.forge.workflows.read_workflow_draft(
        writer.identity, workflow, WORKFLOW_FILE
    )
    definition = parse_workflow(file.content)
    problems = await _problems(ctx, writer.identity, definition)
    if problems:
        raise InvalidDefinition(WORKFLOW_FILE, problems)
    try:
        await ctx.forge.workflows.create_workflow_version(
            writer.identity, workflow, version, at=head
        )
    except Conflict:
        raise Conflict(f"{writer.label}/{name} has a version {version} already.") from None
    log.info("workflows.versioned", workflow=workflow, version=version, user_id=session.user_id)
    return version


@action
async def check(ctx: Context, session: Session, text: str) -> tuple[Problem, ...]:
    """Every problem a version of `text` would be refused for, each at its
    YAML path, checked against the primitives its steps use as the person
    reads them. Nothing is written, no version is made and no plan is
    produced; none means it would make a version.
    """
    definition_text = _sized(text)
    try:
        definition = parse_workflow(definition_text)
    except InvalidDefinition as invalid:
        return tuple(invalid.errors)
    return tuple(await _problems(ctx, await _as_user(ctx, session), definition))


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
            f"{writer.label}/{name} is public, so everyone reads it; make it shared to name "
            "who reads it."
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
        raise Conflict(f"There is a workflow {writer.label}/{name} already.") from None
    except NotFound, Forbidden:
        raise _not_readable(f"{ref.owner}", f"{ref.name}@{ref.version}") from None
    except PortError as exc:
        raise making.failure(
            exc, "workflows.step_failed", owner=writer.at_forge, name=name
        ) from None
    log.info("workflows.copied", workflow=workflow, source=str(ref), user_id=session.user_id)
    return NewWorkflow(workflow, writer.label, name)


@action
async def combine(
    ctx: Context, session: Session, sources: Sequence[str], owner: str, name: str
) -> NewWorkflow:
    """A new private workflow `name` under `owner` that inlines every source,
    each `<owner>/<name>@<version>` the caller reads, into one definition
    (`forge.domain.workflow_combine`). `Rejected` for fewer than two
    sources or one in a format no longer read; `NotFound` for one the
    caller may not read; otherwise refused as `create` refuses.
    """
    if len(sources) < 2:
        raise Rejected("Combine takes two workflows or more; to start from one, copy it.")
    refs = [parse_workflow_ref(source) for source in sources]
    writer = await _writer(ctx, session, owner)
    validate_name(name)
    definitions: list[WorkflowDefinition] = []
    for ref in refs:
        text = await _read_at(ctx, writer.identity, ref)
        try:
            definitions.append(parse_workflow(text))
        except InvalidDefinition as invalid:
            raise Rejected(
                f"{ref} is not in the current format, so it cannot be combined: {invalid.detail}"
            ) from None
    files = {WORKFLOW_FILE: combine_workflows(definitions).encode()}
    workflow = await _make(ctx, writer, name, files)
    log.info(
        "workflows.combined",
        workflow=workflow,
        sources=[str(ref) for ref in refs],
        user_id=session.user_id,
    )
    return NewWorkflow(workflow, writer.label, name)


async def newer_version(ctx: Context, as_: Identity, ref: WorkflowRef) -> str | None:
    """The workflow's latest version in natural order when it comes after
    the one `ref` names, read as `as_`, or none: when there is no later one,
    or the workflow cannot be read.
    """
    try:
        described = await ctx.forge.workflows.describe_workflow(
            as_, await names.workflow_id(ctx, ref)
        )
    except NotFound, Forbidden:
        return None
    if not described.versions:
        return None
    latest = max(described.versions, key=natural)
    return latest if natural(latest) > natural(ref.version) else None


# Building blocks


async def _problems(ctx: Context, as_: Identity, definition: WorkflowDefinition) -> list[Problem]:
    """Every problem a version of the definition would be refused for: each
    `use:` that cannot be read as a primitive at its own path, then what
    `check_workflow` finds against the ones that can.
    """
    found: dict[str, PrimitiveDeclaration] = {}
    read: dict[str, str | None] = {}
    unread: list[Problem] = []
    for index, step in enumerate(definition.steps):
        use = str(step.use)
        if use not in read:
            declaration, problem = await primitives.declaration(ctx, as_, step.use)
            read[use] = problem
            if declaration is not None:
                found[use] = declaration
        problem = read[use]
        if problem is not None:
            unread.append(Problem(path=path_text(("steps", index, "use")), message=problem))
    said = {problem["path"] for problem in unread}
    checked = [
        problem for problem in check_workflow(definition, found) if problem["path"] not in said
    ]
    return [*unread, *checked]


async def _make(ctx: Context, writer: _Writer, name: str, files: dict[str, bytes]) -> WorkflowId:
    try:
        return await ctx.forge.workflows.create_workflow(
            writer.identity, writer.at_forge, name, files, Visibility.PRIVATE
        )
    except Conflict:
        raise Conflict(f"There is a workflow {writer.label}/{name} already.") from None
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
    return NotFound(f"There is no workflow {owner}/{name} you may read.")


async def _place(ctx: Context, owner: str, name: str) -> WorkflowId:
    validate_name(owner)
    validate_name(name)
    return await names.workflow_place(ctx, owner, name)


def _sized(text: str) -> bytes:
    content = text.encode()
    if len(content) > MAX_DEFINITION:
        raise Rejected(f"{WORKFLOW_FILE} holds at most {MAX_DEFINITION // 1024} KB.")
    return content


async def _user(ctx: Context, username: str) -> User:
    try:
        return await ctx.forge.identity.find_user_by_username(username)
    except NotFound:
        raise NotFound(f"There is no user {username}.") from None


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
        raise Forbidden(f"This needs the {Role.MANAGER.value} role at {owner}.")
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
            f"Your username {username!r} cannot name a workflow: an owner is lower case "
            "letters, digits, hyphens and underscores. Make it under an org instead."
        ) from None
