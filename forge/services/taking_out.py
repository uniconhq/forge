"""Taking a task out of its contest (TASK-FORMAT.md section 1.1, #450), by a
manager of the contest, since a task's entry in `contest.yaml` is a
manager's key.

- **Removed**, while nobody has submitted to it: its entry leaves `tasks`,
  so the letters after it move up, and the task itself goes as a failed
  `create` is undone: its activation at the CI, then its place at the forge
  with its roles, and its name, so the name may be taken again. What still
  named it goes with it: an entrant's extension that names it no longer
  does, and an invite to one of its roles still waiting is withdrawn. A
  leaderboard that names it in its `tasks` refuses it
  (`take_out_refused`): taking it out of the board is the organisers' call.
- **Withdrawn**, once anyone has: its entry stays, marked `withdrawn: true`,
  so it keeps its place and its letter, and every reader of the contest's
  tasks skips it for entrants (`forge.domain.release`): they no longer see
  it, its statement, its announcements or their own submissions to it, and
  a submit to it is refused as `task_withdrawn`; no leaderboard counts it,
  so the totals drop its points (`board_checks.covered_tasks`). Its
  submissions, gradings and history stay for the organisers, who see it in
  their task list as withdrawn and can **put it back** (`put_back`), after
  which it scores exactly as before. Putting back is a save of the
  contest's settings like any other, checked the same way.

Which of the two it is depends on whether any submission to the task exists,
any entrant's, whatever became of it. The organisers' dialog says which
before it asks (`tasks.standing` serves the count), and the request names
the one confirmed: when someone submitted since, or the other way, it is
refused as `take_out_changed` with the count now, so a removal never loses a
submission. The count and the deletion are made with the task held as a
save holds it, which a submit waits for, so no submission lands in between.

Every edit of `contest.yaml` is made in its text as the platform
(`forge.domain.contest_entries`), so its comments and layout stay, and an
edit the file's layout does not allow is refused as `take_out_refused`
with what to do by hand.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, func, select, update

from forge.db.tables import Entrant, Grading, Name
from forge.db.tables import Invite as InviteRow
from forge.domain.content import File
from forge.domain.contest_entries import NoEntry, Unlisted, with_withdrawn, without_entry
from forge.domain.definitions import CONTEST_FILE, parse_contest
from forge.domain.errors import (
    Conflict,
    Forbidden,
    NotFound,
    PortError,
    TakeOutChanged,
    TakeOutRefused,
)
from forge.domain.identity import PLATFORM, AsOrgAccount
from forge.domain.ids import ContestId, OrgId, TaskId
from forge.domain.invites import InviteStatus
from forge.domain.roles import Role, contest_id_of, contest_scope, task_scope
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import boards, making, names, org_accounts, places, published, timelines
from forge.services.access import Organiser, require

log = get_logger(__name__)


class TakeOut(StrEnum):
    """What taking a task out of its contest does: `remove` it, while nobody
    has submitted to it, or `withdraw` it, once anyone has.
    """

    REMOVE = "remove"
    WITHDRAW = "withdraw"


def take_out_of(submissions: int) -> TakeOut:
    """What taking out a task with `submissions` submissions does."""
    return TakeOut.REMOVE if submissions == 0 else TakeOut.WITHDRAW


@dataclass(frozen=True, slots=True)
class PutBack:
    """A withdrawn task put back: what each board reports of its tasks now,
    as a save of the contest's settings reports it.
    """

    notes: tuple[str, ...]


async def submission_counts(ctx: Context, tasks: list[TaskId]) -> dict[TaskId, int]:
    """How many submissions each of `tasks` has, any entrant's, whatever
    became of them; a task with none is left out.
    """
    if not tasks:
        return {}
    made = (
        select(Grading.task_id, Grading.workspace_id, Grading.submission_number)
        .where(Grading.task_id.in_(tasks))
        .distinct()
        .subquery()
    )
    rows = await ctx.db.execute(select(made.c.task_id, func.count()).group_by(made.c.task_id))
    return {TaskId(task): count for task, count in rows.tuples()}


@action
async def take_out(ctx: Context, organiser: Organiser, task: TaskId, confirmed: TakeOut) -> TakeOut:
    """Take the task out of its contest as `confirmed` says, and answer what
    was done. Needs the manager role at the contest. `TakeOutChanged` when
    it would now do the other; `TakeOutRefused` when a leaderboard names a
    task to be removed, or the settings cannot be edited line by line;
    `InvalidDefinition` naming `contest.yaml` when the settings do not
    read; `NotFound` for no such task. Withdrawing a task withdrawn already
    changes nothing.
    """
    contest = contest_id_of(task_scope(task))
    require(organiser, contest_scope(contest), Role.MANAGER)
    name = await _name(ctx, task)
    await timelines.hold_rules(ctx, contest)
    await timelines.hold_task(ctx, task)
    count = (await submission_counts(ctx, [task])).get(task, 0)
    would = take_out_of(count)
    if would is not confirmed:
        raise TakeOutChanged(_changed(name, count, would), submissions=count, take_out=would.value)
    found = await _settings_file(ctx, contest)
    if would is TakeOut.WITHDRAW:
        await _withdraw(ctx, contest, found, name, organiser.user.id)
        log.info("tasks.withdrawn", task=task, user_id=organiser.user.id)
        return would
    await _remove(ctx, contest, found, task, name, organiser.user.id)
    log.info("tasks.removed", task=task, user_id=organiser.user.id)
    return would


@action
async def put_back(ctx: Context, organiser: Organiser, task: TaskId) -> PutBack:
    """Put a withdrawn task back in its contest, as a save of the contest's
    settings taking `withdrawn` out of its entry would, checked the same
    way: `InvalidDefinition` naming `contest.yaml` with what a leaderboard
    or a timeline refuses. Needs the manager role at the contest.
    `TakeOutRefused` when the entry cannot be edited line by line. A task
    not withdrawn changes nothing.
    """
    contest = contest_id_of(task_scope(task))
    require(organiser, contest_scope(contest), Role.MANAGER)
    name = await _name(ctx, task)
    await timelines.hold_rules(ctx, contest)
    found = await _settings_file(ctx, contest)
    updated = _edited(name, lambda: with_withdrawn(found.content, name, False))
    if updated is None:
        return PutBack(())
    before, after = parse_contest(found.content), parse_contest(updated, saved=True)
    problems = await timelines.check_contest(ctx, contest, before, after)
    checked = await boards.check_contest(ctx, contest, after)
    problems += checked.problems
    if problems:
        raise InvalidDefinition(CONTEST_FILE, problems)
    await _write(ctx, contest, found, updated, f"Put {name} back in the contest", organiser.user.id)
    places.ahead_at(ctx, task)
    log.info("tasks.put_back", task=task, user_id=organiser.user.id)
    return PutBack(tuple(checked.notes))


async def _name(ctx: Context, task: TaskId) -> str:
    found = (await names.names_of(ctx, [task])).get(task)
    if found is None:
        raise NotFound(names.NO_SUCH_TASK)
    return found


def _changed(name: str, count: int, would: TakeOut) -> str:
    if would is TakeOut.WITHDRAW:
        said = f"{count} submission" + ("s" if count != 1 else "")
        return (
            f"{name} has {said} now, so it can no longer be removed, only withdrawn: hidden "
            "from entrants and counted on no leaderboard, its submissions kept."
        )
    return f"Nobody has submitted to {name}, so it would be removed, not withdrawn."


async def _settings_file(ctx: Context, contest: ContestId) -> File:
    """`contest.yaml` as it stands, read as the platform, once its settings
    read: `InvalidDefinition` naming it with every problem otherwise.
    """
    try:
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
    except NotFound as exc:
        raise NotFound(names.NO_SUCH_CONTEST) from exc
    parse_contest(found.content)
    return found


def _edited(name: str, edit: Callable[[], bytes | None]) -> bytes | None:
    """`edit` of `contest.yaml`, refused in words when the file's layout does
    not allow it.
    """
    try:
        return edit()
    except NoEntry:
        raise TakeOutRefused(
            f"The contest's settings do not list {name}. Add an entry for it to their tasks by "
            "hand first."
        ) from None
    except Unlisted as exc:
        log.warning("tasks.not_edited", task=name, reason=str(exc))
        raise TakeOutRefused(
            f"The contest's settings list {name} in a way that cannot be changed line by line. "
            f"Change its entry in the settings by hand."
        ) from None


async def _write(
    ctx: Context, contest: ContestId, found: File, updated: bytes, message: str, author: int
) -> None:
    """`updated` written over `contest.yaml` by the platform, the manager
    who asked named as the change's author, as every organiser's change is.
    """
    await ctx.forge.content.write_file(
        PLATFORM,
        contest,
        CONTEST_FILE,
        updated,
        message=message,
        expected=found.token,
        author=author,
    )
    published.forget_contests(ctx)
    places.forget_tasks(ctx, contest)


async def _withdraw(ctx: Context, contest: ContestId, found: File, name: str, author: int) -> None:
    updated = _edited(name, lambda: with_withdrawn(found.content, name, True))
    if updated is not None:
        await _write(ctx, contest, found, updated, f"Withdraw {name} from the contest", author)


async def _remove(
    ctx: Context, contest: ContestId, found: File, task: TaskId, name: str, author: int
) -> None:
    """The task's entry out of `tasks`, then the task at the CI and the
    forge, the one step that cannot be taken back last; what was done
    before a step that fails is put back, as `create` undoes a try.
    """
    settings = parse_contest(found.content)
    naming = [
        board.name
        for board in settings.leaderboards
        if board.tasks is not None and name in board.tasks
    ]
    if naming:
        raise TakeOutRefused(
            f"The leaderboard {naming[0]} counts {name} by name. Take {name} out of that "
            "leaderboard first, then remove the task.",
            boards=naming,
        )
    made = making.undo_on_rollback(ctx, "tasks", task=task)
    try:
        updated = _edited(name, lambda: without_entry(found.content, name))
        if updated is not None:
            await _write(ctx, contest, found, updated, f"Remove {name} from the contest", author)
            made.add("contest_entry", contest, _restore(ctx, contest, found, updated))
        account = await org_accounts.identity(ctx, OrgId(contest_scope(contest).org))
        account = await _deactivate(ctx, account, task)

        async def reactivate() -> None:
            await ctx.forge.grading.activate(account, task)

        made.add("activation", task, reactivate)
        await _forget(ctx, contest, task, name)
        await ctx.forge.content.delete_place(task)
    except PortError as exc:
        raise making.failure(exc, "tasks.remove_failed", task=task) from None


async def _deactivate(ctx: Context, account: AsOrgAccount, task: TaskId) -> AsOrgAccount:
    try:
        await ctx.forge.grading.deactivate(account, task)
    except Forbidden:
        account = await org_accounts.renew(ctx, account)
        await ctx.forge.grading.deactivate(account, task)
    return account


def _restore(ctx: Context, contest: ContestId, found: File, updated: bytes) -> making.Removal:
    """Put the entry back while the file still says what the removal wrote."""

    async def restore() -> None:
        now = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
        if now.content != updated:
            raise Conflict(f"{CONTEST_FILE} changed after the task was taken out")
        await ctx.forge.content.write_file(
            PLATFORM,
            contest,
            CONTEST_FILE,
            found.content,
            message="Put the removed task back",
            expected=now.token,
        )
        published.forget_contests(ctx)

    return restore


async def _forget(ctx: Context, contest: ContestId, task: TaskId, name: str) -> None:
    """What the platform keeps that names the task: its name, which may be
    taken again; each entrant's extension naming it, which then names it no
    more; and every invite to one of its roles still waiting, withdrawn.
    """
    await ctx.db.execute(delete(Name).where(Name.id == task))
    rows = await ctx.db.scalars(select(Entrant).where(Entrant.contest_id == contest))
    for row in rows:
        if row.extension_tasks is not None and name in row.extension_tasks:
            row.extension_tasks = [each for each in row.extension_tasks if each != name]
    await ctx.db.execute(
        update(InviteRow)
        .where(InviteRow.scope == task_scope(task).path, InviteRow.status == InviteStatus.PENDING)
        .values(status=InviteStatus.WITHDRAWN, decided_at=ctx.now)
    )
    await ctx.db.flush()
