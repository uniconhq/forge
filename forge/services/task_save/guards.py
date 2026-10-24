import builtins
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

from forge.domain.content import Edit, FileSet, WrittenEdit, check_path
from forge.domain.definitions import (
    ADMIN_ONLY_FILES,
    TASK_FILE,
    Show,
    State,
    TaskDefinition,
    admin_only_changes,
    admin_only_refusal,
)
from forge.domain.errors import AdminOnly, InvalidInputs, ReservedPath
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.leaderboard_task_check import Scored, across, leaderboards_of
from forge.domain.port_values import is_reserved
from forge.domain.publication_record import Publication
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.run_task import (
    RunTask,
    fewer_runs,
    hidden_again,
    most_shown,
    openness,
    shown_under,
    unreleased,
)
from forge.domain.yaml_models import Problem
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrant_view, names, timelines
from forge.services.access import Organiser
from forge.services.task_save import sources
from forge.services.task_save.records import OPENNESS, SHOWN_WORDS
from forge.services.task_save.sources import _content, _groups_of
from forge.services.task_save.writing import _contest

log = get_logger(__name__)


async def _shown_before(ctx: Context, task: TaskId, definition: TaskDefinition) -> list[Problem]:
    """T7 and T10: once the task has a graded submission, a group shown
    `always` or `verdict` under any publication a graded submission ran
    under is not hidden again, nor `always` made `verdict`, so a group
    removed and added back cannot come back hidden; and a group the save
    adds to the latest publication's says its `show`.
    """
    graded = await sources._graded_under(ctx, task)
    if not graded:
        return []
    publications = await ctx.forge.workspaces.list_publications(task)
    if not publications:
        return []
    most_open: dict[str, Show] = {}
    for publication in publications:
        if publication.id not in graded:
            continue
        for name, group in (await _groups_of(ctx, task, publication)).items():
            was = most_open.get(name)
            if was is None or OPENNESS[group.shown] > OPENNESS[was]:
                most_open[name] = group.shown
    latest = await _groups_of(ctx, task, publications[-1])
    problems: list[Problem] = []
    for name, group in definition.test_groups.items():
        at = f"test_groups.{name}"
        if latest and name not in latest and group.show is None:
            problems.append(
                Problem(
                    path=at,
                    message=f"{name} is a new test group, and the task has graded "
                    "submissions, so choose when entrants see its results.",
                )
            )
            continue
        then = most_open.get(name)
        if then is not None and OPENNESS[group.shown] < OPENNESS[then]:
            problems.append(
                Problem(
                    path=f"{at}.show",
                    message=f"Entrants have seen the results of {name} {SHOWN_WORDS[then]}, "
                    "and what they have seen cannot be hidden again.",
                )
            )
    return problems


async def _shown_before_runs(
    ctx: Context,
    task: TaskId,
    definition: TaskDefinition,
    present: Collection[str],
    run_task: RunTask | None,
) -> list[Problem]:
    """T7 for a run task (SIMPLER-CORE.md 9.4 and 16): what `show` listed
    under any publication a graded submission ran under, or whose files
    were served (`_served`), is listed as soon, or sooner; a folder it
    listed stands for the files that publication held under it, each still
    in `present` shown as soon whichever entry now lists it. What it listed
    under `on_release` counts only while the task's results are released,
    and only from a publication that was the task's own at some time since
    the release that stands, so what no release showed never holds a save
    back, and after a take-back a save may drop it (stage 5 section 6 items
    10 and 41). While released and before the reveal, `on_release` is
    sooner than `after_close` (`openness`), so what a release shows stays
    listed under `now` or `on_release` until the reveal. And a group or
    value shown during the contest reads no fewer of the runs the task
    still has (`fewer_runs`), checked once the task checks (`run_task`).
    """
    if definition.scoring is None:
        return []
    publications = await ctx.forge.workspaces.list_publications(task)
    counted = await sources._graded_under(ctx, task) | await _served(ctx, task, publications)
    if not counted:
        return []
    since = await entrant_view.released_since(ctx, task)
    shown_by_release = (
        set()
        if since is None
        else _own_since(await entrant_view.live(ctx, task, publications), since)
    )
    rank = openness(released=since is not None, revealed=await _task_revealed(ctx, task))
    shown = []
    read = []
    for publication in publications:
        if publication.id not in counted:
            continue
        found = await sources._definition_of(ctx, task, publication)
        if found is None or found.scoring is None:
            continue
        held = await ctx.forge.content.list_files(PLATFORM, task, at=publication.version)
        each = shown_under(found.scoring, held.tokens.keys())
        if publication.id not in shown_by_release:
            each = unreleased(each)
        shown.append(each)
        if run_task is not None:
            runs = await sources._runs_file_of(ctx, task, publication)
            if runs is not None:
                read.append((each, runs.reads))
    problems = hidden_again(most_shown(shown, rank), definition.scoring, present, rank)
    if run_task is not None:
        problems += fewer_runs(read, run_task)
    return problems


async def _task_revealed(ctx: Context, task: TaskId) -> bool:
    """Whether the task has closed for every row of its contest, so what
    `after_close` lists is shown; a contest or task that does not read has
    not.
    """
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return False
    reveal = await timelines.reveal(ctx, contest_id_of(task_scope(task)), contest, name)
    return reveal is not None and reveal <= ctx.now


async def _served(
    ctx: Context, task: TaskId, publications: Sequence[Publication]
) -> builtins.set[str]:
    """The publications whose files entrants may have been served: each
    that was the task's own (`published.live`) at some time since its
    timeline first released it, as the contest's settings read now.
    """
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    entry = contest.entry(name) if contest is not None and name is not None else None
    if contest is None or entry is None or contest.state is State.DRAFT:
        return set()
    opened = max(contest.start, contest.release_of(entry))
    if opened > ctx.now:
        return set()
    return _own_since(await entrant_view.live(ctx, task, publications), opened)


def _own_since(own: Sequence[Publication], since: datetime) -> builtins.set[str]:
    """Each of the task's own publications, `own` in order, that was its
    own at some time since `since`. A publication's time is from when it
    was made to when the next one was, which a save row passing later than
    that only shortens.
    """
    return {
        each.id
        for index, each in enumerate(own)
        if index + 1 == len(own) or own[index + 1].at > since
    }


async def _leaderboard_reads(
    ctx: Context, task: TaskId, definition: TaskDefinition
) -> list[Problem]:
    """What the contest's leaderboards ask of the task (stage 5, phase 5.7),
    against the values the save would give it: every value a column reads
    of it is kept, by its name, and no column would be refused across files
    for what the task's values now read (`leaderboard_checks.across`). Each
    problem names the leaderboard and the column, at the value it is about when
    the task still gives it, else at `scoring.values`. A leaderboard whose own
    check finds anything is the contest's to mend, and asks nothing here.
    A run task's save calls it; a task graded test by test gives a leaderboard
    no results, and is not checked (stage 5 section 6, item 103).
    """
    if definition.scoring is None:
        return []
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None or not contest.leaderboards:
        return []
    values = definition.scoring.values
    problems: list[Problem] = []
    for each in across(contest, leaderboards_of(contest), {name: Scored(values)}):
        path = f"scoring.values.{each.value}" if each.value in values else "scoring.values"
        problem = Problem(path=path, message=each.saved)
        if problem not in problems:
            problems.append(problem)
    return problems


async def _refuse_admin_only(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> None:
    as_ = organiser.identity
    keys = (
        admin_only_changes(
            "task",
            await _content(ctx, as_, task, TASK_FILE, head, {}),
            written[TASK_FILE].content,
        )
        if TASK_FILE in written
        else []
    )
    for path in ADMIN_ONLY_FILES:
        if path in written and written[path].content != await _content(
            ctx, as_, task, path, head, {}
        ):
            keys.append(path)
    if keys:
        log.info("publications.refused", task=task, keys=keys, user_id=organiser.user.id)
        scope = await names.labelled(ctx, task_scope(task))
        raise AdminOnly(admin_only_refusal(scope.name, keys), keys=keys)


def _refuse_reserved(written: Mapping[str, Edit]) -> None:
    for path in written:
        check_path(path)
    reserved = sorted(path for path in written if is_reserved(path))
    if reserved:
        raise ReservedPath(
            "Unicon writes the files in plans/ itself, so a save cannot change them. Save the "
            "task's other files.",
            paths=reserved,
        )


def _refuse_untokened_removals(changes: Mapping[str, Edit]) -> None:
    """A removal names the file as it was read, so one with no token, which
    would remove whatever is there now, is refused naming each.
    """
    untokened = sorted(
        path for path, edit in changes.items() if edit.content is None and edit.token is None
    )
    if untokened:
        raise InvalidInputs(
            "To remove a file, the save must say which version of it was read. Reload the "
            "files and try again.",
            errors=[
                {"input": path, "message": "Reload the files, then remove this one again."}
                for path in untokened
            ],
        )


def _present(head: FileSet, written: Mapping[str, WrittenEdit]) -> set[str]:
    """Every file of the state a save leaves: the head's and the save's,
    without the ones it removes.
    """
    removed = {path for path, edit in written.items() if edit.content is None}
    return {*head.tokens, *written} - removed
