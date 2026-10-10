"""The contest's boards, computed on every read from the stored gradings, the
latest publication of each covered task and the contest's settings
(`forge.domain.boards`), and handed to each viewer as their audience may see
them (TASK-FORMAT.md sections 1.1 and 3.6).

- **Who reads.** A board's `who` names the least audience that sees it:
  `everyone`, anyone the contest's visibility admits, a visitor with no
  session included; `contestants`, approved contestants and organisers;
  `organisers`, observers of the contest and above. A person holding a role
  only at one of the contest's tasks reads as a contestant would without a
  row, since they observe no board.
- **One ranking.** Every viewer of a board sees the same rows in the same
  order: the board's `rows` only cuts which they are given, with the
  viewer's own row below the top `n`; which submission a cell counts and
  how many are still grading are given in the viewer's own row only.
- **Organisers** read every board as `now`, exactly what its audience sees,
  every row given, and `final`, as it will read once every task has
  revealed; picking a row gives each board it sees, `now` as it sees it.
  Beside them, what the boards ask of the covered tasks that does not hold
  (C4), and each board ranking `points` that counts nothing from a task.
- **An archived contest** is read by those who could read it published,
  and by its approved contestants.

A row is an approved contestant in no team, or a team with an approved
member: the rows a contest's reveal waits for (`timelines.rows`). Each row
is named by its username or its team's name.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from forge.db.tables import Grading, Team
from forge.domain import release as rules
from forge.domain.board_checks import (
    Breach,
    Covered,
    breaches,
    counts_nothing,
    counts_nothing_message,
    covered,
    covered_tasks,
)
from forge.domain.boards import (
    Audience,
    Column,
    Entry,
    Row,
    Standings,
    State,
    for_viewer,
    keys_of,
    rank,
    sees,
)
from forge.domain.definitions import ContestDefinition, Leaderboard, Select
from forge.domain.definitions import State as ContestState
from forge.domain.errors import NotFound, PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, PublicationId, TaskId, WorkspaceId
from forge.domain.names import TeamOwner, UserOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.release import Extension, reveal_of
from forge.domain.roles import Role, contest_scope, holds
from forge.domain.scoring import Better, exact
from forge.domain.sessions import Session
from forge.domain.yaml_models import Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    gradings,
    marks,
    names,
    published,
    release,
    scores,
    teams,
    timelines,
)
from forge.services.access import Organiser, require
from forge.services.published import PublishedTask

log = get_logger(__name__)

NO_SUCH_ROW = "No such contestant or team in this contest."


@dataclass(frozen=True, slots=True)
class OrganisedBoard:
    """A board as organisers read it: `now`, every row, or as the picked row
    sees it; `final`; and what it asks of its tasks that does not hold.
    """

    now: Standings
    final: Standings
    notes: tuple[str, ...]


@action
async def seen(
    ctx: Context, session: Session | None, contest: ContestId, board: str | None = None
) -> tuple[Standings, ...]:
    """Every board of the contest the reader's audience sees, in the order
    the file gives, each as they are given it, or only the one named
    `board`. A visitor reads with no session. `NotFound` for a contest the
    reader may not see.
    """
    settings, audience, own = await _reader(ctx, session, contest)
    boards = [
        each
        for each in settings.leaderboards
        if sees(each, audience) and (board is None or each.name == board)
    ]
    if not boards:
        return ()
    read = await _read(ctx, contest, settings, await _covered_tasks(ctx, contest, settings, boards))
    every_row = audience is Audience.ORGANISERS
    return tuple(
        for_viewer(standings, standings.board.rows, own, every_row=every_row)
        for standings in _ranked(settings, boards, read, final=False)
    )


@action
async def organised(
    ctx: Context, organiser: Organiser, contest: ContestId, row: WorkspaceOwner | None = None
) -> tuple[OrganisedBoard, ...]:
    """Every board of the contest as `now` and `final`, every row given, or
    each board `row` sees, `now` as it sees it. Needs the observer role at
    the contest; `NotFound` for a `row` that is not one of the contest's.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await published.contest(ctx, contest)
    boards = [
        board for board in settings.leaderboards if row is None or sees(board, Audience.CONTESTANTS)
    ]
    if row is not None and row not in await timelines.rows(ctx, contest):
        raise NotFound(NO_SUCH_ROW)
    if not boards:
        return ()
    tasks = await _covered_tasks(ctx, contest, settings, boards)
    read = await _read(ctx, contest, settings, tasks)
    now = _ranked(settings, boards, read, final=False)
    final = _ranked(settings, boards, read, final=True)
    shapes = {task.name: _covered(task) for task in tasks}
    found: list[OrganisedBoard] = []
    for board, current, last in zip(boards, now, final, strict=True):
        if row is not None:
            current = for_viewer(current, board.rows, row)
        found.append(OrganisedBoard(current, last, tuple(_notes(settings, board, shapes))))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ContestCheck:
    """What C4 and C1 found of a save of the contest's settings: each
    refusal, and, for a save none refuses, what each board reports of its
    tasks, the notes the organisers' reading carries beside it.
    """

    problems: list[Problem]
    notes: list[str]


async def check_contest(
    ctx: Context, contest: ContestId, settings: ContestDefinition
) -> ContestCheck:
    """C4 for a save of the contest's settings, what each board asks of the
    latest publication of every task it covers, each refusal at the
    board's line, and each board's notes; and C1's last rule, a task's marks
    not lowered below the marks a row holds on it.
    """
    problems = await _held_marks(ctx, contest, settings)
    if not settings.leaderboards:
        return ContestCheck(problems, [])
    tasks = await _covered_tasks(ctx, contest, settings, settings.leaderboards)
    shapes = {task.name: _covered(task) for task in tasks}
    notes: list[str] = []
    for index, board in enumerate(settings.leaderboards):
        for breach in _breaches(settings, board, shapes):
            problems.append(Problem(path=_board_path(index, board, breach), message=breach.message))
        notes += _notes(settings, board, shapes)
    return ContestCheck(problems, notes)


async def _held_marks(
    ctx: Context, contest: ContestId, settings: ContestDefinition
) -> list[Problem]:
    listed = await names.named(ctx, await ctx.forge.content.list_tasks(PLATFORM, contest))
    ids = {each.name: TaskId(each.id) for each in listed}
    problems: list[Problem] = []
    for index, entry in enumerate(settings.tasks):
        allowed = settings.marks_of(entry)
        if allowed is None or entry.id not in ids:
            continue
        most, rows = await marks.most_held(ctx, ids[entry.id])
        if most <= allowed:
            continue
        held = f"{rows} {'row holds' if rows == 1 else 'rows hold'} {most} marks on {entry.id}"
        path = f"tasks[{index}].marks" if entry.marks is not None else f"tasks[{index}]"
        problems.append(Problem(path=path, message=f"{held}; it cannot take fewer."))
    return problems


async def check_task(
    ctx: Context, contest: ContestId, settings: ContestDefinition, name: str, shape: Covered
) -> list[Problem]:
    """T8 for a save of the task `name` that leaves it as `shape`: what each
    board covering it asks of it beside the other covered tasks as their
    latest publications stand, each refusal at the line of `task.yaml` that
    would mend it. A task the contest does not list yet is checked against
    the boards that cover every task, which its appended entry joins.
    """
    boards = [board for board in settings.leaderboards if board.tasks is None or board.covers(name)]
    if not boards:
        return []
    tasks = await _covered_tasks(ctx, contest, settings, boards)
    others = {task.name: _covered(task) for task in tasks if task.name != name}
    problems: list[Problem] = []
    for board in boards:
        names = [*covered_tasks(settings, board)]
        if name not in names:
            names.append(name)
        shapes = {each: others[each] for each in names if each in others}
        shapes[name] = shape
        ordered = {each: shapes[each] for each in names if each in shapes}
        for breach in breaches(board, ordered, many=len(names) > 1, only=name):
            problems.append(Problem(path=breach.mended_in, message=breach.message))
    return problems


def task_notes(settings: ContestDefinition, name: str, shape: Covered) -> list[str]:
    """T9's report of each board ranking `points` that counts nothing from
    the task `name` as it is saved.
    """
    found: list[str] = []
    for board in settings.leaderboards:
        if board.tasks is not None and not board.covers(name):
            continue
        if counts_nothing(board, {name: shape}):
            found.append(counts_nothing_message(board, name))
    return found


def _board_path(index: int, board: Leaderboard, breach: Breach) -> str:
    if breach.key is not None:
        return f"leaderboards[{index}].order[{breach.key}]"
    if breach.mended_in == "test_groups" and board.select is Select.MARKED:
        return f"leaderboards[{index}].select"
    return f"leaderboards[{index}].over"


def _breaches(
    settings: ContestDefinition, board: Leaderboard, shapes: Mapping[str, Covered]
) -> list[Breach]:
    names = covered_tasks(settings, board)
    ordered = {name: shapes[name] for name in names if name in shapes}
    return breaches(board, ordered, many=len(names) > 1)


def _notes(
    settings: ContestDefinition, board: Leaderboard, shapes: Mapping[str, Covered]
) -> list[str]:
    names = covered_tasks(settings, board)
    ordered = {name: shapes[name] for name in names if name in shapes}
    found = [breach.message for breach in _breaches(settings, board, shapes)]
    found += [counts_nothing_message(board, task) for task in counts_nothing(board, ordered)]
    return found


def _covered(task: PublishedTask) -> Covered:
    return covered(task.publication.measures, task.definition.test_groups)


async def _reader(
    ctx: Context, session: Session | None, contest: ContestId
) -> tuple[ContestDefinition, Audience, WorkspaceOwner | None]:
    """The contest's settings, the reader's audience and their own row."""
    if session is None:
        settings = await published.contest(ctx, contest)
        if not rules.contest_visible_to(
            settings, has_session=False, is_contestant=False, is_organiser=False
        ):
            raise NotFound(published.NO_SUCH_CONTEST)
        return settings, Audience.EVERYONE, None
    settings = await published.contest(ctx, contest)
    person = await release.reader(ctx, session, contest)
    approved = person.row is not None and person.row.status == Status.APPROVED
    entered = settings.state is ContestState.ARCHIVED and approved
    if not release.sees(settings, person) and not entered:
        raise NotFound(published.NO_SUCH_CONTEST)
    if person.organises:
        try:
            grants = await ctx.forge.orgs.roles_of_user(session.user_id)
        except NotFound:
            grants = ()
        if holds(grants, contest_scope(contest), Role.OBSERVER):
            return settings, Audience.ORGANISERS, None
    if approved:
        standing = await teams.standing(ctx, contest, session.user_id)
        return settings, Audience.CONTESTANTS, standing.owner
    if person.organises:
        return settings, Audience.CONTESTANTS, None
    return settings, Audience.EVERYONE, None


async def _covered_tasks(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    boards: Sequence[Leaderboard],
) -> list[PublishedTask]:
    """Every task the contest lists that one of `boards` covers and that has
    a publication, in the contest's order.
    """
    wanted = {name for board in boards for name in covered_tasks(settings, board)}
    return [
        task
        for task in await published.tasks(ctx, contest, settings)
        if task.entry is not None and task.name in wanted
    ]


@dataclass(frozen=True, slots=True)
class _Read:
    """What the boards are ranked from, read once for every board and both
    of organisers' readings: the covered tasks, their columns, and every row
    with its submissions.
    """

    tasks: Sequence[PublishedTask]
    columns: Mapping[str, Column]
    rows: Sequence[Row]


async def _read(
    ctx: Context, contest: ContestId, settings: ContestDefinition, tasks: Sequence[PublishedTask]
) -> _Read:
    owners = await timelines.rows(ctx, contest)
    extensions = await timelines.of_owners(ctx, contest, owners)
    columns = {task.name: _column(ctx, settings, task, list(extensions.values())) for task in tasks}
    names = await _names(ctx, owners)
    entries = await _entries(ctx, contest, settings, tasks, owners, extensions)
    rows = [Row(owner, names.get(owner, ""), entries.get(owner, {})) for owner in owners]
    return _Read(tasks, columns, rows)


def _ranked(
    settings: ContestDefinition, boards: Sequence[Leaderboard], read: _Read, *, final: bool
) -> list[Standings]:
    """Every one of `boards` ranked now, or `final`, every row given."""
    found: list[Standings] = []
    for board in boards:
        covering = [
            read.columns[name] for name in covered_tasks(settings, board) if name in read.columns
        ]
        shown = {column.name for column in covering}
        keys = keys_of(board, _directions([task for task in read.tasks if task.name in shown]))
        found.append(rank(board, keys, settings.start, covering, read.rows, final=final))
    return found


def _directions(tasks: Sequence[PublishedTask]) -> dict[str, Better | None]:
    """Each value key's direction, as the first covered task giving it
    one says; the checks keep every covered task agreeing.
    """
    found: dict[str, Better | None] = {}
    for task in tasks:
        for name, measure in task.publication.measures.items():
            if found.get(name) is None and measure.better is not None:
                found[name] = measure.better
    return found


def _column(
    ctx: Context, settings: ContestDefinition, task: PublishedTask, extensions: Sequence[Extension]
) -> Column:
    entry = task.entry
    assert entry is not None
    reveal_at = reveal_of(settings, entry, extensions)
    return Column(
        name=task.name,
        label=task.label or task.name,
        worth=exact(task.worth),
        shows={name: group.shown for name, group in task.definition.test_groups.items()},
        release_at=settings.release_of(entry),
        released=rules.released(settings, task.name, ctx.now),
        reveal_at=reveal_at,
        revealed=ctx.now >= reveal_at,
    )


async def _names(ctx: Context, owners: Sequence[WorkspaceOwner]) -> dict[WorkspaceOwner, str]:
    """Each row's name: a team's own, a contestant's username; a username
    the forge does not give is left empty.
    """
    team_ids = sorted({owner.team_id for owner in owners if isinstance(owner, TeamOwner)})
    found: dict[WorkspaceOwner, str] = {}
    if team_ids:
        for team_id, name in await ctx.db.execute(
            select(Team.id, Team.name).where(Team.id.in_(team_ids))
        ):
            found[TeamOwner(team_id)] = name
    await ctx.let_go()
    for owner in owners:
        if isinstance(owner, UserOwner):
            try:
                found[owner] = (await ctx.forge.identity.find_user(owner.user_id)).username
            except PortError:
                found[owner] = ""
    return found


async def _entries(
    ctx: Context,
    contest: ContestId,
    settings: ContestDefinition,
    tasks: Sequence[PublishedTask],
    owners: Sequence[WorkspaceOwner],
    extensions: Mapping[WorkspaceOwner, Extension],
) -> dict[WorkspaceOwner, dict[str, tuple[Entry, ...]]]:
    """Every row's submissions to each task, each where it stands on a
    board, scored when it is a candidate.
    """
    workspaces = {ctx.forge.workspaces.workspace_of(contest, owner): owner for owner in owners}
    marked = await marks.of_tasks(ctx, [task.id for task in tasks])
    found: dict[WorkspaceOwner, dict[str, tuple[Entry, ...]]] = {}
    for task in tasks:
        rows = (
            await ctx.db.scalars(
                select(Grading).where(
                    Grading.task_id == task.id,
                    Grading.workspace_id.in_(sorted(workspaces)),
                )
            )
        ).all()
        grouped: dict[tuple[str, int], list[Grading]] = {}
        for row in rows:
            grouped.setdefault((row.workspace_id, row.submission_number), []).append(row)
        latest = [max(each, key=lambda row: row.attempt) for each in grouped.values()]
        lost = await gradings.lost(ctx, latest)
        scorer = scores.Scorer(ctx, task, settings.on_system_error)
        by_owner: dict[WorkspaceOwner, list[Entry]] = {}
        for (workspace, number), attempts in grouped.items():
            owner = workspaces[WorkspaceId(workspace)]
            at = min(row.submitted_at for row in attempts)
            entry = await _entry(
                ctx,
                scorer,
                settings,
                task,
                number,
                at,
                attempts,
                lost,
                extensions[owner],
                number in marked.get((WorkspaceId(workspace), task.id), set()),
            )
            by_owner.setdefault(owner, []).append(entry)
        for owner, listed in by_owner.items():
            found.setdefault(owner, {})[task.name] = tuple(listed)
    return found


async def _entry(
    ctx: Context,
    scorer: scores.Scorer,
    settings: ContestDefinition,
    task: PublishedTask,
    number: int,
    at: datetime,
    attempts: Sequence[Grading],
    lost: frozenset[uuid.UUID],
    extension: Extension,
    marked: bool,
) -> Entry:
    usable = scores.usable(ctx, attempts, lost, settings.on_system_error)
    if usable.state is not State.CANDIDATE or usable.row is None:
        return Entry(number, at, usable.state, marked=marked)
    graded = await scorer.graded(PublicationId(usable.row.publication_id))
    if graded is None:
        return Entry(number, at, State.VOID, marked=marked)
    state = scores.stands(usable.row, graded)
    if state is not State.CANDIDATE:
        return Entry(number, at, state, marked=marked)
    late = scores.factor(settings, task, extension, at)
    return Entry(number, at, state, await scorer.scored(usable.row, graded, late), marked)
