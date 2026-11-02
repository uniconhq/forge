"""Read the platform-written lock and wire each exec step's own languages."""

import json
from collections.abc import Mapping
from dataclasses import replace

from forge.domain.content import FileSet, WrittenEdit
from forge.domain.environment import (
    LOCK_PATH,
    PLATFORMS,
    Environment,
    EnvironmentLock,
    LanguageLock,
    fingerprint,
)
from forge.domain.environment_plan import with_environment
from forge.domain.errors import NotFound
from forge.domain.identity import Identity
from forge.domain.ids import TaskId, VersionId
from forge.domain.plan_basics import PLAN_PATH, Snapshot
from forge.domain.run_plan import RunPlan
from forge.domain.run_task.models import EntrantInput
from forge.runtime.context import Context
from forge.services.task_save.sources import _content


async def matching(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    asked: Environment | None,
) -> EnvironmentLock | None:
    if asked is None:
        return None
    raw = await _content(ctx, as_, task, LOCK_PATH, head, written)
    if raw is None:
        return None
    try:
        lock = EnvironmentLock.model_validate_json(raw)
    except ValueError:
        return None
    expected = asked.requirements()
    if set(lock.platforms) != set(PLATFORMS):
        return None
    for languages in lock.platforms.values():
        if set(languages) != set(expected):
            return None
        if any(
            not entry.conda or entry.asked != expected[language]
            for language, entry in languages.items()
        ):
            return None
    return lock


def anticipated(
    snapshot: Snapshot,
    asked: Environment,
    entrant: Mapping[str, EntrantInput],
    previous: EnvironmentLock | None = None,
) -> Snapshot:
    """Compare unresolved requirements conservatively, never writing the provisional lock."""
    languages = {
        name: LanguageLock(
            asked=entry,
            picked="preparing",
            fingerprint=fingerprint([json.dumps(entry, sort_keys=True)]),
        )
        for name, entry in asked.requirements().items()
    }
    lock = EnvironmentLock(
        platforms={
            platform: {
                name: old
                if (
                    previous is not None
                    and (old := previous.platforms.get(platform, {}).get(name)) is not None
                    and old.conda
                    and old.asked == entry.asked
                )
                else entry
                for name, entry in languages.items()
            }
            for platform in PLATFORMS
        }
    )
    plan = with_environment(RunPlan.from_bytes(snapshot.plans[PLAN_PATH]), lock, entrant)
    return replace(snapshot, plans={**snapshot.plans, PLAN_PATH: plan.to_bytes()})


async def at(
    ctx: Context, as_: Identity, task: TaskId, version: VersionId
) -> EnvironmentLock | None:
    try:
        file = await ctx.forge.content.read_file(as_, task, LOCK_PATH, at=version)
        return EnvironmentLock.model_validate_json(file.content)
    except NotFound, ValueError:
        return None
