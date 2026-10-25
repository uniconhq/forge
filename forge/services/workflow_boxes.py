"""A run workflow that uses another as a step, read and expanded where a
version or a text is checked (SIMPLER-CORE.md 7.4).

`files_at` lives here so a workflow's files and a box's files are read by
the same function, and the services that call it do not import each other
in a ring. `expand` reads every box as the person checking, refuses a
workflow that would hold itself, counts the 32 `use:` references of the
whole expansion, and flattens what it read into primitive steps.
"""

from dataclasses import dataclass

from forge.domain.errors import Forbidden, NotFound, Rejected
from forge.domain.identity import Identity
from forge.domain.ids import VersionId, WorkflowId
from forge.domain.primitives import PrimitiveDeclaration
from forge.domain.program_manifest import MAX_MANIFEST
from forge.domain.run_workflow import WorkflowFiles, manifest_paths
from forge.domain.uploads import upload_info
from forge.domain.workflow_boxes import FlatWorkflow, flatten, over_cap
from forge.domain.workflow_definition import (
    MAX_USES,
    WORKFLOW_FILE,
    RunWorkflow,
    WorkflowRef,
    parse_run_workflow,
)
from forge.domain.yaml_models import InvalidDefinition, Problem, path_text
from forge.runtime.context import Context
from forge.services import names, primitives

_CANNOT_OPEN = (
    "Unicon cannot open {use}: there is no such primitive or workflow at that "
    "version, or it is not shared with you."
)
_OLD = (
    "The workflow {use} is written in an older form that Unicon no longer reads, so its "
    "owner needs to make a new version of it. {detail}"
)


@dataclass(frozen=True, slots=True)
class PinnedUse:
    """One inner workflow version, in pin order from 1: the `use:` text, the
    workflow and commit it was read at, and the forge's own key for it."""

    use: str
    workflow: WorkflowId
    at: VersionId
    key: str


@dataclass(frozen=True, slots=True)
class Expanded:
    """What expanding a run workflow found. `flat` is none when the workflow
    would hold itself, the expansion uses more than 32 versions, or the flat
    list itself is refused. `primitives` is every primitive `use:` that did
    read, by its text. `inner_files` is each pin's files, from pin 1, and
    `pins` the workflow each of those pins was read from, in order.
    """

    flat: FlatWorkflow | None
    primitives: dict[str, PrimitiveDeclaration]
    inner_files: dict[int, WorkflowFiles]
    problems: list[Problem]
    pins: tuple[PinnedUse, ...] = ()


@dataclass(frozen=True, slots=True)
class _Use:
    """One distinct `use:` after it has been read once. `kind` is
    `primitive`, `workflow` or `bad`.
    """

    kind: str
    message: str | None = None
    key: str | None = None


async def files_at(
    ctx: Context, as_: Identity, workflow: WorkflowId, at: VersionId, definition: RunWorkflow
) -> WorkflowFiles:
    """The workflow's files at the commit `at`, read as `as_`, as making a
    version checks them and a task's save checks them again: every
    path with its size, and each one wired into a manifest port that is
    there. One listed that does not read as a file is left out, so the
    check refuses it as missing at the manifest port; one listed as over
    `MAX_MANIFEST` is never read.
    """
    listed = await ctx.forge.workflows.workflow_files(as_, workflow, at)
    paths = set(listed.tokens)
    manifests: dict[str, bytes] = {}
    oversized: set[str] = set()
    sizes = dict(listed.sizes)
    for path in sorted(manifest_paths(definition) & paths):
        if listed.sizes.get(path, 0) > MAX_MANIFEST:
            oversized.add(path)
            continue
        try:
            read = await ctx.forge.workflows.read_workflow_at(as_, workflow, at, path)
        except NotFound:
            paths.discard(path)
            continue
        content = read.content
        if info := upload_info(content):
            sizes[path] = info.size
            if info.size > MAX_MANIFEST:
                oversized.add(path)
                continue
            try:
                content = await ctx.forge.workflows.read_workflow_media(
                    as_, workflow, at, path, max_size=MAX_MANIFEST
                )
            except Rejected:
                oversized.add(path)
                continue
        manifests[path] = content
    sizes = {path: size for path, size in sizes.items() if path in paths}
    return WorkflowFiles(frozenset(paths), manifests, frozenset(oversized), sizes)


async def expand(
    ctx: Context, as_: Identity, workflow: RunWorkflow, *, own: WorkflowId | None
) -> Expanded:
    """`workflow` expanded into primitive steps, read as `as_`. `own`, when
    a version is being made or a text is checked as a named workflow, is
    the first workflow on the path, so a box that uses it at any version
    is refused.
    """
    found: dict[str, PrimitiveDeclaration] = {}
    inners: dict[str, RunWorkflow] = {}
    pins: dict[str, int] = {}
    inner_files: dict[int, WorkflowFiles] = {}
    held: list[PinnedUse] = []
    seen: dict[str, _Use] = {}
    problems: list[Problem] = []
    blocked = False
    outer_index = 0

    async def open_use(ref: WorkflowRef) -> _Use:
        use = str(ref)
        if use in seen:
            return seen[use]
        declaration, problem = await primitives.declaration(ctx, as_, ref, workflows=True)
        if declaration is not None:
            found[use] = declaration
            opened = _Use("primitive")
        elif problem is not None:
            opened = _Use("bad", problem)
        else:
            opened = await open_box(ref)
        seen[use] = opened
        return opened

    async def open_box(ref: WorkflowRef) -> _Use:
        use = str(ref)
        try:
            workflow_id = await names.workflow_id(ctx, ref)
            at = await ctx.forge.workflows.workflow_version(as_, workflow_id, ref.version)
            file = await ctx.forge.workflows.read_workflow_at(as_, workflow_id, at, WORKFLOW_FILE)
            key = await ctx.forge.workflows.workflow_key(workflow_id)
        except NotFound, Forbidden:
            return _Use("bad", _CANNOT_OPEN.format(use=use))
        try:
            definition = parse_run_workflow(file.content)
        except InvalidDefinition as invalid:
            return _Use("bad", _OLD.format(use=use, detail=invalid.detail))
        pin = len(pins) + 1
        pins[use] = pin
        inners[use] = definition
        inner_files[pin] = await files_at(ctx, as_, workflow_id, at, definition)
        held.append(PinnedUse(use, workflow_id, at, key))
        return _Use("workflow", key=key)

    async def descend(definition: RunWorkflow, prefix: str, on_path: tuple[str, ...]) -> None:
        nonlocal blocked, outer_index
        for index, step in enumerate(definition.steps):
            if not prefix:
                outer_index = index
            step_path = f"{prefix}/{step.id}" if prefix else step.id
            use = str(step.use)
            fresh = use not in seen
            opened = await open_use(step.use)
            at = path_text(("steps", outer_index, "use"))
            if opened.kind == "workflow" and opened.key in on_path:
                problems.append(
                    Problem(
                        path=at,
                        message=(
                            f"`{step_path}` uses {step.use}, and {step.use.owner}/{step.use.name} "
                            "is already on that path, so the workflow would hold itself."
                        ),
                        step=step_path,
                    )
                )
                blocked = True
                continue
            if opened.kind == "bad":
                message = opened.message or ""
                if prefix:
                    message = f"`{step_path}`: {message}"
                problems.append(Problem(path=at, message=message))
                continue
            if opened.kind == "workflow" and fresh:
                assert opened.key is not None
                await descend(inners[use], step_path, (*on_path, opened.key))

    on_path: tuple[str, ...] = ()
    if own is not None:
        on_path = (await ctx.forge.workflows.workflow_key(own),)
    await descend(workflow, "", on_path)
    if len(seen) > MAX_USES:
        problem = over_cap(len(seen))
        if problem is not None:
            problems.append(problem)
        blocked = True
    if blocked:
        return Expanded(None, found, inner_files, problems, tuple(held))
    flat = flatten(workflow, inners, pins)
    if isinstance(flat, list):
        return Expanded(None, found, inner_files, [*problems, *flat], tuple(held))
    return Expanded(flat, found, inner_files, problems, tuple(held))
