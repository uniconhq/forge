import uuid
from collections.abc import Mapping

from forge.domain.content import Edit
from forge.domain.definitions import PUBLIC_FOLDER, STATEMENT_FILE
from forge.domain.ids import TaskId
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.plans import grading_changes
from forge.domain.publication_record import Check, write_note
from forge.domain.regrades import RegradeCause
from forge.domain.roles import Role, contest_id_of, holds, task_scope
from forge.domain.run_plan import RELEASE_MOVED, pin_moved, run_plan_changes, steps_run_again
from forge.domain.run_task import file_time, self_reported_question
from forge.domain.save_effect import ReadChanges
from forge.domain.yaml_models import Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrant_view, entries, gradings, pipelines, places, timelines
from forge.services.access import Organiser, require
from forge.services.task_save import sources
from forge.services.task_save.checking import _ask, _checked_first, check
from forge.services.task_save.effect import _differing, _programs_again, _report, _save_effect
from forge.services.task_save.guards import (
    _refuse_admin_only,
    _refuse_reserved,
    _refuse_untokened_removals,
)
from forge.services.task_save.listing import _resolve
from forge.services.task_save.records import (
    LANDED_UNDER,
    PIN_ANSWER,
    SAVE_MESSAGE,
    Checked,
    Draft,
    Published,
)
from forge.services.task_save.sources import _published_snapshot, _saved_snapshot
from forge.services.task_save.stored_facts import _marks_digest
from forge.services.task_save.writing import _draft, _landed_under, _plan_files, _write

log = get_logger(__name__)


@action
async def save(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    changes: Mapping[str, Edit],
    *,
    confirm: bool = False,
    asked: str | None = None,
    keep_as_draft: bool = False,
    message: str | None = None,
) -> Published | Draft:
    """Save the task's files, each path with its new content, or none to
    remove it, and the token it was read with, and publish the result when
    it is valid. Needs the manager role at the task. `keep_as_draft` writes
    the files and publishes nothing, on any save; otherwise `confirm`
    publishes anything new while its contest runs (#449). `asked` is the
    question's own `asked`: a confirmation of a question asked about another
    head or another latest publication than now is asked again, so nobody
    publishes a change the question did not describe. A token that has
    moved is `Conflict`, with nothing written.
    """
    scope = task_scope(task)
    require(organiser, scope, Role.MANAGER)
    _refuse_reserved(changes)
    _refuse_untokened_removals(changes)
    as_ = organiser.identity
    written = await _resolve(ctx, organiser, task, changes)
    head = await ctx.forge.content.list_files(as_, task)
    if not holds(organiser.grants, scope, Role.ADMIN):
        await _refuse_admin_only(ctx, organiser, task, head, written)
    said = message or SAVE_MESSAGE

    await timelines.hold_rules(ctx, contest_id_of(scope))
    await timelines.hold_task(ctx, task)
    checked = await check(ctx, as_, task, head, written)
    if checked.errors:
        version = await _write(ctx, organiser, task, head, written, {}, said)
        return _draft(organiser, task, version, checked.errors, ())

    # A run task's publication still checked, or refused, is not the task's,
    # so a save says what it changes against the one entrants are graded on.
    await pipelines.take_over(ctx, task)
    made = await ctx.forge.workspaces.list_publications(task)
    live = await entrant_view.live(ctx, task, made)
    latest = live[-1] if live else None
    newest = made[-1] if made else None
    changed: tuple[str, ...] = ()
    rerun: tuple[str, ...] | None = None
    pinned = False
    read = ReadChanges()
    anew = True
    worded = False
    if latest is not None:
        before, published_files = await _published_snapshot(ctx, as_, task, latest)
        differ = await _differing(ctx, as_, task, head, written, published_files)
        if checked.run_task is not None:
            # What a run task's plan changes is said from its plan and runs
            # file, which say whether anything moved.
            after = await _saved_snapshot(ctx, as_, task, head, written, checked, published_files)
            changed = run_plan_changes(before, after)
            pinned = pin_moved(before, after)
            if RELEASE_MOVED in changed and not await entrant_view.results_released(ctx, task):
                # The released split moved while no grading takes it: one
                # made at a release takes it as it then stands, so nothing
                # grades again for it (stage 5 section 6 item 44).
                changed = tuple(each for each in changed if each != RELEASE_MOVED)
            rerun = steps_run_again(before, after)
            scoring = checked.definition.scoring if checked.definition is not None else None
            read = ReadChanges(
                statement=STATEMENT_FILE in differ,
                public=scoring is not None
                and any(file_time(scoring, path) is not None for path in differ),
            )
            anew = bool(differ) or any(
                before.plans.get(path) != content for path, content in checked.plan_files().items()
            )
        else:
            after = await _saved_snapshot(ctx, as_, task, head, written, checked, published_files)
            changed = grading_changes(before, after)
            # A workflow moved in place can change a value's words alone,
            # which only a new publication's note carries.
            worded = checked.compiled is not None and latest.meanings != checked.compiled.meanings
            read = ReadChanges(
                statement=STATEMENT_FILE in differ,
                public=any(path.startswith(PUBLIC_FOLDER) for path in differ),
                words=worded,
            )
            anew = bool(differ) or before.plans.get(PLAN_PATH) != after.plans[PLAN_PATH] or worded
    if keep_as_draft:
        version = await _write(ctx, organiser, task, head, written, {}, said)
        return _draft(organiser, task, version, (), changed)
    assert checked.definition is not None
    basis = f"{head.version}:{latest.id if latest is not None else ''}"
    # What the organiser confirmed is the latest publication's, taken over
    # or not.
    question = (
        self_reported_question(
            checked.run_task,
            await sources._runs_file_of(ctx, task, newest) if newest is not None else None,
        )
        if checked.run_task is not None
        else None
    )
    # A self-reported column, or a pin moved, is confirmed only by an
    # answer to the question that named it: a bare confirm, or one to
    # another question, is no answer (stage 5 section 6 item 68).
    expected = basis if question is None else f"{basis}:{_marks_digest(checked.run_task)}"
    if pinned:
        expected = f"{expected}:{PIN_ANSWER}"
    exact = question is not None or pinned
    confirmed = confirm and (asked == expected if exact else asked in (None, basis))
    if not confirmed:
        await _ask(
            ctx,
            task,
            checked,
            latest,
            question,
            changed,
            rerun,
            read,
            expected,
            anew=anew,
            pinned=pinned,
        )

    plans = await _plan_files(ctx, as_, task, head, checked)
    version = await _write(ctx, organiser, task, head, written, plans, said)
    touched = {*written, *plans}
    if version != head.version and await _landed_under(ctx, as_, task, head, version, touched):
        return _draft(organiser, task, version, (Problem(path="", message=LANDED_UNDER),), ())
    notes = (
        checked.compiled.notes
        if checked.compiled is not None
        else checked.run_plan.notes
        if checked.run_plan is not None
        else ()
    )
    if newest is not None and version == newest.version and not worded:
        # A save that published a change to how the task grades and stopped
        # before its regrade left the publication live and its submissions
        # graded as before: a save of the same files grades them again.
        # A task checked first is graded again when its check passes.
        regraded = 0
        started: uuid.UUID | None = None
        if (
            newest.grading_changed
            and not _checked_first(checked)
            and await gradings.behind(ctx, task, newest.id)
        ):
            regrading = await gradings.regrade(
                ctx,
                task,
                newest.id,
                cause=RegradeCause.SAVE,
                by=organiser.user.id,
                limit=checked.definition.submissions.max,
            )
            regraded, started = regrading.queued, regrading.regrade
        effect = await _save_effect(
            ctx, task, checked.definition, latest, (), regraded, ReadChanges()
        )
        again = (
            await pipelines.check_again(ctx, task, newest.id, checked.run_plan, organiser.user.id)
            if checked.run_plan is not None
            else None
        )
        return Published(
            newest.id,
            newest.number,
            newest.grading_changed,
            newest.changes,
            notes,
            regraded,
            effect,
            again,
            started,
        )
    notes = (*notes, *await _report(ctx, task, checked.definition, checked))
    note = (
        write_note(
            bool(changed),
            changed,
            checked.workflows,
            checked.compiled.held,
            checked.compiled.measures,
            checked.compiled.titles,
            checked.compiled.meanings,
        )
        if checked.compiled is not None
        else write_note(
            bool(changed), changed, checked.workflows, checked_first=_checked_first(checked)
        )
    )
    # A publication checked first is held back by its note until a save row
    # passes, so a save that fails once it is made leaves it unchecked, never
    # the task's (`published.live`).
    if latest is not None:
        # A value given an `entered` block the live version lacks starts with
        # no marks, before any mark can be entered under the new version
        # (stage 5 section 6 item 57).
        prior = entries.entered_values(await sources._definition_of(ctx, task, latest))
        added = entries.entered_values(checked.definition) - prior
        if added:
            await entries.start_again(ctx, task, added, organiser.user.id)
    publication = await ctx.forge.workspaces.publish(task, version, note)
    number = await sources._number_of(ctx, task, publication)
    checking: Check | None = None
    if checked.run_plan is not None:
        saving = await pipelines.queue_save(
            ctx, task, publication, checked.run_plan, organiser.user.id
        )
        checking = "checking" if saving is not None else None
    regraded = 0
    started = None
    if _regrades(changed, checked):
        regrading = await gradings.regrade(
            ctx,
            task,
            publication,
            cause=RegradeCause.SAVE,
            by=organiser.user.id,
            limit=checked.definition.submissions.max,
        )
        regraded, started = regrading.queued, regrading.regrade
    if changed and _checked_first(checked):
        # Graded again once the save row passes: counted as the take-over
        # will choose them.
        counting = await gradings.to_regrade(ctx, task, limit=checked.definition.submissions.max)
    else:
        counting = regraded
    effect = await _save_effect(
        ctx,
        task,
        checked.definition,
        latest,
        changed,
        counting,
        read,
        steps=rerun,
        checked_first=_checked_first(checked),
        programs=_programs_again(checked, rerun),
    )
    if newest is None:
        places.ahead_at(ctx, task)
    log.info(
        "publications.published",
        task=task,
        publication=publication,
        number=number,
        version=version,
        grading_changed=bool(changed),
        regraded=regraded,
        user_id=organiser.user.id,
    )
    return Published(
        publication, number, bool(changed), changed, notes, regraded, effect, checking, started
    )


def _regrades(changed: tuple[str, ...], checked: Checked) -> bool:
    """Whether the save grades the task's submissions again now: when what
    it grades changed, the store running again only what changed by key for
    a run task (SIMPLER-CORE.md 11.6); but not for a run task whose plan
    holds steps done at the save, which grades them again once its save row
    lets it take over (`pipelines`).
    """
    return bool(changed) and not _checked_first(checked)
