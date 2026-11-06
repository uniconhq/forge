"""A contest's or a task's settings as their form reads and saves them: the
settings file's data as plain values, and a contest's values written into
`contest.yaml` as it is, through the same write as the file's own save. A
task's values are saved with the task (`task_save.save` with `Values`), and
the runs a draft's `repeat` makes are answered for its cards.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from forge.domain.content import ConflictToken
from forge.domain.definitions import CONTEST_FILE, TASK_FILE
from forge.domain.errors import Conflict, NotFound
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Role, ScopeKind, contest_scope, scope_of_place, task_scope
from forge.domain.run_task.draft_runs import DraftRuns
from forge.domain.run_task.draft_runs import draft_runs as runs_of
from forge.domain.settings_values import NotValues, values_of, written_values
from forge.domain.yaml_models import YAMLError, load_yaml
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import files
from forge.services.access import Organiser, require


@dataclass(frozen=True, slots=True)
class FormValues:
    """The settings as values with the token a save presents back, or, when
    the file does not read as settings, none and why."""

    values: dict[str, Any] | None
    unreadable: str | None
    token: ConflictToken | None
    version: VersionId | None


@action
async def read(ctx: Context, organiser: Organiser, place: ContestId | TaskId) -> FormValues:
    """The settings of a contest or a task. Needs the observer role there."""
    scope = scope_of_place(place)
    require(organiser, scope, Role.OBSERVER)
    task = scope.kind is ScopeKind.TASK
    path = TASK_FILE if task else CONTEST_FILE
    try:
        found = await files.read(ctx, organiser, place, path)
    except NotFound:
        return FormValues({}, None, None, None)
    try:
        data = load_yaml(found.content)
        values = values_of("task" if task else "contest", data)
    except (YAMLError, NotValues) as error:
        return FormValues(None, str(error), found.token, found.version)
    return FormValues(values, None, found.token, found.version)


@action
async def write_contest(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    values: dict[str, Any],
    expected: ConflictToken | None,
    *,
    confirm: bool = False,
    asked: str | None = None,
) -> files.Written:
    """Write the values into `contest.yaml` as it is and save it as its own
    save does (`files.write`). Needs the manager role at the contest; a
    token that has moved is `Conflict`."""
    require(organiser, contest_scope(contest), Role.MANAGER)
    try:
        current = await ctx.forge.content.read_file(organiser.identity, contest, CONTEST_FILE)
    except NotFound:
        current = None
    if (current.token if current is not None else None) != expected:
        raise Conflict(f"{CONTEST_FILE} has changed since it was read.")
    old = current.content if current is not None else b""
    content = await asyncio.to_thread(written_values, "contest", old, values)
    written = await files.write(
        ctx, organiser, contest, CONTEST_FILE, content, expected, confirm=confirm, asked=asked
    )
    assert isinstance(written, files.Written)
    return written


@action
async def draft_runs(ctx: Context, organiser: Organiser, task: TaskId, repeat: object) -> DraftRuns:
    """The runs the task's draft `repeat` makes. Needs the observer role at
    the task."""
    require(organiser, task_scope(task), Role.OBSERVER)
    return runs_of(repeat)
