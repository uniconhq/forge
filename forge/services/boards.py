"""The contest's leaderboards (SIMPLER-CORE.md 10), worked out on every read
from each row's results and never stored, and the check a save of the
contest's settings runs over them.

- **Who reads.** Until stage 5's phase 5.6 decides who sees what, a board
  is read by staff alone, an observer of the contest or above: every
  board the check reads, every row and every displayed cell, with no hold.
  Anyone else, a visitor among them, reads no board, so nothing is shown
  wider than a board's `audience` will let it be.
- **A row** is an approved entrant (`timelines.rows`), named as its
  entrant is shown. Its results for a task are its submissions to it,
  oldest first, each by the attempt of it that counts (`scores.usable`):
  one cancelled or ended over the limit with no result is left out, and
  one still grading has its values and `compiled` waiting. Each value is
  worked out by the shared scoring (`scores.run_scored`) as staff read it,
  every value (`organised`). What an entrant or a visitor may read of a
  board is phase 5.6's, built from what each of them is shown of a task.
- **Facts:** `attempt` counts the row's submissions to the task that are
  read, 1 the oldest; `seconds` and `seconds_late` are from the contest's
  start and the row's due on the task, its extension included; `marked`
  is 1 for a submission the row marked; `compiled` is the grading's.
- **Tasks.** A board reads a run task's results; a task graded test by
  test, a withdrawn one, or one with no publication gives none (item 70).
- **A regrade's board diff** works the boards out twice, the regraded
  task's submissions read once by the attempts that counted when the
  regrade began and once with its own added (`Side`, `regrade_sides`).
- **A read works out** only the boards asked for and the columns they
  read (`BoardsCheck.needed`), and loads only the tasks those read.

A save of the contest's settings is checked by `check_contest`: each board
against the catalogue (`board_checks`), and a task's marks not lowered
below the marks a row holds on it.
"""

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from sqlalchemy import select

from forge.db.tables import Entrant, Grading
from forge.db.tables.gradings import FRONT
from forge.domain import release as rules
from forge.domain.blocks.board_check import BoardsCheck
from forge.domain.blocks.board_evaluate import Submitted, work_out_row
from forge.domain.blocks.bounds import Bounds, column_bounds, self_reported_parts, value_bounds
from forge.domain.blocks.catalogue import ALL
from forge.domain.blocks.evaluate import RunResult
from forge.domain.board_checks import boards_of, marks_of
from forge.domain.board_checks import check_contest as check_boards
from forge.domain.boards import Board, Unranked, headers_of, ranked
from forge.domain.definitions import ContestDefinition
from forge.domain.definitions import State as ContestState
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import EntrantOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.release import Extension, due_of
from forge.domain.roles import Role, contest_scope, holds
from forge.domain.run_results import facts_of
from forge.domain.run_task import holds as run_holds
from forge.domain.scoring import State
from forge.domain.sessions import Session
from forge.domain.yaml_models import Problem, path_text
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    entrants,
    entries,
    gradings,
    marks,
    names,
    published,
    release,
    scores,
    timelines,
)
from forge.services.published import PublishedTask

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Side:
    """How a regrade's board diff reads its task on one side: the task as
    it scores there, and the attempts of one submission it reads."""

    task: TaskId
    current: PublishedTask
    attempts: Callable[[Sequence[Grading]], Sequence[Grading]]


@action
async def seen(
    ctx: Context, session: Session | None, contest: ContestId, board: str | None = None
) -> tuple[Board, ...]:
    """Every board of the contest the reader reads, in the file's order, or
    only the one keyed `board`: staff read each board the check reads, and
    anyone else none until phase 5.6. A visitor reads with no session.
    `NotFound` for a contest the reader may not see.
    """
    settings, staff = await _reader(ctx, session, contest)
    if not staff:
        return ()
    keys = [key for key in settings.leaderboards if board is None or key == board]
    return await worked_out(ctx, contest, settings, keys)


async def worked_out(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    keys: Collection[str],
    side: Side | None = None,
) -> tuple[Board, ...]:
    """The boards `keys` of those the check reads, in the file's order, as
    staff read them: every row, every value, no hold; with `side`, its task
    read as it says."""
    checked = boards_of(settings)
    wanted = [key for key in settings.leaderboards if key in keys and key in checked.readable]
    if not wanted:
        return ()
    checked = checked.needed(wanted)
    read = _tasks_read(checked)
    tasks = [
        task
        for task in await published.tasks(ctx, contest, settings)
        if task.name in read
        and task.entry is not None
        and task.definition.scoring is not None
        and not rules.withdrawn(settings, task.name)
    ]
    if side is not None:
        tasks = [side.current if task.id == side.task else task for task in tasks]
    owners = await timelines.rows(ctx, contest)
    extensions = await timelines.of_owners(ctx, contest, owners)
    shown = await _names(ctx, owners)
    marked = await marks.of_tasks(ctx, [task.id for task in tasks])
    results: dict[WorkspaceOwner, dict[str, list[Submitted]]] = {owner: {} for owner in owners}
    trees: dict[str, Mapping[str, object]] = {}
    bounds: dict[str, Mapping[str, Bounds]] = {}
    reported: dict[str, frozenset[str]] = {}
    for task in tasks:
        scoring = task.definition.scoring
        assert scoring is not None
        held = await published.runs_held(ctx, task)
        trees[task.name] = scoring.values
        groups = {ALL: [RunResult(run.id, "", run.key) for run in held.runs]}
        for name, picked in scoring.groups.items():
            groups[name] = [
                RunResult(run.id, "", run.key) for run in held.runs if run_holds(picked, run.key)
            ]
        bounds[task.name] = value_bounds(scoring.values, groups, held.columns)
        reported[task.name] = held.self_reported
        attempts = side.attempts if side is not None and side.task == task.id else None
        found = await _results(ctx, contest, settings, task, owners, extensions, marked, attempts)
        for owner, listed in found.items():
            results[owner][task.name] = listed
    most = {task.name: task.definition.submissions.max for task in tasks}
    columns = column_bounds(settings.leaderboards, checked, bounds, most)
    parts = self_reported_parts(checked, reported)
    cells = {
        owner: work_out_row(settings.leaderboards, checked, results[owner], trees)
        for owner in owners
    }
    found_boards: list[Board] = []
    for key in wanted:
        board = settings.leaderboards[key]
        headers = headers_of(key, board, columns, parts)
        rows = [
            Unranked(
                owner,
                shown.get(owner, ""),
                {name: cells[owner][(key, name)] for name in board.columns},
            )
            for owner in owners
        ]
        found_boards.append(ranked(key, board, headers, rows))
    return tuple(found_boards)


def _tasks_read(checked: BoardsCheck) -> set[str]:
    """Every task whose results a column the check orders reads."""
    return {task for board, name in checked.order for task in checked.shapes[board][name].tasks}


async def _results(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    task: PublishedTask,
    owners: Sequence[WorkspaceOwner],
    extensions: Mapping[WorkspaceOwner, Extension],
    marked: Mapping[tuple[WorkspaceId, TaskId], set[int]],
    pick: Callable[[Sequence[Grading]], Sequence[Grading]] | None = None,
) -> dict[WorkspaceOwner, list[Submitted]]:
    """Each row's submissions to a run task as staff read them, every
    value, oldest first; `pick` the attempts of each submission read, all
    when none, a submission it leaves none of left out.
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
    assert scoring is not None
    found: dict[WorkspaceOwner, list[Submitted]] = {}
    for workspace, number, first, counted in read:
        owner = workspaces[WorkspaceId(workspace)]
        extension = extensions[owner]
        listed = found.setdefault(owner, [])
        own = {
            "attempt": Fraction(len(listed) + 1),
            "marked": Fraction(number in marked.get((WorkspaceId(workspace), task.id), set())),
        }
        if counted is None:
            facts = facts_of(
                due_of(settings, entry, extension), settings.start, first.submitted_at, 1
            )
            numbers = {**{name: facts[name] for name in ("seconds", "seconds_late")}, **own}
            waiting = frozenset({*scoring.values, "compiled"})
            listed.append(Submitted(number, numbers, waiting))
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
        worked = scored.organised
        values = {
            name: outcome.number
            for name, outcome in worked.outcomes.items()
            if outcome.number is not None
        }
        waits = frozenset(name for name, outcome in worked.outcomes.items() if outcome.waiting)
        listed.append(Submitted(number, {**values, **worked.facts, **own}, waits))
    return found


@dataclass(frozen=True, slots=True)
class ContestCheck:
    """What a save of the contest's settings is refused for: each board's
    problems, and a task's marks below what a row holds; and the notes the
    save carries, none since the boards report none of their own."""

    problems: list[Problem]
    notes: list[str]


async def check_contest(
    ctx: Context, contest: ContestId, settings: ContestDefinition
) -> ContestCheck:
    """The boards' check for a save of the contest's settings, each refusal
    at its path; and C1's last rule, a task's marks not lowered below the
    marks a row holds on it."""
    found, _ = check_boards(settings)
    problems = [Problem(path=path_text(path), message=message) for path, message in found]
    problems += await _held_marks(ctx, contest, settings)
    return ContestCheck(problems, [])


async def _held_marks(
    ctx: Context, contest: ContestId, settings: ContestDefinition
) -> list[Problem]:
    listed = await names.named(ctx, await ctx.forge.content.list_tasks(PLATFORM, contest))
    ids = {each.name: TaskId(each.id) for each in listed}
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
) -> tuple[ContestDefinition, bool]:
    """The contest's settings, and whether the reader is staff: an observer
    of the contest or above. `NotFound` for a contest they may not see."""
    settings = await published.contest(ctx, contest)
    if session is None:
        if not rules.contest_visible_to(
            settings, has_session=False, is_contestant=False, is_organiser=False
        ):
            raise NotFound(published.NO_SUCH_CONTEST)
        return settings, False
    person = await release.reader(ctx, session, contest)
    approved = person.entrant is not None and person.entrant.status == Status.APPROVED
    entered = settings.state is ContestState.ARCHIVED and approved
    if not release.sees(settings, person) and not entered:
        raise NotFound(published.NO_SUCH_CONTEST)
    if person.organises:
        try:
            grants = await ctx.forge.orgs.roles_of_user(session.user_id)
        except NotFound:
            grants = ()
        if holds(grants, contest_scope(contest), Role.OBSERVER):
            return settings, True
    return settings, False


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
    """How a regrade's board diff reads its task before and after (stage
    5 section 6, item 71): before, each submission by the attempts queued
    by the time the regrade began, but its own, under the publication then
    the task's latest; after, by those and the regrade's own, under the one
    it grades against. Each side counts the attempt every board does
    (`scores.usable`), so while a regrade's attempt of a submission is
    still to finish, or once it was cancelled or ended over the limit, the
    after side reads the attempt before it, as the live board does. A
    submission made after the regrade began has neither, and is on
    neither side."""

    def before(attempts: Sequence[Grading]) -> Sequence[Grading]:
        return [
            row for row in attempts if row.regrade_id != regrade and row.queued_at <= started_at
        ]

    def after(attempts: Sequence[Grading]) -> Sequence[Grading]:
        return [row for row in attempts if row.regrade_id == regrade or row.queued_at <= started_at]

    return Side(task, then, before), Side(task, now, after)


__all__ = ["ContestCheck", "Side", "check_contest", "regrade_sides", "seen", "worked_out"]
