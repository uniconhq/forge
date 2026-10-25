"""The contest's leaderboards (SIMPLER-CORE.md 10), worked out on every read
from each row's results, for each viewer as SIMPLER-CORE.md 10.2 says
(`forge.domain.leaderboards`), never stored; the contest's hold ended early; and
the check a save of the contest's settings runs over them.

- **Who reads.** Staff, an observer of the contest or above, read every
  leaderboard, every row and every cell, with no hold. Anyone else who sees the
  contest, a visitor with no session among them, reads each leaderboard whose
  `who` lets them, as the leaderboards note's 6.3 works it out: the
  members of an approved entrant read `entrants` leaderboards with their row as
  their own, and anyone reads `everyone` leaderboards. A person holding a role
  only at one of the contest's tasks reads as anyone in no entrant would.
- **A row** is an approved entrant (`timelines.rows`), named as its
  entrant is shown. Its results for a task are its submissions to it,
  oldest first, each by the attempt of it that counts (`scores.usable`):
  one cancelled or ended over the limit with no result is left out, and
  one still grading has its values and `compiled` waiting. Only staff's
  read starts the retry of a failure it finds (`gradings.notice`, stage 5
  section 6, item 100). Each value is
  worked out by the shared scoring (`scores.run_scored`): staff read
  every value (`whole`); anyone else reads what the row's own entrant
  is shown now (`run_showing.shown`), so a leaderboard never shows more than the
  task's own page: a value reading an entered mark by `show` alone, since
  a cell reads one only on a submission it counted, and the page serves
  it there once such a cell is shown (stage 5 section 6, item 117); on a
  row the hold holds, such a value is hidden, since a pick there may take
  another submission than the entrant's own row does.
- **Facts:** `attempt` counts the row's submissions to the task that are
  read, 1 the oldest; `seconds` and `seconds_late` are from the contest's
  start and the row's due on the task, its extension included; `marked`
  is 1 for a submission the row marked; `compiled` is the grading's.
- **Tasks.** A leaderboard reads a task's results; a withdrawn task, or
  one with no publication, gives none (item 70).
- **The hold** holds from its `from` until its `until`, or until a manager
  or admin of the contest ends it early (`end_hold`), a `hold_ends` row
  naming the hold by its `from`.
- **A regrade's leaderboard diff** works the leaderboards out twice for staff, the
  regraded task's submissions read once by the attempts that counted when
  the regrade began and once with its own added (`Side`, `regrade_sides`).
- **A read works out** only the leaderboards asked for, the leaderboards their columns
  read, and the columns of those (`LeaderboardsCheck.needed`), and loads only the
  tasks those read.
- **What a row's cells counted** of one task (`chosen`): those a cell
  shown to its own entrant counted, which the task's page reads to serve
  an entered mark only once such a cell counts it (SIMPLER-CORE.md 14).

A save of the contest's settings is checked by `check_contest`: each leaderboard
against the catalogue and against the live publication of each task it
reads (`leaderboard_checks`), and against its newest while a save row still
checks that one, the hold's `from` inside the contest and never set or
moved to a time passed, and a task's marks not lowered below the marks a
row holds on it. A save of a task a leaderboard reads is checked against the
leaderboards by the save itself (`publications`).
"""

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Final

from sqlalchemy import select

from forge.db.tables import Entrant, Grading
from forge.db.tables.gradings import FRONT
from forge.domain import release as rules
from forge.domain import run_showing
from forge.domain.blocks.bounds import Bounds, column_bounds, self_reported_parts, value_bounds
from forge.domain.blocks.catalogue import ALL, ENTERED
from forge.domain.blocks.evaluate import RunResult, using
from forge.domain.blocks.leaderboard_evaluate import Submitted, work_out_row
from forge.domain.blocks.leaderboard_shape_check import LeaderboardsCheck
from forge.domain.definitions import TASK_FILE, ContestDefinition, parse_task
from forge.domain.definitions import State as ContestState
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.leaderboard_task_check import (
    Scored,
    across,
    hold_problems,
    leaderboards_of,
    marks_of,
)
from forge.domain.leaderboard_task_check import check_contest as check_leaderboards
from forge.domain.leaderboards import (
    STAFF,
    LeaderboardView,
    Loaded,
    Row,
    Viewer,
    counted_in,
    grading_seen,
    headers_of,
    held_back,
    holding,
    leaderboards_read,
    listed_now,
    read_leaderboards,
    results_seen,
    seen_bounds,
    sees,
    still_grading,
)
from forge.domain.names import EntrantOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.release import Extension, due_of, reveal_of
from forge.domain.roles import Role, contest_scope, holds
from forge.domain.run_results import facts_of
from forge.domain.run_task import holds as run_holds
from forge.domain.sessions import Session
from forge.domain.yaml_models import InvalidDefinition, Problem, path_text
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    entrant_view,
    entrants,
    entries,
    gradings,
    hold,
    marks,
    names,
    pipelines,
    release,
    scores,
    timelines,
)
from forge.services.entrant_view import PublishedTask
from forge.services.scores import State

CHECKING: Final = (
    "This is {task} as its newest save gives it, still being checked: it takes over once its "
    "check passes."
)
"""Said after a refusal across files found only against a task's newest
publication, which a save row still checks."""


@dataclass(frozen=True, slots=True)
class Chosen:
    """A row's submissions to one task that the leaderboards' cells counted, by
    number: `shown`, those a cell shown to the row's own entrant counted."""

    shown: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class Side:
    """How a regrade's leaderboard diff reads its task on one side: the task as
    it scores there, and the attempts of one submission it reads."""

    task: TaskId
    current: PublishedTask
    attempts: Callable[[Sequence[Grading]], Sequence[Grading]]


@action
async def seen(
    ctx: Context, session: Session | None, contest: ContestId, leaderboard: str | None = None
) -> tuple[LeaderboardView, ...]:
    """Every leaderboard of the contest the reader reads, in the file's order, or
    only the one keyed `leaderboard`, each as they read it: staff every leaderboard the
    check reads, every row and cell; anyone else each leaderboard whose `who`
    lets them. A visitor reads with no session. `NotFound` for a contest
    the reader may not see.
    """
    settings, viewer = await _reader(ctx, session, contest)
    keys = [
        key
        for key, each in settings.leaderboards.items()
        if (leaderboard is None or key == leaderboard) and sees(each, viewer)
    ]
    return await read(ctx, contest, settings, keys, viewer, notice=viewer.staff)


async def own_rows(
    ctx: Context, contest: ContestId, settings: ContestDefinition, owner: WorkspaceOwner
) -> tuple[LeaderboardView, ...]:
    """Each leaderboard a member of the approved entrant `owner` reads, in the
    file's order, with their own row alone, as they read it: the contest
    home's standing."""
    viewer = Viewer(own=owner)
    keys = [key for key, each in settings.leaderboards.items() if sees(each, viewer)]
    found = await read(ctx, contest, settings, keys, viewer)
    return tuple(
        LeaderboardView(
            each.key,
            each.title,
            each.audience,
            each.headers,
            each.sort,
            tuple(row for row in each.rows if row.owner == owner),
            each.dropped,
            each.held_from,
        )
        for each in found
    )


async def chosen(
    ctx: Context, contest: ContestId, settings: ContestDefinition, owner: WorkspaceOwner, task: str
) -> Chosen:
    """The submissions of the row `owner` to the task named `task` that
    the leaderboards' cells counted (`Chosen`): its own row of each leaderboard its
    entrant reads, as they read it. A row not approved has none."""
    if not settings.leaderboards:
        return Chosen()
    viewer = Viewer(own=owner)
    keys = [key for key, each in settings.leaderboards.items() if sees(each, viewer)]
    own = await read(ctx, contest, settings, keys, viewer, only=owner)
    return Chosen(frozenset(counted_in(own, task).get(owner, {})))


async def worked_out(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    keys: Collection[str],
    side: Side | None = None,
) -> tuple[LeaderboardView, ...]:
    """The leaderboards `keys` of those the check reads, in the file's order, as
    staff read them: every row, every value, no hold, a failure found seen
    to; with `side`, its task read as it says."""
    return await read(ctx, contest, settings, keys, STAFF, side, notice=True)


async def read(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    keys: Collection[str],
    viewer: Viewer,
    side: Side | None = None,
    only: WorkspaceOwner | None = None,
    *,
    notice: bool = False,
) -> tuple[LeaderboardView, ...]:
    """The leaderboards `keys` of those the check reads, in the file's order, as
    `viewer` reads them now; with `side`, staff's, its task read as it
    says; with `only`, that row alone, its cells as they read on every
    leaderboard; with `notice`, a failure found seen to, which only staff's
    leaderboard read does (item 100)."""
    checked = leaderboards_of(settings)
    wanted = [key for key in settings.leaderboards if key in keys and key in checked.readable]
    if not wanted:
        return ()
    checked = checked.needed(leaderboards_read(checked, wanted))
    read = _tasks_read(checked)
    tasks = [
        task
        for task in await entrant_view.tasks(ctx, contest, settings)
        if task.name in read and task.entry is not None and not rules.withdrawn(settings, task.name)
    ]
    if side is not None:
        tasks = [side.current if task.id == side.task else task for task in tasks]
    owners = [
        owner for owner in await timelines.rows(ctx, contest) if only is None or owner == only
    ]
    extensions = await timelines.of_owners(ctx, contest, owners)
    # A task reveals once it has closed for every row, whichever rows are
    # read, so a read of one row never reveals it sooner.
    every_row = await timelines.every_extension(ctx, contest)
    shown = await _names(ctx, owners)
    marked = await marks.of_tasks(ctx, [task.id for task in tasks])
    since = None
    if not viewer.staff:
        since = holding(settings.hold, ctx.now, await hold.hold_ended(ctx, contest, settings.hold))
    released = [entry.id for entry in settings.tasks if rules.released(settings, entry.id, ctx.now)]
    results: dict[WorkspaceOwner, dict[str, list[Loaded]]] = {owner: {} for owner in owners}
    trees: dict[str, Mapping[str, object]] = {}
    bounds: dict[str, Mapping[str, Bounds]] = {}
    most: dict[str, int | None] = {}
    reported: dict[str, frozenset[str]] = {}
    entered: dict[str, frozenset[str]] = {}
    for task in tasks:
        scoring = task.definition.scoring
        assert task.entry is not None
        held = await entrant_view.runs_held(ctx, task)
        trees[task.name] = scoring.values
        entered[task.name] = using(scoring.values, ENTERED)
        revealed = ctx.now >= reveal_of(settings, task.entry, every_row)
        results_released = not viewer.staff and await entrant_view.results_released(ctx, task.id)
        groups = {ALL: [RunResult(run.id, "", run.key) for run in held.runs]}
        for name, picked in scoring.groups.items():
            groups[name] = [
                RunResult(run.id, "", run.key) for run in held.runs if run_holds(picked, run.key)
            ]
        # Anyone but staff knows a column's bounds only from what they are
        # shown now (item 126): none from a task not released to them.
        if viewer.staff:
            bounds[task.name] = value_bounds(scoring.values, groups, held.columns)
            most[task.name] = task.definition.submissions.max
        elif task.name in released:
            times = run_showing.times_of(revealed=revealed, released=results_released)
            bounds[task.name] = seen_bounds(scoring, groups, held.columns, times)
            most[task.name] = task.definition.submissions.max
        reported[task.name] = held.self_reported
        attempts = side.attempts if side is not None and side.task == task.id else None
        found = await _results(
            ctx,
            contest,
            settings,
            task,
            owners,
            extensions,
            marked,
            viewer,
            attempts,
            revealed=revealed,
            released=results_released,
            notice=notice,
        )
        for owner, listed in found.items():
            results[owner][task.name] = listed
    columns = column_bounds(settings.leaderboards, checked, bounds, most, trees)
    parts = self_reported_parts(checked, reported)
    rows: list[Row] = []
    for owner in owners:
        lists, held_counts = held_back(
            results[owner], since if owner != viewer.own else None, entered
        )
        rows.append(
            Row(
                owner,
                shown.get(owner, ""),
                work_out_row(settings.leaderboards, checked, lists, trees),
                held_counts,
                still_grading(lists),
            )
        )
    every = list(dict.fromkeys(leaderboard for leaderboard, _ in checked.order))
    return read_leaderboards(
        settings.leaderboards,
        checked,
        wanted,
        rows,
        viewer,
        released=released,
        headers={key: headers_of(key, settings.leaderboards[key], columns, parts) for key in every},
        held_from=since,
    )


def _tasks_read(checked: LeaderboardsCheck) -> set[str]:
    """Every task whose results a column the check orders reads."""
    return {
        task
        for leaderboard, name in checked.order
        for task in checked.shapes[leaderboard][name].tasks
    }


async def _results(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    task: PublishedTask,
    owners: Sequence[WorkspaceOwner],
    extensions: Mapping[WorkspaceOwner, Extension],
    marked: Mapping[tuple[WorkspaceId, TaskId], set[int]],
    viewer: Viewer,
    pick: Callable[[Sequence[Grading]], Sequence[Grading]] | None = None,
    *,
    revealed: bool,
    released: bool,
    notice: bool = False,
) -> dict[WorkspaceOwner, list[Loaded]]:
    """Each row's submissions to a task, oldest first, each with when
    it was made: as staff read them, every value, or for anyone else as
    the row's own entrant is shown them now, a value reading a mark by
    `show` alone, since a cell reading one counted its submission (stage 5
    section 6, item 117); `pick` the attempts of each submission read, all
    when none, a submission it leaves none of left out; `revealed` whether
    the task has revealed, and `released` whether its results are
    released, for anyone but staff; with `notice`, a failure found is seen
    to.
    """
    workspaces = {ctx.forge.workspaces.workspace_of(contest, owner): owner for owner in owners}
    rows = (
        await ctx.db.scalars(
            select(Grading).where(
                Grading.task_id == task.id,
                Grading.workspace_id.in_(sorted(workspaces)),
                FRONT,
            )
        )
    ).all()
    grouped: dict[tuple[str, int], list[Grading]] = {}
    for row in rows:
        grouped.setdefault((row.workspace_id, row.submission_number), []).append(row)
    latest = [max(each, key=lambda row: row.attempt) for each in grouped.values()]
    lost = await gradings.lost(ctx, latest)
    if notice:
        # Only staff's read sees to a failure, so a read with no session
        # writes nothing; an entrant's own submission page sees to theirs.
        await gradings.notice(ctx, latest, lost)
        await gradings.notice_late(ctx, latest)
    read: list[tuple[str, int, Grading, Grading | None]] = []
    for (workspace, number), attempts in sorted(grouped.items()):
        chosen = pick(attempts) if pick is not None else attempts
        if not chosen:
            continue
        usable = scores.usable(ctx, chosen, lost)
        if usable.state is State.VOID:
            continue
        first = min(chosen, key=lambda row: row.attempt)
        read.append((workspace, number, first, usable.row))
    good = [row for _, _, _, row in read if row is not None]
    lates = await scores.lates_of(ctx, good)
    entered = await entries.entered_for(ctx, task.id, [row.submission_id for row in good])
    entry = task.entry
    assert entry is not None
    scoring = task.definition.scoring
    times = run_showing.times_of(revealed=revealed, released=released)
    values_listed, compiled_listed = listed_now(scoring, times)
    found: dict[WorkspaceOwner, list[Loaded]] = {}
    for workspace, number, first, counted in read:
        owner = workspaces[WorkspaceId(workspace)]
        extension = extensions[owner]
        listed_of = found.setdefault(owner, [])
        own = {
            "attempt": Fraction(len(listed_of) + 1),
            "marked": Fraction(number in marked.get((WorkspaceId(workspace), task.id), set())),
        }
        if counted is None:
            facts = facts_of(
                due_of(settings, entry, extension), settings.start, first.submitted_at, 1
            )
            numbers = {**{name: facts[name] for name in ("seconds", "seconds_late")}, **own}
            if viewer.staff:
                waiting = frozenset({*scoring.values, "compiled"})
                results = Submitted(number, numbers, waiting)
            else:
                results = grading_seen(
                    number, numbers, scoring.values, values_listed, compiled_listed
                )
            listed_of.append(Loaded(first.submitted_at, results))
            continue
        scored = await scores.run_scored(
            ctx,
            task,
            settings,
            extension,
            counted,
            lates.get(counted.id),
            entered[counted.submission_id],
        )
        if viewer.staff:
            worked = scored.whole
            values = {
                name: outcome.number
                for name, outcome in worked.outcomes.items()
                if outcome.number is not None
            }
            waits = {name for name, outcome in worked.outcomes.items() if outcome.waiting}
            if worked.grading.compiled is None:
                waits.add("compiled")
            results = Submitted(number, {**values, **worked.facts, **own}, frozenset(waits))
        else:
            shown = run_showing.shown(
                scoring,
                scored.held.runs,
                scored.held.files,
                scored.whole,
                scored.early,
                scored.self_reported,
                revealed=revealed,
                released=released,
                late=scored.late,
            )
            facts = {name: scored.whole.facts[name] for name in ("seconds", "seconds_late")}
            results = results_seen(
                number,
                {**facts, **own},
                shown.values,
                shown.compiled,
                scoring.values,
                values_listed,
                compiled_listed,
            )
        listed_of.append(Loaded(first.submitted_at, results))
    return found


@dataclass(frozen=True, slots=True)
class ContestCheck:
    """What a save of the contest's settings is refused for: each leaderboard's
    problems, and a task's marks below what a row holds; and the notes the
    save carries, none since the leaderboards report none of their own."""

    problems: list[Problem]
    notes: list[str]


async def check_contest(
    ctx: Context,
    contest: ContestId,
    before: ContestDefinition | None,
    settings: ContestDefinition,
) -> ContestCheck:
    """The leaderboards' check for a save of the contest's settings from
    `before`, each refusal at its path: each leaderboard against the catalogue
    and against the live publication of each task it reads, a task with
    none yet left to its first save (`leaderboard_checks.across`), and against
    its newest while a save row still checks that one, since it then takes
    over with no save to check the leaderboards again (`_checking`); the hold's
    `from` inside the contest, and never set or moved to a time passed;
    and C1's last rule, a task's marks not lowered below the marks a row
    holds on it."""
    listed = await names.named(ctx, await ctx.forge.content.list_tasks(PLATFORM, contest))
    ids = {each.name: TaskId(each.id) for each in listed}
    found, checked = check_leaderboards(settings, await _scored(ctx, settings, ids))
    for each in across(settings, checked, await _checking(ctx, settings, ids)):
        if all(at != each.at for at, _ in found):
            found.append((each.at, f"{each.message} {CHECKING.format(task=each.task)}"))
    found += hold_problems(before, settings, ctx.now)
    problems = [Problem(path=path_text(path), message=message) for path, message in found]
    problems += await _held_marks(ctx, settings, ids)
    return ContestCheck(problems, [])


async def _scored(
    ctx: Context, settings: ContestDefinition, ids: Mapping[str, TaskId]
) -> dict[str, Scored]:
    """Each task scored by its runs that a leaderboard reads a value of, by name,
    as its live publication gives its values; a task with no live
    publication is left out."""
    read = {task for task, _ in _values_read(leaderboards_of(settings))}
    found: dict[str, Scored] = {}
    for name in sorted(read):
        task = ids.get(name)
        current = await entrant_view.task(ctx, task, settings, name) if task is not None else None
        if current is None:
            continue
        found[name] = Scored(current.definition.scoring.values)
    return found


async def _checking(
    ctx: Context, settings: ContestDefinition, ids: Mapping[str, TaskId]
) -> dict[str, Scored]:
    """Each task scored by its runs that a leaderboard reads a value of, by name,
    as its newest publication gives its values while that one is not live
    and a save row still checks it (`pipelines.checking`). Only the newest
    can be, since each save stops the checking of the one before."""
    read = {task for task, _ in _values_read(leaderboards_of(settings))}
    found: dict[str, Scored] = {}
    for name in sorted(read):
        task = ids.get(name)
        if task is None:
            continue
        await ctx.let_go()
        try:
            made = await ctx.forge.workspaces.list_publications(task)
        except NotFound:
            continue
        live = {each.id for each in await entrant_view.live(ctx, task, made)}
        if not made or made[-1].id in live or not await pipelines.checking(ctx, made[-1].id):
            continue
        newest = await ctx.forge.content.read_file(PLATFORM, task, TASK_FILE, at=made[-1].version)
        try:
            scoring = parse_task(newest.content).scoring
        except InvalidDefinition:
            continue
        found[name] = Scored(scoring.values)
    return found


def _values_read(checked: LeaderboardsCheck) -> set[tuple[str, str]]:
    """Every task's value or fact a column of any leaderboard reads directly."""
    return {
        each
        for columns in checked.shapes.values()
        for shape in columns.values()
        for each in shape.reads
    }


async def _held_marks(
    ctx: Context, settings: ContestDefinition, ids: Mapping[str, TaskId]
) -> list[Problem]:
    problems: list[Problem] = []
    for index, entry in enumerate(settings.tasks):
        allowed = marks_of(settings, entry)
        if allowed is None or entry.id not in ids:
            continue
        most, rows = await marks.most_held(ctx, ids[entry.id])
        if most <= allowed:
            continue
        held = (
            f"{rows} {'entrant has' if rows == 1 else 'entrants have'} marked {most} "
            f"submissions on {entry.id}"
        )
        path = f"tasks[{index}].marks" if entry.marks is not None else f"tasks[{index}]"
        problems.append(Problem(path=path, message=f"{held}, so it cannot allow fewer marks."))
    return problems


async def _reader(
    ctx: Context, session: Session | None, contest: ContestId
) -> tuple[ContestDefinition, Viewer]:
    """The contest's settings, and who the reader is to its leaderboards: staff,
    an observer of the contest or above; the member of an approved entrant,
    with that row as their own; or anyone else. `NotFound` for a contest
    they may not see."""
    settings = await entrant_view.contest(ctx, contest)
    if session is None:
        if not rules.contest_visible_to(
            settings, has_session=False, is_contestant=False, is_organiser=False
        ):
            raise NotFound(entrant_view.NO_SUCH_CONTEST)
        return settings, Viewer()
    person = await release.reader(ctx, session, contest)
    approved = person.entrant is not None and person.entrant.status == Status.APPROVED
    entered = settings.state is ContestState.ARCHIVED and approved
    if not release.sees(settings, person) and not entered:
        raise NotFound(entrant_view.NO_SUCH_CONTEST)
    if person.organises:
        try:
            grants = await ctx.forge.orgs.roles_of_user(session.user_id)
        except NotFound:
            grants = ()
        if holds(grants, contest_scope(contest), Role.OBSERVER):
            return settings, STAFF
    if approved:
        assert person.entrant is not None
        return settings, Viewer(own=EntrantOwner(person.entrant.id))
    return settings, Viewer()


async def _names(ctx: Context, owners: Sequence[WorkspaceOwner]) -> dict[WorkspaceOwner, str]:
    """Each row's name, as its entrant is shown; a username the forge does
    not give is left empty.
    """
    ids = sorted({owner.entrant_id for owner in owners})
    rows = (await ctx.db.scalars(select(Entrant).where(Entrant.id.in_(ids)))).all() if ids else []
    shown = await entrants.display_names(ctx, rows)
    return {EntrantOwner(entrant): name for entrant, name in shown.items()}


def regrade_sides(
    regrade: uuid.UUID,
    started_at: datetime,
    task: TaskId,
    then: PublishedTask,
    now: PublishedTask,
) -> tuple[Side, Side]:
    """How a regrade's leaderboard diff reads its task before and after (stage
    5 section 6, item 71): before, each submission by the attempts queued
    by the time the regrade began, but its own, under the publication then
    the task's latest; after, by those and the regrade's own, under the one
    it grades against. Each side counts the attempt every leaderboard does
    (`scores.usable`), so while a regrade's attempt of a submission is
    still to finish, or once it was cancelled or ended over the limit, the
    after side reads the attempt before it, as the live leaderboard does. A
    submission made after the regrade began has neither, and is on
    neither side."""

    def before(attempts: Sequence[Grading]) -> Sequence[Grading]:
        return [
            row for row in attempts if row.regrade_id != regrade and row.queued_at <= started_at
        ]

    def after(attempts: Sequence[Grading]) -> Sequence[Grading]:
        return [row for row in attempts if row.regrade_id == regrade or row.queued_at <= started_at]

    return Side(task, then, before), Side(task, now, after)


__all__ = [
    "Chosen",
    "ContestCheck",
    "Side",
    "check_contest",
    "chosen",
    "own_rows",
    "read",
    "regrade_sides",
    "seen",
    "worked_out",
]
