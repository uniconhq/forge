"""A contest's or a task's files, read and written through the port as the
organiser doing it, so the history of each file is theirs and the forge's own
permission check stays underneath the platform's. Reading needs the observer
role at the place and writing the manager role.

Every write carries the token the file was read with, and a file that has
moved since is `Conflict`, with nothing written. A write to a contest's
`contest.yaml` is validated first and refused whole when it is not valid,
or when it moves a task's timeline behind what rows already did or puts a
worth or a due on a task that gives no points (`timelines.check_contest`),
or when a board asks what a covered task's latest publication does not give
or a task's marks fall below what a row holds (`boards.check_contest`),
and a manager's change to one of its admin-only keys is refused naming each.
One that is written answers with what C4 reports of its boards beside the
version, as a task's save answers T9's (`Written`). A
write to a task is a save of the task, which publishes it when it is valid
(`publications.save`). A rollback is not an undo: the file as it was at the
chosen version is written back as a new change through the same write, so a
task's rollback is a save too and the history stays whole.

A file the organiser uploaded is held in the commit as a pointer to its
bytes in the forge's large-file store, and is read and listed as an upload,
with the size and digest of what it holds, so it is never opened as text;
it is changed by uploading it again (`write_upload`). Every read names a
version holding the file as read, and `download` opens the door to any file
of a task at such a version, typed or uploaded, for the proxy to stream it
to the organiser from the forge's store whatever its size.
"""

import re
import uuid
from dataclasses import dataclass, replace

from forge.domain.content import (
    Change,
    ConflictToken,
    Edit,
    EntryKind,
    File,
    TreeEntry,
    Uploaded,
    check_path,
)
from forge.domain.definitions import (
    CONTEST_FILE,
    ContestDefinition,
    admin_only_changes,
    admin_only_refusal,
    parse_contest,
)
from forge.domain.errors import AdminOnly, NotFound, PortError
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Role, ScopeKind, holds, scope_of_place, task_scope
from forge.domain.uploads import Door, may_be_pointer, refuse_pointer, upload_info
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import boards, publications, published, timelines
from forge.services.access import Organiser, require
from forge.services.publications import Draft, Published

log = get_logger(__name__)

Place = ContestId | TaskId

COMMIT = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
"""A commit's id, as a file read gives its version: SHA-1's or SHA-256's."""
NO_SUCH_VERSION = "The task has no such version."


@dataclass(frozen=True, slots=True)
class Written:
    """A contest's file written: the version the change made, and, for
    `contest.yaml`, each board's notes on the tasks it covers, such as one
    that counts nothing from a task; the organisers' reading of the boards
    carries the same notes beside each.
    """

    version: VersionId
    notes: tuple[str, ...] = ()


@action
async def read(
    ctx: Context, organiser: Organiser, place: Place, path: str, at: VersionId | None = None
) -> File:
    """One file at the latest version, or at `at`, with the token a write
    presents back, saying so when it is an upload: its content is then the
    pointer, and the file's own bytes are in the forge's large-file store.
    """
    require(organiser, scope_of_place(place), Role.OBSERVER)
    found = await ctx.forge.content.read_file(organiser.identity, place, check_path(path), at=at)
    return replace(found, upload=upload_info(found.content))


@action
async def download(
    ctx: Context, organiser: Organiser, task: TaskId, path: str, at: VersionId
) -> Door:
    """Where one file of the task is read whole at the version `at`, a
    commit's id as a file read gives it, and what to present there as the
    organiser, for the proxy to fetch it and stream it to them: a typed file
    and an upload alike, an upload as the bytes its pointer names, whatever
    its size. Any file of the task, `plans/` included, which an observer
    reads anyway. Needs the observer role reaching the task; `InvalidPath`
    for a path that is not plain and `NotFound` for a version that is not a
    commit's id, which the forge would read as a name. Whether the file is
    there at that version is the forge's to answer when the proxy asks.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    if COMMIT.fullmatch(at) is None:
        raise NotFound(NO_SUCH_VERSION)
    door = await ctx.forge.content.media_door(organiser.identity, task, check_path(path), at=at)
    log.info("files.download_opened", task=task, user_id=organiser.user.id)
    return door


@action
async def tree(
    ctx: Context, organiser: Organiser, place: Place, path: str = ""
) -> tuple[TreeEntry, ...]:
    """The files and folders directly under `path`, each file that is an
    upload with the size and digest of what it holds. Only a file whose size
    is a pointer's is read to tell (`uploads.may_be_pointer`), so a folder of
    large files or of typed ones costs no read but the listing.
    """
    require(organiser, scope_of_place(place), Role.OBSERVER)
    entries = await ctx.forge.content.list_tree(
        organiser.identity, place, check_path(path) if path else ""
    )
    return tuple([await _with_upload(ctx, organiser, place, entry) for entry in entries])


async def _with_upload(
    ctx: Context, organiser: Organiser, place: Place, entry: TreeEntry
) -> TreeEntry:
    if entry.kind is not EntryKind.FILE or not may_be_pointer(entry.size):
        return entry
    try:
        found = await ctx.forge.content.read_file(organiser.identity, place, entry.path)
    except NotFound:
        return entry
    return replace(entry, upload=upload_info(found.content))


@action
async def history(
    ctx: Context, organiser: Organiser, place: Place, path: str | None = None
) -> tuple[Change, ...]:
    """Every change to the place, or to one file in it, newest first, each
    with who made it, by id and username, and when. A version's author is
    named whether or not they still hold a role at the place, since a
    history that cannot name its authors is no true record; only those who
    may read the history read the names in it, and no other route names a
    user by id. The forge names most authors itself, as the history comes;
    one it gives an id for and no name is looked up once.
    """
    require(organiser, scope_of_place(place), Role.OBSERVER)
    changes = await ctx.forge.content.history(
        organiser.identity, place, check_path(path) if path is not None else None
    )
    unnamed = dict.fromkeys(
        change.author_id
        for change in changes
        if change.author_id is not None and change.author is None
    )
    if not unnamed:
        return changes
    await ctx.let_go()
    names: dict[int, str | None] = {}
    for author in unnamed:
        try:
            names[author] = (await ctx.forge.identity.find_user(author)).username
        except PortError:
            names[author] = None
    return tuple(
        replace(change, author=names[change.author_id]) if change.author_id in names else change
        for change in changes
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
) -> Written | Published | Draft:
    """Write one file, `expected` being the token it was read with or none
    for a new file. A contest's file comes back as the version written, with
    its boards' notes; a task's as the save's publication or draft, `confirm` and `keep_as_draft`
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
    notes: list[str] = []
    if path == CONTEST_FILE:
        after = parse_contest(content)
        before = await _current(ctx, organiser, contest, path)
        if not holds(organiser.grants, scope, Role.ADMIN):
            keys = admin_only_changes("contest", before, content)
            if keys:
                log.info("files.refused", place=place, keys=keys, user_id=organiser.user.id)
                raise AdminOnly(admin_only_refusal(scope.name, keys), keys=keys)
        await timelines.hold_rules(ctx, contest)
        standing = await _last_read(ctx, organiser, contest, before)
        problems = await timelines.check_contest(ctx, contest, standing, after)
        checked = await boards.check_contest(ctx, contest, after)
        problems += checked.problems
        notes = checked.notes
        if problems:
            raise InvalidDefinition(CONTEST_FILE, problems)
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
    return Written(version, tuple(notes))


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
) -> Written | Published | Draft:
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


SETTINGS_LOOKED_BACK = 50
"""How many earlier versions of `contest.yaml` a save looks back through for
settings that read, when the ones it replaces do not."""


async def _last_read(
    ctx: Context, organiser: Organiser, contest: ContestId, before: bytes | None
) -> ContestDefinition | None:
    """The contest's settings a save is checked against (C2, #438): the
    ones it replaces, or, when those do not read, the latest earlier
    version's that do, since what entrants already have does not depend on
    whether yesterday's file reads; none for a contest whose settings never
    read, or a new file.
    """
    found = _parsed(before)
    if found is not None or before is None:
        return found
    changes = await ctx.forge.content.history(organiser.identity, contest, CONTEST_FILE)
    for change in changes[1 : SETTINGS_LOOKED_BACK + 1]:
        try:
            older = await ctx.forge.content.read_file(
                organiser.identity, contest, CONTEST_FILE, at=change.version
            )
        except NotFound:
            continue
        found = _parsed(older.content)
        if found is not None:
            return found
    return None


def _parsed(content: bytes | None) -> ContestDefinition | None:
    """The contest's settings as they stood, or none when they did not read."""
    if content is None:
        return None
    try:
        return parse_contest(content)
    except InvalidDefinition:
        return None


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
