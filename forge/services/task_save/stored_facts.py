import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence

from forge.domain.content import FileSet, WrittenEdit, named_by
from forge.domain.definitions import TaskDefinition
from forge.domain.identity import Identity
from forge.domain.ids import TaskId
from forge.domain.run_plan import FileFact
from forge.domain.run_task import RUNS_PATH, RunTask
from forge.domain.run_workflow import WorkflowFiles
from forge.domain.uploads import upload_info
from forge.domain.workflow_definition import WorkflowRef
from forge.domain.yaml_models import Problem
from forge.runtime.context import Context
from forge.services import entrant_view
from forge.services.task_save import sources
from forge.services.task_save.sources import _content, _Found


async def _task_facts(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    present: set[str],
    named: Iterable[str],
) -> dict[str, FileFact]:
    """The size and hash of each task file `named` names, a folder's files
    each by itself, an upload's own: a file the save writes from what it
    writes, and the head's read in batches of at most `FACTS_BATCH` bytes,
    so the plan pins every file a step is given by its content.
    """
    paths = sorted({path for entry in named for path in named_by(present, entry)})
    facts: dict[str, FileFact] = {}
    unread: list[str] = []
    for path in paths:
        if path not in written:
            unread.append(path)
        elif (content := written[path].content) is not None:
            facts[path] = _fact_of(content)
    for batch in _batches(unread, head.sizes):
        blobs = await ctx.forge.content.read_blobs(as_, task, {head.tokens[path] for path in batch})
        for path in batch:
            content = blobs.get(head.tokens[path])
            if content is None:
                content = await _content(ctx, as_, task, path, head, written)
            if content is not None:
                facts[path] = _fact_of(content)
    return facts


FACTS_BATCH = 32 * 1024 * 1024
"""The most bytes of task files the save reads in one batch to hash them."""


def _batches(paths: Sequence[str], sizes: Mapping[str, int]) -> Iterator[list[str]]:
    """`paths` in order, in batches of at most `FACTS_BATCH` bytes by
    `sizes`, a file larger than that or of no known size in a batch alone.
    """
    batch: list[str] = []
    held = 0
    for path in paths:
        size = sizes.get(path, FACTS_BATCH)
        if batch and held + size > FACTS_BATCH:
            yield batch
            batch, held = [], 0
        batch.append(path)
        held += size
    if batch:
        yield batch


async def _workflow_facts(
    ctx: Context, as_: Identity, read: _Found, files: WorkflowFiles, named: Iterable[str]
) -> dict[str, FileFact]:
    """The size and hash of each workflow file `named` names at the commit
    the workflow was read at, as `_task_facts` gives a task file's, each
    read but the manifests the check already holds.
    """
    facts: dict[str, FileFact] = {}
    for path in sorted({path for entry in named for path in named_by(files.paths, entry)}):
        content = files.manifests.get(path)
        if content is None:
            found = await ctx.forge.workflows.read_workflow_at(as_, read.workflow, read.at, path)
            content = found.content
        facts[path] = _fact_of(content)
    return facts


def _fact_of(content: bytes) -> FileFact:
    """A file's size and SHA-256: an upload's own, from its pointer, and
    any other file's of its content.
    """
    info = upload_info(content)
    if info is not None:
        return FileFact(size=info.size, sha256=info.digest)
    return FileFact(size=len(content), sha256=hashlib.sha256(content).hexdigest())


RUNS_UNREAD = (
    f"The latest publication's {RUNS_PATH}, which keeps the id of each of the task's runs, is "
    "missing or does not read, so this save cannot keep those ids. Ask Unicon's operators to "
    "restore it."
)


def _marks_digest(task: RunTask | None) -> str:
    """A short digest of the columns `task` marks self-reported and the
    inputs that mark each, and of the values reading them and those `show`
    lists, which a confirmation of them carries back.
    """
    marks = task.self_reported if task is not None else {}
    values = sorted(task.self_reported_values()) if task is not None else []
    shown = sorted(task.self_reported_shown()) if task is not None else []
    text = json.dumps(
        [{name: sorted(inputs) for name, inputs in sorted(marks.items())}, values, shown]
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _workflow_problem(ref: WorkflowRef, problem: Problem) -> Problem:
    return Problem(
        path="workflow",
        message=f"The workflow {ref} has a problem ({problem['path']}): {problem['message']} "
        "Choose another version of it, or ask its owner to fix it.",
    )


async def _format_kept(ctx: Context, task: TaskId, definition: TaskDefinition) -> list[Problem]:
    """Once anyone has submitted, the task keeps the format of the
    publication it is graded on: a submission to one is laid out as the
    other cannot read.
    """
    publications = await entrant_view.live(ctx, task)
    if not publications:
        return []
    before = await sources._definition_of(ctx, task, publications[-1])
    if before is None or before.runs == definition.runs or not await sources._submitted(ctx, task):
        return []
    was = "scored by its runs" if before.runs else "graded test by test"
    return [
        Problem(
            path="workflow",
            message=f"Entrants have submitted to this task, which is {was}, so its workflow "
            "must keep that format. Choose a version that does.",
        )
    ]
