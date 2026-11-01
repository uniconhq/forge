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
from forge.domain.identity import Identity
from forge.domain.ids import TaskId
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
    expected = asked.model_dump(exclude_none=True)
    if set(lock.platforms) != set(PLATFORMS):
        return None
    for languages in lock.platforms.values():
        if set(languages) != set(expected):
            return None
        if any(entry.asked != expected[language] for language, entry in languages.items()):
            return None
    return lock


def anticipated(
    snapshot: Snapshot, asked: Environment, entrant: Mapping[str, EntrantInput]
) -> Snapshot:
    """Compare unresolved requirements conservatively, never writing the provisional lock."""
    languages = {
        name: LanguageLock(
            asked=entry,
            picked="preparing",
            fingerprint=fingerprint([json.dumps(entry, sort_keys=True)]),
        )
        for name, entry in asked.model_dump(exclude_none=True).items()
    }
    lock = EnvironmentLock(platforms=dict.fromkeys(PLATFORMS, languages))
    plan = with_environment(RunPlan.from_bytes(snapshot.plans[PLAN_PATH]), lock, entrant)
    return replace(snapshot, plans={**snapshot.plans, PLAN_PATH: plan.to_bytes()})
