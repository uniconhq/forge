"""A contest's or a task's files, read and written through the port as the
organiser doing it, so the history of each file is theirs and the forge's own
permission check stays underneath the platform's. Reading needs the observer
role at the place and writing the manager role.

Every write carries the token the file was read with, and a file that has
moved since is `Conflict`, with nothing written. A write to a contest's
`contest.yaml` is validated first and refused whole when it is not valid, and
a manager's change to one of its admin-only keys is refused naming each. A
write to a task is a save of the task, which publishes it when it is valid
(`publications.save`). A rollback is not an undo: the file as it was at the
chosen version is written back as a new change through the same write, so a
task's rollback is a save too and the history stays whole.
"""

import uuid

from forge.domain.content import (
    Change,
    ConflictToken,
    Edit,
    File,
    TreeEntry,
    Uploaded,
    check_path,
)
from forge.domain.definitions import CONTEST_FILE, admin_only_changes, parse_contest
from forge.domain.errors import AdminOnly, NotFound
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Role, ScopeKind, holds, scope_of_place, task_scope
from forge.domain.uploads import refuse_pointer
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import publications, published
from forge.services.access import Organiser, require
from forge.services.publications import Draft, Published

log = get_logger(__name__)

Place = ContestId | TaskId


@action
async def read(
    ctx: Context, organiser: Organiser, place: Place, path: str, at: VersionId | None = None
) -> File:
    """One file at the latest version, or at `at`, with the token a write
    presents back.
    """
    require(organiser, scope_of_place(place), Role.OBSERVER)
    return await ctx.forge.content.read_file(organiser.identity, place, check_path(path), at=at)


@action
async def tree(
    ctx: Context, organiser: Organiser, place: Place, path: str = ""
) -> tuple[TreeEntry, ...]:
    """The files and folders directly under `path`."""
    require(organiser, scope_of_place(place), Role.OBSERVER)
    return await ctx.forge.content.list_tree(
        organiser.identity, place, check_path(path) if path else ""
    )


@action
async def history(
    ctx: Context, organiser: Organiser, place: Place, path: str | None = None
) -> tuple[Change, ...]:
    """Every change to the place, or to one file in it, newest first, each
    with who made it and when.
    """
    require(organiser, scope_of_place(place), Role.OBSERVER)
    return await ctx.forge.content.history(
        organiser.identity, place, check_path(path) if path is not None else None
    )


@action
async def write(
    ctx: Context,
    organiser: Organiser,
    place: Place,
    path: str,
    content: bytes,
    expected: ConflictToken | None,
    *,
    message: str | None = None,
    confirm: bool = False,
    keep_as_draft: bool = False,
) -> VersionId | Published | Draft:
    """Write one file, `expected` being the token it was read with or none
    for a new file. A contest's file comes back as the version written; a
    task's as the save's publication or draft, `confirm` and `keep_as_draft`
    doing what they do for a save.
    """
    scope = scope_of_place(place)
    require(organiser, scope, Role.MANAGER)
    check_path(path)
    if scope.kind is ScopeKind.TASK:
        return await publications.save(
            ctx,
            organiser,
            TaskId(place),
            {path: Edit(content, expected)},
            confirm=confirm,
            keep_as_draft=keep_as_draft,
            message=message,
        )
    contest = ContestId(place)
    refuse_pointer(path, content)
    if path == CONTEST_FILE:
        parse_contest(content)
        if not holds(organiser.grants, scope, Role.ADMIN):
            before = await _current(ctx, organiser, contest, path)
            keys = admin_only_changes("contest", before, content)
            if keys:
                log.info("files.refused", place=place, keys=keys, user_id=organiser.user.id)
                raise AdminOnly(
                    f"Only an admin of {scope.name} may change {', '.join(keys)}.", keys=keys
                )
    version = await ctx.forge.content.write_file(
        organiser.identity,
        contest,
        path,
        content,
        message=message or f"Update {path}",
        expected=expected,
    )
    if path == CONTEST_FILE:
        published.forget_contests(ctx)
    log.info("files.written", place=place, path=path, version=version, user_id=organiser.user.id)
    return version


@action
async def rollback(
    ctx: Context,
    organiser: Organiser,
    place: Place,
    path: str,
    version: VersionId,
    expected: ConflictToken | None,
    *,
    message: str | None = None,
    confirm: bool = False,
    keep_as_draft: bool = False,
) -> VersionId | Published | Draft:
    """Write the file as it was at `version` back as a new change, through
    `write`, `expected` being the token of the file as it is now. `NotFound`
    when the file was not there at that version.
    """
    require(organiser, scope_of_place(place), Role.MANAGER)
    old = await ctx.forge.content.read_file(organiser.identity, place, check_path(path), at=version)
    written = await write(
        ctx,
        organiser,
        place,
        path,
        old.content,
        expected,
        message=message or f"Roll back {path} to {version}",
        confirm=confirm,
        keep_as_draft=keep_as_draft,
    )
    log.info("files.rolled_back", place=place, path=path, to=version, user_id=organiser.user.id)
    return written


async def _current(ctx: Context, organiser: Organiser, place: ContestId, path: str) -> bytes | None:
    try:
        return (await ctx.forge.content.read_file(organiser.identity, place, path)).content
    except NotFound:
        return None


@action
async def write_upload(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    path: str,
    upload: uuid.UUID,
    expected: ConflictToken | None,
    *,
    message: str | None = None,
    confirm: bool = False,
    keep_as_draft: bool = False,
) -> Published | Draft:
    """Put a file the organiser uploaded into the task at `path`, as a save
    like any other: the commit carries the pointer to it beside the
    organiser's other files and the compiled plans, so a task is never half
    changed. The bytes went to the forge before this, through the upload
    door (`services.uploads.task_file_slot`).

    Only a task takes one. A contest's files are the ones people type, and
    nothing it holds is large enough to need the store.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    check_path(path)
    return await publications.save(
        ctx,
        organiser,
        task,
        {path: Edit(Uploaded(upload), expected)},
        confirm=confirm,
        keep_as_draft=keep_as_draft,
        message=message,
    )
