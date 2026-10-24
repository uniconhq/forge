"""Staff releasing a run task's results, and taking a release back
(SIMPLER-CORE.md 9.4, stage 5 section 6 items 2 and 10).

A release shows every entrant what the task's `show` lists under
`on_release`, from then on: its values, its groups' runs and its files.
Gradings made while it stands take the plan's released split, which runs
what a release shows early (`RunPlan.for_split`). A take-back puts the task
back as if it had never been released: `on_release` is hidden again from
every serving path at once, gradings from then on take the other split, and
an extension is allowed again. A grading already made keeps the split it
was made with, so neither action grades anything again.

Each action is a `releases` row with who and when, never changed or
deleted, and the latest row is the task's state (`state_of`). An action
that would not change the state adds no row. Each asks first: sent without
its confirmation, it changes nothing and answers, as
`ConfirmationRequired`, the words to show. Only a manager or admin of the
task does either, a role at its contest or org included. Each nudges the
task's entrants, so an open page reads again. Neither answers the history:
`state` reads it once the action has committed, outside the task's hold.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select

from forge.db.tables import Release
from forge.db.tables.releases import HIDDEN_AGAIN, RELEASED
from forge.domain import release as rules
from forge.domain.errors import ConfirmationRequired, NotFound, PortError
from forge.domain.ids import ContestId, TaskId
from forge.domain.live import Nudge, NudgeKind
from forge.domain.roles import Role, contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrant_view, entrants, timelines
from forge.services.access import Organiser, require
from forge.services.entrant_view import PublishedTask

log = get_logger(__name__)

TAKE_BACK_WARNING: Final = (
    "Entrants may already have seen these results; hiding them again won't undo that."
)
NO_PUBLICATION: Final = "The task has no publication yet. Save it so that it publishes."


@dataclass(frozen=True, slots=True)
class ResultsAction:
    """One staff action on a task's results: `released` or `hidden_again`,
    by whom, by user id and `by_name`, their username, none when the forge
    does not say, and when.
    """

    action: str
    by: int
    by_name: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class ResultsState:
    """Where a task's results stand: whether they are released now, and
    every release and take-back, oldest first.
    """

    released: bool
    history: tuple[ResultsAction, ...]


async def state_of(ctx: Context, task: TaskId) -> ResultsState:
    """Where the task's results stand now, read afresh, in the order the
    rows were written (`releases.seq`): released when its latest action is
    a release, as `published.results_released` reads it. Each organiser's
    username is asked of the forge once, so this is never called under the
    task's hold.
    """
    rows = (
        (await ctx.db.execute(select(Release).where(Release.task_id == task).order_by(Release.seq)))
        .scalars()
        .all()
    )
    names: dict[int, str | None] = {}
    for row in rows:
        if row.acted_by not in names:
            names[row.acted_by] = await entrants.username_of(ctx, row.acted_by) or None
    history = tuple(
        ResultsAction(row.action, row.acted_by, names[row.acted_by], row.acted_at) for row in rows
    )
    return ResultsState(bool(history) and history[-1].action == RELEASED, history)


@action
async def state(ctx: Context, organiser: Organiser, task: TaskId) -> ResultsState:
    """Where the task's results stand, for an observer of the task, as a
    release or a take-back answers once it has committed.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    return await state_of(ctx, task)


@action
async def release(
    ctx: Context, organiser: Organiser, task: TaskId, *, confirmed: bool = False
) -> bool:
    """Release the task's results: from now on every entrant is shown what
    its `show` lists under `on_release`. Without `confirmed`, nothing
    changes and `ConfirmationRequired` says what becomes visible, to whom,
    and that it can be taken back. A task already released stays as it is,
    with no row added. Whether a row was added; `state` reads the outcome.
    """
    return await _act(ctx, organiser, task, RELEASED, confirmed=confirmed)


@action
async def hide_again(
    ctx: Context, organiser: Organiser, task: TaskId, *, confirmed: bool = False
) -> bool:
    """Take the release of the task's results back: from now on the task
    works as if it had never been released. Without `confirmed`, nothing
    changes and `ConfirmationRequired` carries `TAKE_BACK_WARNING`. A task
    not released stays as it is, with no row added. Whether a row was
    added; `state` reads the outcome.
    """
    return await _act(ctx, organiser, task, HIDDEN_AGAIN, confirmed=confirmed)


async def _act(
    ctx: Context, organiser: Organiser, task: TaskId, act: str, *, confirmed: bool
) -> bool:
    """`act` done to the task's results when it changes their state. What
    the forge is asked, the task and whether its entrants may hear of it,
    is asked before the task is held alone, as a save holds it, so a submit
    or a regrade in flight never waits on the forge, and reads the state
    before or after the action, never during it. `NotFound` for a task with
    no publication.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    found = await entrant_view.task(ctx, task)
    if found is None:
        raise NotFound(NO_PUBLICATION)
    contest = contest_id_of(task_scope(task))
    heard = await _visible(ctx, contest, task)
    await timelines.hold_task(ctx, task)
    if await entrant_view.results_released(ctx, task) == (act == RELEASED):
        return False
    if not confirmed:
        if act == RELEASED:
            raise ConfirmationRequired(_release_words(found), action="release")
        raise ConfirmationRequired(TAKE_BACK_WARNING, action="hide_again")
    ctx.db.add(Release(task_id=task, action=act, acted_by=organiser.user.id, acted_at=ctx.now))
    await ctx.db.flush()
    ctx.nudge(
        Nudge(NudgeKind.RESULTS, task, scope=task_scope(task), contest=contest if heard else None)
    )
    log.info("results.acted", task=task, action=act, by=organiser.user.id)
    return True


async def _visible(ctx: Context, contest: ContestId, task: TaskId) -> bool:
    """Whether the task is released to its contest's entrants now, so they
    may hear of it. A forge that does not answer says no.
    """
    try:
        settings = await entrant_view.contest(ctx, contest)
        found = await entrant_view.task(ctx, task, settings)
    except PortError:
        return False
    return found is not None and rules.visible(settings, found.name, ctx.now)


def _release_words(found: PublishedTask) -> str:
    scoring = found.definition.scoring
    listed = list(scoring.show.on_release)
    if not listed:
        shows = "Its scoring's show lists nothing under on_release, so nothing more is shown."
    else:
        shows = (
            f"Every entrant of {found.name} will be shown, from now on, what its scoring's show "
            f"lists under on_release: {', '.join(listed)}."
        )
    return (
        f"{shows} Gradings from now on run those runs early. You can hide the results again "
        "later, though entrants may have seen them by then."
    )


__all__ = [
    "TAKE_BACK_WARNING",
    "ResultsAction",
    "ResultsState",
    "hide_again",
    "release",
    "state",
    "state_of",
]
