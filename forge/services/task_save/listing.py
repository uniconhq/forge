from collections.abc import Mapping
from dataclasses import replace

from forge.domain.content import Edit, Uploaded, WrittenEdit
from forge.domain.errors import InvalidInputs, UploadNotReady
from forge.domain.ids import TaskId
from forge.domain.publication_record import Publication
from forge.domain.roles import Role, task_scope
from forge.domain.uploads import (
    ATTRIBUTES_FILE,
    UploadStatus,
    pointer_text,
    read_pointer,
    refuse_pointer,
)
from forge.port.uploads import TaskPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import pipelines, uploads
from forge.services.access import Organiser, require


@action
async def list_publications(
    ctx: Context, organiser: Organiser, task: TaskId
) -> tuple[Publication, ...]:
    """Every publication of the task, oldest first, each saying whether it
    changed how the task grades, and one that has not taken over where it
    stands (`pipelines.checks`), and one refused each check that
    failed it. Needs the observer role at the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    made = await ctx.forge.workspaces.list_publications(task)
    found = await pipelines.checks(ctx, task, made)
    failed = await pipelines.failed(
        ctx, task, [name for name, check in found.items() if check == "refused"]
    )
    return tuple(
        replace(each, check=found.get(each.id), failed_checks=failed.get(each.id, ()))
        for each in made
    )


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
    a rollback or a file saved back unchanged writes. A removal passes as
    it is.

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
        if edit.content is None:
            resolved[path] = WrittenEdit(None, edit.token)
            continue
        if isinstance(edit.content, Uploaded):
            row = rows.get(edit.content.upload)
            if row is None or row.status not in (UploadStatus.WAITING, UploadStatus.VERIFIED):
                raise InvalidInputs(
                    "That upload is not one of yours for this task.",
                    errors=[{"input": path, "message": "There is no such upload."}],
                )
            if row.repo_path != path:
                raise InvalidInputs(
                    f"That upload was made for {row.repo_path}, not {path}. Upload the file "
                    "again here.",
                    errors=[{"input": path, "message": "This upload was made for another file."}],
                )
            if not await uploads.holds(ctx, TaskPlace(task), organiser.identity, row):
                raise UploadNotReady(
                    "The file has not finished uploading. Save again once it has.",
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
