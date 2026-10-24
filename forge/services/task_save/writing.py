from collections.abc import Collection, Mapping

from forge.domain.content import ConflictToken, FileSet, WrittenEdit
from forge.domain.definitions import CONTEST_FILE, ContestDefinition, parse_contest
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import TaskId, VersionId
from forge.domain.port_values import is_reserved
from forge.domain.release import has_started
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services.access import Organiser
from forge.services.task_save.records import Checked, Draft
from forge.services.task_save.sources import _content

log = get_logger(__name__)


async def _contest(ctx: Context, task: TaskId) -> ContestDefinition | None:
    """The settings of the task's contest, read as the platform, since an
    organiser of the task alone may not read the contest; none when they do
    not read.
    """
    contest = contest_id_of(task_scope(task))
    try:
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
        return parse_contest(found.content)
    except NotFound, InvalidDefinition:
        return None


async def _contest_running(ctx: Context, task: TaskId) -> bool:
    """Whether the task's contest has started and is not archived."""
    contest = await _contest(ctx, task)
    return contest is not None and has_started(contest, ctx.now)


async def _plan_files(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, checked: Checked
) -> dict[str, bytes | None]:
    """The plan the save writes, or a run task's runs file, when it is new
    or differs from the head's, and none for every other file under
    `plans/` at the head, which the save removes.
    """
    compiled = checked.plan_files()
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
    organiser: Organiser,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    plans: Mapping[str, bytes | None],
    message: str,
) -> VersionId:
    """Write the organiser's files and the plans as one change, and return
    its version; with nothing to write, the head is the version. The
    platform writes it, the only account a task's files are written by at
    the forge (#443), with the organiser as its author, so the history is
    theirs; `save` has checked they may.
    """
    files: dict[str, bytes | None] = {path: edit.content for path, edit in written.items()}
    files.update(plans)
    if not files:
        return head.version
    expected: dict[str, ConflictToken | None] = {path: head.tokens.get(path) for path in plans}
    expected.update({path: edit.token for path, edit in written.items()})
    return await ctx.forge.content.save_files(
        PLATFORM, task, files, expected=expected, message=message, author=organiser.user.id
    )


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
