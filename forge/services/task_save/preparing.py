"""Background preparation owns no publication until its current row is ready."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from forge.db.tables import EnvironmentPreparation
from forge.domain.environment import LOCK_PATH, Environment, EnvironmentLock, PreparationStatus
from forge.domain.errors import NotFound, PortError, Rejected, TooLarge
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId, VersionId
from forge.domain.roles import Role, task_scope
from forge.log import get_logger
from forge.port.uploads import PackagePlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services.access import Organiser, require
from forge.services.task_save.records import Preparing, Published
from forge.services.task_save.writing import _landed_under

log = get_logger(__name__)

LIMIT = timedelta(minutes=15)
INTERRUPTED = "Preparing the packages was interrupted; save again"
FAILED = "Preparing the packages failed; save again"
AT_ONCE = 2
ROOM = asyncio.Semaphore(AT_ONCE)
"""The process's preparations downloading and storing at once; the rest wait."""


class _Replaced(Exception):
    """A later save replaced the preparation while it ran."""


@dataclass(frozen=True, slots=True)
class Preparation:
    id: uuid.UUID
    status: PreparationStatus
    reason: str | None
    deadline_at: datetime
    result: dict[str, object] | None


def _expire(ctx: Context, row: EnvironmentPreparation) -> None:
    if row.status == "preparing" and ctx.now > row.deadline_at + timedelta(minutes=1):
        row.status, row.reason = "refused", INTERRUPTED


async def replace(ctx: Context, task: TaskId) -> None:
    rows = (
        await ctx.db.execute(
            select(EnvironmentPreparation)
            .where(
                EnvironmentPreparation.task_id == str(task),
                EnvironmentPreparation.status == "preparing",
            )
            .with_for_update()
        )
    ).scalars()
    for row in rows:
        _expire(ctx, row)
        if row.status == "preparing":
            row.status = "replaced"


@action
async def read(ctx: Context, organiser: Organiser, task: TaskId) -> Preparation:
    require(organiser, task_scope(task), Role.MANAGER)
    row = (
        await ctx.db.execute(
            select(EnvironmentPreparation)
            .where(EnvironmentPreparation.task_id == str(task))
            .order_by(EnvironmentPreparation.created_at.desc(), EnvironmentPreparation.id.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("This task has no package preparation.")
    _expire(ctx, row)
    return Preparation(row.id, row.status, row.reason, row.deadline_at, row.result)


async def start(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    version: VersionId,
    asked: Environment,
    publish: Callable[[Context, EnvironmentLock, VersionId], Awaitable[Published]],
    *,
    previous: EnvironmentLock | None = None,
) -> Preparing:
    row = EnvironmentPreparation(
        task_id=str(task),
        version=str(version),
        owner_user_id=organiser.user.id,
        status="preparing",
        reason=None,
        deadline_at=ctx.now + LIMIT,
        result=None,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    identity = row.id

    async def later(later: Context) -> None:
        async def proceed() -> None:
            # Unlocked, so a later save replacing the row never waits on it,
            # and committed, so no connection is held between downloads.
            status = await later.db.scalar(
                select(EnvironmentPreparation.status).where(EnvironmentPreparation.id == identity)
            )
            await later.db.commit()
            if status != "preparing":
                raise _Replaced

        try:
            # Only resolving, downloading and storing the pieces is limited: a
            # limit firing once Forgejo holds the publication, before the commit,
            # would leave it without its pipeline and regrade rows.
            async with (
                asyncio.timeout(LIMIT.total_seconds()),
                ROOM,
                later.forge.packages.prepare(
                    asked, cap=later.settings.environment_max, previous=previous, proceed=proceed
                ) as prepared,
            ):
                for digest, path in prepared.files.items():
                    await proceed()
                    size = path.stat().st_size
                    if not await later.forge.uploads.holds(
                        PackagePlace(), as_=PLATFORM, digest=digest, size=size
                    ):
                        await later.forge.uploads.store(
                            PackagePlace(),
                            as_=PLATFORM,
                            digest=digest,
                            size=size,
                            content=_chunks(path),
                        )
                    if not await later.forge.uploads.holds(
                        PackagePlace(), as_=PLATFORM, digest=digest, size=size
                    ):
                        raise Rejected("A package was not held by the large-file store.")
                lock = prepared.lock
            current = await _current(later, identity)
            if current is None:
                return
            head = await later.forge.content.list_files(PLATFORM, task)
            if str(head.version) != current.version:
                current.status = "replaced"
                return
            if not await later.forge.content.may_write(task, organiser.user.id):
                raise Rejected("You no longer have permission to write this task.")
            locked = await later.forge.content.save_files(
                PLATFORM,
                task,
                {LOCK_PATH: lock.to_bytes()},
                expected={LOCK_PATH: head.tokens.get(LOCK_PATH)},
                message="Prepare packages",
                author=organiser.user.id,
            )
            if await _landed_under(later, PLATFORM, task, head, locked, {LOCK_PATH}):
                raise Rejected("The task changed while its packages were prepared. Save again.")
            # The row stays locked through both writes. A later save waits,
            # then replaces this preparation before changing the task.
            current = await _current(later, identity)
            if current is None:
                return
            result = await publish(later, lock, locked)
            current.status = "ready"
            current.result = json.loads(json.dumps(asdict(result), default=str))
        except _Replaced:
            return
        except (PortError, TooLarge, TimeoutError) as error:
            await later.db.rollback()
            current = await _current(later, identity)
            if current is not None:
                current.status = "refused"
                current.reason = (
                    "Preparing the packages took longer than 15 minutes"
                    if isinstance(error, TimeoutError)
                    else str(error)
                )[:4000]
        except Exception:
            log.exception("preparations.failed", task=task, preparation=identity)
            await later.db.rollback()
            current = await _current(later, identity)
            if current is not None:
                current.status, current.reason = "refused", FAILED

    ctx.in_background(later)
    return Preparing(row.id, version)


async def _current(ctx: Context, identity: uuid.UUID) -> EnvironmentPreparation | None:
    row = (
        await ctx.db.execute(
            select(EnvironmentPreparation)
            .where(EnvironmentPreparation.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    _expire(ctx, row)
    return row if row.status == "preparing" else None


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as stream:
        while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
            yield chunk
