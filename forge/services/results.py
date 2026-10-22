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
deleted, and the latest row is the task's state (`published.results_state`).
An action that would not change the state adds no row and answers the state
as it is. Each asks first: sent without its confirmation, it changes nothing
and answers, as `ConfirmationRequired`, the words to show. Only a manager or
admin of the task does either, a role at its contest or org included. Each
nudges the task's entrants, so an open page reads again.
"""

from typing import Final

from forge.db.tables import Release
from forge.db.tables.releases import HIDDEN_AGAIN, RELEASED
from forge.domain import release as rules
from forge.domain.errors import ConfirmationRequired, NotARunTask, NotFound, PortError
from forge.domain.ids import ContestId, TaskId
from forge.domain.live import Nudge, NudgeKind
from forge.domain.roles import Role, contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import published, timelines
from forge.services.access import Organiser, require
from forge.services.published import PublishedTask, ResultsState

log = get_logger(__name__)

TAKE_BACK_WARNING: Final = (
    "Entrants may already have seen these results; hiding them again won't undo that."
)
NO_PUBLICATION: Final = "The task has no publication yet. Save it so that it publishes."
NOT_A_RUN_TASK: Final = (
    "Only a task that grades by runs has results to release: its scoring's show lists what "
    "a release shows under on_release."
)


@action
async def release(
    ctx: Context, organiser: Organiser, task: TaskId, *, confirmed: bool = False
) -> ResultsState:
    """Release the task's results: from now on every entrant is shown what
    its `show` lists under `on_release`. Without `confirmed`, nothing
    changes and `ConfirmationRequired` says what becomes visible, to whom,
    and that it can be taken back. A task already released is answered as
    it stands, with no row added.
    """
    found = await _held(ctx, organiser, task)
    state = await published.results_state(ctx, task)
    if state.released:
        return state
    if not confirmed:
        raise ConfirmationRequired(_release_words(found), action="release")
    return await _act(ctx, organiser, found, RELEASED)


@action
async def hide_again(
    ctx: Context, organiser: Organiser, task: TaskId, *, confirmed: bool = False
) -> ResultsState:
    """Take the release of the task's results back: from now on the task
    works as if it had never been released. Without `confirmed`, nothing
    changes and `ConfirmationRequired` carries `TAKE_BACK_WARNING`. A task
    not released is answered as it stands, with no row added.
    """
    found = await _held(ctx, organiser, task)
    state = await published.results_state(ctx, task)
    if not state.released:
        return state
    if not confirmed:
        raise ConfirmationRequired(TAKE_BACK_WARNING, action="hide_again")
    return await _act(ctx, organiser, found, HIDDEN_AGAIN)


async def _held(ctx: Context, organiser: Organiser, task: TaskId) -> PublishedTask:
    """The task as its latest publication froze it, held alone as a save
    holds it, so a submit or a regrade in flight reads the state before or
    after the action, never during it. `NotFound` for a task with no
    publication, and `NotARunTask` for one that does not grade by runs.
    """
    require(organiser, task_scope(task), Role.MANAGER)
    await timelines.hold_task(ctx, task)
    found = await published.task(ctx, task)
    if found is None:
        raise NotFound(NO_PUBLICATION)
    if not found.definition.runs:
        raise NotARunTask(NOT_A_RUN_TASK)
    return found


async def _act(ctx: Context, organiser: Organiser, found: PublishedTask, act: str) -> ResultsState:
    ctx.db.add(Release(task_id=found.id, action=act, acted_by=organiser.user.id, acted_at=ctx.now))
    await ctx.db.flush()
    contest = contest_id_of(task_scope(found.id))
    ctx.nudge(
        Nudge(
            NudgeKind.RESULTS,
            found.id,
            scope=task_scope(found.id),
            contest=contest if await _visible(ctx, contest, found.id) else None,
        )
    )
    log.info("results.acted", task=found.id, action=act, by=organiser.user.id)
    return await published.results_state(ctx, found.id)


async def _visible(ctx: Context, contest: ContestId, task: TaskId) -> bool:
    """Whether the task is released to its contest's entrants now, so they
    may hear of it. A forge that does not answer says no.
    """
    try:
        settings = await published.contest(ctx, contest)
        found = await published.task(ctx, task, settings)
    except PortError:
        return False
    return found is not None and rules.visible(settings, found.name, ctx.now)


def _release_words(found: PublishedTask) -> str:
    scoring = found.definition.scoring
    listed = list(scoring.show.on_release) if scoring is not None else []
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


__all__ = ["NOT_A_RUN_TASK", "TAKE_BACK_WARNING", "hide_again", "release"]
