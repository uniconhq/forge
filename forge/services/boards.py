"""The contest's leaderboards (SIMPLER-CORE.md 10), worked out on every read
from each row's results, for each viewer as SIMPLER-CORE.md 10.2 says
(`forge.domain.boards`), never stored; the contest's hold ended early; and
the check a save of the contest's settings runs over them.

- **Who reads.** Staff, an observer of the contest or above, read every
  board, every row and every cell, with no hold. Anyone else who sees the
  contest, a visitor with no session among them, reads each board whose
  `who` lets them, as the leaderboards note's 6.3 works it out: the
  members of an approved entrant read `entrants` boards with their row as
  their own, and anyone reads `everyone` boards. A person holding a role
  only at one of the contest's tasks reads as anyone in no entrant would.
- **A row** is an approved entrant (`timelines.rows`), named as its
  entrant is shown. Its results for a task are its submissions to it,
  oldest first, each by the attempt of it that counts (`scores.usable`):
  one cancelled or ended over the limit with no result is left out, and
  one still grading has its values and `compiled` waiting. Only staff's
  read starts the retry of a failure it finds (`gradings.notice`, stage 5
  section 6, item 100). Each value is
  worked out by the shared scoring (`scores.run_scored`): staff read
  every value (`organised`); anyone else reads what the row's own entrant
  is shown now (`run_showing.shown`), so a board never shows more than the
  task's own page, a value reading an entered mark among what it leaves
  out (stage 5 section 6, item 45).
- **Facts:** `attempt` counts the row's submissions to the task that are
  read, 1 the oldest; `seconds` and `seconds_late` are from the contest's
  start and the row's due on the task, its extension included; `marked`
  is 1 for a submission the row marked; `compiled` is the grading's.
- **Tasks.** A board reads a run task's results; a task graded test by
  test, a withdrawn one, or one with no publication gives none (item 70).
- **The hold** holds from its `from` until its `until`, or until a manager
  or admin of the contest ends it early (`end_hold`), a `hold_ends` row
  naming the hold by its `from`.
- **A regrade's board diff** works the boards out twice for staff, the
  regraded task's submissions read once by the attempts that counted when
  the regrade began and once with its own added (`Side`, `regrade_sides`).
- **A read works out** only the boards asked for, the boards their columns
  read, and the columns of those (`BoardsCheck.needed`), and loads only the
  tasks those read.

A save of the contest's settings is checked by `check_contest`: each board
against the catalogue (`board_checks`), and a task's marks not lowered
below the marks a row holds on it.
"""

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Final

from sqlalchemy import select

from forge.db.tables import Entrant, Grading, HoldEnd
from forge.db.tables.gradings import FRONT
from forge.domain import release as rules
from forge.domain import run_showing
from forge.domain.blocks.board_check import BoardsCheck
from forge.domain.blocks.board_evaluate import Submitted, work_out_row
from forge.domain.blocks.bounds import Bounds, column_bounds, self_reported_parts, value_bounds
from forge.domain.blocks.catalogue import ALL
from forge.domain.blocks.evaluate import RunResult
from forge.domain.board_checks import boards_of, marks_of
from forge.domain.board_checks import check_contest as check_boards
from forge.domain.boards import (
    STAFF,
    Board,
    Loaded,
    Row,
    Viewer,
    boards_read,
    grading_seen,
    headers_of,
    held_back,
    holding,
    listed_now,
    read_boards,
    results_seen,
    sees,
    still_grading,
)
from forge.domain.definitions import ContestDefinition, Hold
from forge.domain.definitions import State as ContestState
from forge.domain.errors import ConfirmationRequired, NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import EntrantOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.release import Extension, due_of, in_words, reveal_of
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
from forge.services.access import Organiser, require
from forge.services.published import PublishedTask

log = get_logger(__name__)

NO_HOLD: Final = "The contest has no hold to end."


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
    only the one keyed `board`, each as they read it: staff every board the
    check reads, every row and cell; anyone else each board whose `who`
    lets them. A visitor reads with no session. `NotFound` for a contest
    the reader may not see.
    """
    settings, viewer = await _reader(ctx, session, contest)
    keys = [
        key
        for key, each in settings.leaderboards.items()
        if (board is None or key == board) and sees(each, viewer)
    ]
    return await read(ctx, contest, settings, keys, viewer)


async def own_rows(
    ctx: Context, contest: ContestId, settings: ContestDefinition, owner: WorkspaceOwner
) -> tuple[Board, ...]:
    """Each board a member of the approved entrant `owner` reads, in the
    file's order, with their own row alone, as they read it: the contest
    home's standing."""
    viewer = Viewer(own=owner)
    keys = [key for key, each in settings.leaderboards.items() if sees(each, viewer)]
    found = await read(ctx, contest, settings, keys, viewer)
    return tuple(
        Board(
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
    return await read(ctx, contest, settings, keys, STAFF, side)


async def read(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    keys: Collection[str],
    viewer: Viewer,
    side: Side | None = None,
) -> tuple[Board, ...]:
    """The boards `keys` of those the check reads, in the file's order, as
    `viewer` reads them now; with `side`, staff's, its task read as it
    says."""
    checked = boards_of(settings)
    wanted = [key for key in settings.leaderboards if key in keys and key in checked.readable]
    if not wanted:
        return ()
    checked = checked.needed(boards_read(checked, wanted))
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
    since = None
    if not viewer.staff:
        since = holding(settings.hold, ctx.now, await _hold_ended(ctx, contest, settings.hold))
    results: dict[WorkspaceOwner, dict[str, list[Loaded]]] = {owner: {} for owner in owners}
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
        found = await _results(
            ctx, contest, settings, task, owners, extensions, marked, viewer, attempts
        )
        for owner, listed in found.items():
            results[owner][task.name] = listed
    most = {task.name: task.definition.submissions.max for task in tasks}
    columns = column_bounds(settings.leaderboards, checked, bounds, most)
    parts = self_reported_parts(checked, reported)
    rows: list[Row] = []
    for owner in owners:
        lists, held_counts = held_back(results[owner], since if owner != viewer.own else None)
        rows.append(
            Row(
                owner,
                shown.get(owner, ""),
                work_out_row(settings.leaderboards, checked, lists, trees),
                held_counts,
                still_grading(lists),
            )
        )
    every = list(dict.fromkeys(board for board, _ in checked.order))
    return read_boards(
        settings.leaderboards,
        checked,
        wanted,
        rows,
        viewer,
        released=[
            entry.id for entry in settings.tasks if rules.released(settings, entry.id, ctx.now)
        ],
        headers={key: headers_of(key, settings.leaderboards[key], columns, parts) for key in every},
        held_from=since,
    )


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
    viewer: Viewer,
    pick: Callable[[Sequence[Grading]], Sequence[Grading]] | None = None,
) -> dict[WorkspaceOwner, list[Loaded]]:
    """Each row's submissions to a run task, oldest first, each with when
    it was made: as staff read them, every value, or for anyone else as
    the row's own entrant is shown them now; `pick` the attempts of each
    submission read, all when none, a submission it leaves none of left
    out.
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
    if viewer.staff:
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
    assert scoring is not None
    revealed = ctx.now >= reveal_of(settings, entry, extensions.values())
    released = not viewer.staff and await published.results_released(ctx, task.id)
    times = run_showing.times_of(revealed=revealed, released=released)
    values_listed, runs_listed = listed_now(scoring, times)
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
                results = grading_seen(number, numbers, scoring.values, values_listed, runs_listed)
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
            worked = scored.organised
            values = {
                name: outcome.number
                for name, outcome in worked.outcomes.items()
                if outcome.number is not None
            }
            waits = frozenset(name for name, outcome in worked.outcomes.items() if outcome.waiting)
            results = Submitted(number, {**values, **worked.facts, **own}, waits)
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
            )
            facts = {name: scored.whole.facts[name] for name in ("seconds", "seconds_late")}
            results = results_seen(
                number,
                {**facts, **own},
                shown.values,
                shown.compiled,
                scoring.values,
                values_listed,
                runs_listed,
            )
        listed_of.append(Loaded(first.submitted_at, results))
    return found


@dataclass(frozen=True, slots=True)
class HoldState:
    """Where the contest's hold stands: its `from` and `until`, none for
    each with no hold; whether it holds now; and, when staff ended it
    early, who, by user id and `by_name`, their username, none when the
    forge does not say, and when."""

    from_: datetime | None
    until: datetime | str | None
    holding: bool
    ended_by: int | None = None
    ended_by_name: str | None = None
    ended_at: datetime | None = None


async def _hold_end(ctx: Context, contest: ContestId, hold: Hold | None) -> HoldEnd | None:
    """The row ending the contest's hold early, none when staff have not
    ended this hold, the one with this `from`."""
    if hold is None:
        return None
    return await ctx.db.scalar(
        select(HoldEnd).where(HoldEnd.contest_id == contest, HoldEnd.hold_from == hold.from_)
    )


async def _hold_ended(ctx: Context, contest: ContestId, hold: Hold | None) -> bool:
    return await _hold_end(ctx, contest, hold) is not None


@action
async def hold_state(ctx: Context, organiser: Organiser, contest: ContestId) -> HoldState:
    """Where the contest's hold stands, for an observer of the contest, as
    ending it early answers once that has committed."""
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await published.contest(ctx, contest)
    hold = settings.hold
    if hold is None:
        return HoldState(None, None, holding=False)
    row = await _hold_end(ctx, contest, hold)
    on = holding(hold, ctx.now, row is not None) is not None
    if row is None:
        return HoldState(hold.from_, hold.until, on)
    name = await entrants.username_of(ctx, row.ended_by) or None
    return HoldState(hold.from_, hold.until, on, row.ended_by, name, row.ended_at)


@action
async def end_hold(
    ctx: Context, organiser: Organiser, contest: ContestId, *, confirmed: bool = False
) -> bool:
    """End the contest's hold early, for a manager or admin of the contest:
    from now on every viewer reads every row as it stands. Without
    `confirmed`, nothing changes and `ConfirmationRequired` says what
    becomes visible and that it cannot be undone for this hold. A hold
    already ended, by staff or at its `until`, or not started yet, stays as
    it is, with no row added and nothing asked. `NotFound` for a contest
    with no hold. Whether a row was added; `hold_state` reads the outcome.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    # A save of the settings in flight cannot move the hold's `from` under it.
    await timelines.hold_rules(ctx, contest)
    settings = await published.contest(ctx, contest)
    hold = settings.hold
    if hold is None:
        raise NotFound(NO_HOLD)
    if await _hold_ended(ctx, contest, hold):
        return False
    if holding(hold, ctx.now, ended=False) is None:
        # Not started yet, or past its `until`: nothing to end.
        return False
    if not confirmed:
        raise ConfirmationRequired(_end_words(hold), action="end_hold")
    ctx.db.add(
        HoldEnd(
            contest_id=contest,
            hold_from=hold.from_,
            ended_by=organiser.user.id,
            ended_at=ctx.now,
        )
    )
    await ctx.db.flush()
    log.info("boards.hold_ended", contest=contest, by=organiser.user.id)
    return True


def _end_words(hold: Hold) -> str:
    return (
        "Every leaderboard will show every viewer each row as it stands, with the submissions "
        f"made since {in_words(hold.from_)} and the marks on other rows. This hold cannot be "
        "put back; a hold written again with another from holds afresh."
    )


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
) -> tuple[ContestDefinition, Viewer]:
    """The contest's settings, and who the reader is to its boards: staff,
    an observer of the contest or above; the member of an approved entrant,
    with that row as their own; or anyone else. `NotFound` for a contest
    they may not see."""
    settings = await published.contest(ctx, contest)
    if session is None:
        if not rules.contest_visible_to(
            settings, has_session=False, is_contestant=False, is_organiser=False
        ):
            raise NotFound(published.NO_SUCH_CONTEST)
        return settings, Viewer()
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


__all__ = [
    "NO_HOLD",
    "ContestCheck",
    "HoldState",
    "Side",
    "check_contest",
    "end_hold",
    "hold_state",
    "own_rows",
    "read",
    "regrade_sides",
    "seen",
    "worked_out",
]
