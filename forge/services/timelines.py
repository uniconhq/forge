"""A task's timeline against what rows did: each row's extension, when a task
reveals, and the checks that keep a timeline from moving behind a row
(TASK-FORMAT.md sections 1.1 and 2, C1 and C2).

A row is a contestant, or a team while a contestant works in one. Its
extension sits on its row: on its `contestants` row while it works alone, on
its `teams` row while it is in a team. It is a length and the tasks it is
for, by name, every task when it names none, and it moves the row's due and
close on those tasks, and so those tasks' reveal for everyone. The
extensions in force are those of approved contestants who are in no team,
and of teams with an approved member; a removed contestant's, or a team
member's own, moves nothing.

A contest save is checked against every row's submissions, read from the
gradings, one per submission, at the moment it was taken:

- C1 across files: every entry of `tasks` names a task of the contest.
  `worth`, and `due` with `late_per_day`, are refused on a task whose latest
  publication gives no points. A task with no publication yet is not
  judged, since its first save says what it gives.
- C2, for what happened: a task released under the settings saved before,
  its contest published and past its start and its `release_at` passed,
  does not move its release later, nor lose its entry; a task with
  submissions does not lose its entry either. A `due` does not move earlier
  past a submission it would make late. A close, the task's `closes` or the
  contest's `end`, does not move earlier than a submission already made,
  nor later once the task has revealed under the settings saved before. A
  contest that was never published released nothing, and moves freely.

Changing an extension is checked the same way (`refuse_extension`): it may
not let a row submit to a task whose reveal has passed, nor, shortened or
withdrawn, leave a submission after the due or the close it was made
before.
"""

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select, text

from forge.db.tables import Contestant, Grading, Team, TeamMember
from forge.domain.definitions import ContestDefinition, ContestTask
from forge.domain.errors import InvalidExtension
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import TeamOwner, UserOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.release import Extension, close_of, due_of, released, reveal_of
from forge.domain.teams import MemberStatus
from forge.domain.yaml_models import Problem
from forge.runtime.context import Context
from forge.services import names, published

RULES_LOCK = 0x52554C45


async def hold_rules(ctx: Context, contest: ContestId, *, shared: bool = False) -> None:
    """Hold the contest's rules until the unit of work ends: alone, for a
    save of its settings or of one of its tasks, so the checks each makes
    against the other (C1, C4, T8) read what the other left; shared, for a
    change to a row's marks, so C1's count of the marks rows hold is not
    passed by a mark in flight.
    """
    lock = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await ctx.db.execute(
        text(f"SELECT {lock}(:space, hashtext(:contest))"),
        {"space": RULES_LOCK, "contest": contest},
    )


def extension_of(length_seconds: int, tasks: Sequence[str] | None) -> Extension:
    """An extension as a row keeps it."""
    return Extension(
        timedelta(seconds=length_seconds), frozenset(tasks) if tasks is not None else None
    )


def of_contestant(row: Contestant | None) -> Extension:
    """A contestant's own extension, none without a row."""
    if row is None:
        return Extension()
    return extension_of(row.time_extension_seconds, row.extension_tasks)


async def of_owner(ctx: Context, contest: ContestId, owner: WorkspaceOwner) -> Extension:
    """The extension of the row that owns a workspace."""
    if isinstance(owner, TeamOwner):
        team = await ctx.db.get(Team, owner.team_id)
        return (
            extension_of(team.time_extension_seconds, team.extension_tasks) if team else Extension()
        )
    assert isinstance(owner, UserOwner)
    row = await ctx.db.scalar(
        select(Contestant).where(
            Contestant.contest_id == contest, Contestant.user_id == owner.user_id
        )
    )
    return of_contestant(row)


async def of_owners(
    ctx: Context, contest: ContestId, owners: Collection[WorkspaceOwner]
) -> dict[WorkspaceOwner, Extension]:
    """`of_owner` for every one of `owners`, in two reads."""
    found: dict[WorkspaceOwner, Extension] = {owner: Extension() for owner in owners}
    team_ids = sorted(owner.team_id for owner in owners if isinstance(owner, TeamOwner))
    user_ids = sorted(owner.user_id for owner in owners if isinstance(owner, UserOwner))
    if team_ids:
        for team_id, seconds, tasks in await ctx.db.execute(
            select(Team.id, Team.time_extension_seconds, Team.extension_tasks).where(
                Team.id.in_(team_ids)
            )
        ):
            found[TeamOwner(team_id)] = extension_of(seconds, tasks)
    if user_ids:
        for row in await ctx.db.scalars(
            select(Contestant).where(
                Contestant.contest_id == contest, Contestant.user_id.in_(user_ids)
            )
        ):
            found[UserOwner(row.user_id)] = of_contestant(row)
    return found


def _members(contest: ContestId) -> Select[int, uuid.UUID]:
    """Each approved contestant of the contest who is a member of a team,
    with the team: the members that make a team one of the rows.
    """
    return (
        select(TeamMember.user_id, TeamMember.team_id)
        .join(
            Contestant,
            (Contestant.contest_id == TeamMember.contest_id)
            & (Contestant.user_id == TeamMember.user_id),
        )
        .where(
            TeamMember.contest_id == contest,
            TeamMember.status == MemberStatus.MEMBER,
            Contestant.status == Status.APPROVED,
        )
    )


async def rows(ctx: Context, contest: ContestId) -> list[WorkspaceOwner]:
    """Every row of the contest, the ones `every_extension` reads: each
    approved contestant in no team, and each team with an approved member.
    """
    members = _members(contest)
    people = await ctx.db.scalars(
        select(Contestant.user_id).where(
            Contestant.contest_id == contest,
            Contestant.status == Status.APPROVED,
            Contestant.user_id.not_in(members.with_only_columns(TeamMember.user_id)),
        )
    )
    teams_with_members = await ctx.db.scalars(
        select(Team.id).where(
            Team.contest_id == contest,
            Team.id.in_(members.with_only_columns(TeamMember.team_id)),
        )
    )
    owners: list[WorkspaceOwner] = [UserOwner(user_id) for user_id in people]
    owners += [TeamOwner(team_id) for team_id in teams_with_members]
    return owners


async def every_extension(ctx: Context, contest: ContestId) -> list[Extension]:
    """Every extension in force in the contest that is more than nothing:
    each approved contestant's who is in no team, and each team's that has
    an approved member.
    """
    members = _members(contest)
    people = await ctx.db.execute(
        select(Contestant.time_extension_seconds, Contestant.extension_tasks).where(
            Contestant.contest_id == contest,
            Contestant.status == Status.APPROVED,
            Contestant.time_extension_seconds > 0,
            Contestant.user_id.not_in(members.with_only_columns(TeamMember.user_id)),
        )
    )
    teams = await ctx.db.execute(
        select(Team.time_extension_seconds, Team.extension_tasks).where(
            Team.contest_id == contest,
            Team.time_extension_seconds > 0,
            Team.id.in_(members.with_only_columns(TeamMember.team_id)),
        )
    )
    return [extension_of(seconds, tasks) for seconds, tasks in [*people, *teams]]


async def reveal(
    ctx: Context, contest: ContestId, settings: ContestDefinition, task: str
) -> datetime | None:
    """When the task, by name, has closed for every row; none when the
    contest does not list it.
    """
    entry = settings.entry(task)
    if entry is None:
        return None
    return reveal_of(settings, entry, await every_extension(ctx, contest))


@dataclass(frozen=True, slots=True)
class Made:
    """One submission as a timeline check reads it: its row's extension,
    its number among the row's submissions of the task, and when it was
    taken.
    """

    extension: Extension
    number: int
    at: datetime

    def said(self) -> str:
        return f"submission {self.number}, made {self.at.isoformat()}"


async def submissions(
    ctx: Context, contest: ContestId, tasks: Mapping[str, TaskId]
) -> dict[str, list[Made]]:
    """Every submission to each of `tasks`, by task name, with its row's
    extension.
    """
    if not tasks:
        return {}
    names_of = {task_id: name for name, task_id in tasks.items()}
    rows = await ctx.db.execute(
        select(
            Grading.task_id,
            Grading.workspace_id,
            Grading.submission_number,
            func.min(Grading.submitted_at),
        )
        .where(Grading.task_id.in_(list(names_of)))
        .group_by(Grading.task_id, Grading.workspace_id, Grading.submission_number)
    )
    owners: dict[str, Extension] = {}
    found: dict[str, list[Made]] = {}
    for task_id, workspace, number, at in rows:
        if workspace not in owners:
            owner = ctx.forge.workspaces.owner_of(WorkspaceId(workspace))
            owners[workspace] = await of_owner(ctx, contest, owner)
        found.setdefault(names_of[TaskId(task_id)], []).append(Made(owners[workspace], number, at))
    return found


async def check_contest(
    ctx: Context,
    contest: ContestId,
    before: ContestDefinition | None,
    after: ContestDefinition,
) -> list[Problem]:
    """C1 across files and C2 for a contest save from `before` to `after`,
    each problem at its path in `contest.yaml`; a dropped entry's at
    `tasks`, since the file no longer has a line for it.
    """
    listed = await names.named(ctx, await ctx.forge.content.list_tasks(PLATFORM, contest))
    tasks = {each.name: TaskId(each.id) for each in listed}
    problems = [
        Problem(path=f"tasks[{index}].id", message=f"{entry.id} is not a task of this contest.")
        for index, entry in enumerate(after.tasks)
        if entry.id not in tasks
    ]
    problems += await _points(ctx, after, tasks)
    if before is None:
        return problems
    made = await submissions(ctx, contest, tasks)
    extensions = await every_extension(ctx, contest)
    for entry in before.tasks:
        if after.entry(entry.id) is None:
            message = _dropped(before, entry, made.get(entry.id, []), ctx.now)
            if message is not None:
                problems.append(Problem(path="tasks", message=message))
    for index, entry in enumerate(after.tasks):
        old = before.entry(entry.id)
        for path, message in _moved(
            before, old, after, entry, made.get(entry.id, []), extensions, ctx.now
        ):
            at = path if path in ("start", "end") else f"tasks[{index}].{path}"
            problems.append(Problem(path=at, message=message))
    return problems


def _dropped(
    before: ContestDefinition, entry: ContestTask, made: Collection[Made], now: datetime
) -> str | None:
    """Why the task's entry may not leave `tasks`, or none: a task released
    under `before`, or with submissions, would be hidden by it.
    """
    if released(before, entry.id, now):
        opened = before.release_of(entry)
        return f"{entry.id} opened at {opened.isoformat()}; it cannot be hidden again."
    if made:
        count = f"{len(made)} submission" + ("s" if len(made) > 1 else "")
        return f"{entry.id} has {count}; it cannot be hidden, so keep its entry."
    return None


async def _points(
    ctx: Context, after: ContestDefinition, tasks: Mapping[str, TaskId]
) -> list[Problem]:
    """C1 across files: no `worth` or `due` on a task that gives no points."""
    problems: list[Problem] = []
    for index, entry in enumerate(after.tasks):
        if entry.worth is None and entry.due is None:
            continue
        task = tasks.get(entry.id)
        found = await published.task(ctx, task, after, entry.id) if task is not None else None
        if found is None or found.definition.gives_points:
            continue
        if entry.worth is not None:
            problems.append(
                Problem(
                    path=f"tasks[{index}].worth",
                    message=f"{entry.id} gives no points, so it has no worth: rank it on a value.",
                )
            )
        if entry.due is not None:
            problems.append(
                Problem(
                    path=f"tasks[{index}].due",
                    message=(
                        f"{entry.id} gives no points, so a due would change nothing; use closes."
                    ),
                )
            )
    return problems


def _moved(
    before: ContestDefinition,
    listed: ContestTask | None,
    after: ContestDefinition,
    new: ContestTask,
    made: Collection[Made],
    extensions: Collection[Extension],
    now: datetime,
) -> list[tuple[str, str]]:
    """C2 for one task: how its entry moved against what rows did. Only a
    task released under `before` opened, and only one that also passed its
    reveal there revealed: a task the contest did not list, or listed in a
    contest not yet published or not yet started, did neither. A time the
    entry leaves out is the contest's `start` or `end`, and a problem with
    it is said at that key, never at one the entry does not write.
    """
    found: list[tuple[str, str]] = []
    task = new.id
    old = listed if listed is not None else ContestTask(id=task)
    opened = released(before, task, now)
    release_key = "release_at" if new.release_at is not None else "start"
    closes_key = "closes" if new.closes is not None else "end"
    old_release, new_release = before.release_of(old), after.release_of(new)
    if opened and new_release > old_release:
        found.append(
            (
                release_key,
                f"{task} opened at {old_release.isoformat()}; it cannot be hidden again.",
            )
        )
    for submission in made:
        old_due = due_of(before, old, submission.extension)
        new_due = due_of(after, new, submission.extension)
        if (
            new_due is not None
            and submission.at > new_due
            and (old_due is None or submission.at <= old_due)
        ):
            found.append(
                (
                    "due",
                    f"{task}'s {submission.said()} was on time; an earlier due would make it late.",
                )
            )
            break
    for submission in made:
        if submission.at >= close_of(after, new, submission.extension):
            found.append(
                (
                    closes_key,
                    f"{task}'s {submission.said()} would be after the task closed; close it later.",
                )
            )
            break
    revealed = reveal_of(before, old, extensions)
    if opened and revealed <= now and after.closes_of(new) > before.closes_of(old):
        found.append(
            (
                closes_key,
                f"{task}'s hidden results were shown at {revealed.isoformat()}; a later close "
                "would let rows submit knowing them.",
            )
        )
    return found


async def refuse_extension(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    workspace: WorkspaceId,
    before: Extension,
    after: Extension,
) -> None:
    """`InvalidExtension` when changing a row's extension from `before` to
    `after` would let it submit to a task whose reveal has passed, or leave
    one of its submissions after the due or the close it was made before.
    """
    listed = await names.named(ctx, await ctx.forge.content.list_tasks(PLATFORM, contest))
    tasks = {each.name: TaskId(each.id) for each in listed}
    extensions = await every_extension(ctx, contest)
    own = await _own(ctx, workspace, tasks)
    for entry in settings.tasks:
        if after.on(entry.id) > before.on(entry.id):
            revealed = reveal_of(settings, entry, extensions)
            if revealed <= ctx.now and close_of(settings, entry, after) > ctx.now:
                raise InvalidExtension(
                    f"{entry.id}'s hidden results were shown at {revealed.isoformat()}; an "
                    "extension on it would let this row submit knowing them."
                )
        for number, at in own.get(entry.id, []):
            made = Made(after, number, at)
            due_now, due_then = due_of(settings, entry, after), due_of(settings, entry, before)
            if due_now is not None and at > due_now and (due_then is None or at <= due_then):
                raise InvalidExtension(
                    f"{entry.id}'s {made.said()} was on time; a shorter extension would make "
                    f"it late."
                )
            if at >= close_of(settings, entry, after):
                raise InvalidExtension(
                    f"{entry.id}'s {made.said()} would be after the task closed for this row."
                )


async def _own(
    ctx: Context, workspace: WorkspaceId, tasks: Mapping[str, TaskId]
) -> dict[str, list[tuple[int, datetime]]]:
    names_of = {task_id: name for name, task_id in tasks.items()}
    rows = await ctx.db.execute(
        select(Grading.task_id, Grading.submission_number, func.min(Grading.submitted_at))
        .where(Grading.workspace_id == workspace, Grading.task_id.in_(list(names_of)))
        .group_by(Grading.task_id, Grading.submission_number)
    )
    found: dict[str, list[tuple[int, datetime]]] = {}
    for task_id, number, at in rows:
        found.setdefault(names_of[TaskId(task_id)], []).append((number, at))
    return found
