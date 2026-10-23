"""The save of a task, which is also how it is published. There is no publish
button: every save of a task, from a form or from the file editor, runs these
steps in order, and refuses before anything is written.

1. A save that writes or removes inside `plans/`, where only the compiler
   writes, is refused with `ReservedPath`. A removal carries the token the
   file was read with, and one that does not is refused with
   `InvalidInputs`, naming it; a removal whose token has moved is
   `Conflict`, as an edit's is. A manager's save that changes an admin-only
   key of `task.yaml`, removes it, or touches `statement.md` is refused with
   `AdminOnly`, naming each.
2. The state being saved, the files at the head with the save's files over
   them and its removals taken out, is checked (TASK-FORMAT.md section 2):
   `task.yaml` validates; the workflow it names is read at its version as
   the organiser saving, with
   every primitive its steps use, and checks as a version
   (`plans.check_workflow`), a workflow in an old format refused at the
   `workflow` line so its owner tags a new version; the task's tests are
   read from `tests/<group>/<test>/` and its groups checked against them;
   the plan compiles over the files of that state, the task's values bound
   to the workflow's inputs, its sealed steps found and its fit checked; and
   a group's `show` neither hides what was shown under any publication a
   graded submission ran under nor is left unsaid on a group added once the
   task has a graded submission (T7, T10); and a task that gives no points
   is refused while its contest's entry for it gives it a `worth` or a
   `due`. What a leaderboard asks of the task is checked across files in
   stage 5's phase 5.7. A workflow is named `<owner>/<name>`, the owner an org by its name or a
   person by their username; when the latest publication used a workflow of
   that name and it is now another workflow, by the forge's own id for it,
   the state is refused at that line, since the owner may have been renamed
   and the name taken by someone else. No org holds a secret yet, so a
   value given as `{secret: <name>}` is refused naming it. A state that
   fails is kept as a draft: the organiser's files are written as one
   change, nothing is published, and the last publication keeps grading.
   The errors are not stored; `check` works them out again whenever the
   task's state is asked for.

   A task whose workflow has no test block is in the run format
   (SIMPLER-CORE.md 8 and 9), and `task.yaml`'s keys of the other format
   are refused naming it; once anyone has submitted, the task keeps the
   format it was published in. A run task's workflow is checked again at
   the commit its version names, then the task against it
   (`run_task.check_run_task`), reading each `program.yaml` the task gives
   and keeping its runs' ids from the latest publication's runs file; what
   its `show` listed under a publication a graded submission ran under is
   not listed later or dropped; and its contest gives it no `worth` or
   late share, since a leaderboard reads its values instead.
3. What the save changes about how the task grades is worked out against
   the latest publication: its plan and the digests of the task's files the
   plan names. Groups, rule weights, `show`, `credit` and `submissions` are
   scoring, read on every read, and change nothing that grades. A save asked
   to be kept as a draft is written as one here, saying what it held back,
   and publishes nothing, whatever else holds. Once the contest has started,
   and until it is archived (`release.has_started`), every save that would
   publish something new, files other than the latest publication's or
   another plan, is refused with `ConfirmationRequired` unless the caller
   confirms it (#449): a first publication too, and a change to scoring or
   the statement as much as one to how the task grades. The refusal lists
   the changes to how it grades, how many submissions it would grade again,
   counted as the regrade (6) chooses them, and what the save does
   (`domain.save_effect`): whether anyone's result changes, what runs
   again, each scoring change, old and new, with the boards counted again,
   and what entrants read that changes. A save of a run task asks before it
   publishes, whether or not the contest runs, when a column the entrant
   could set is new or set through other inputs than under the latest
   publication (#504), one question asking both.
4. The organiser's files and `plans/plan.json`, or a run task's
   `plans/runs.json`, are written as one change, by
   the platform, which alone writes a task's files at the forge (#443), with
   the organiser as its author, so the history is theirs, and a publication
   never catches a task half saved.
5. That change is named as the next publication, as the platform, with a
   note saying whether it changed how the task grades and what, which
   workflow each workflow name was, what the task's sealed steps hold back
   until its reveal, and what each value it reports means. The save answers
   with what it did, in the same words a running contest asks with (M66):
   each group's most points where they moved, and the boards reading a value
   it moves or, on a first publication, reading the task; and notes the
   task's reveal. When another
   save landed between the check and the write, the change holds files this
   save never checked, so it is kept as a draft instead, saying so, and the
   next save checks and publishes the task as it then stands.
6. A publication that changed how the task grades regrades every
   submission to the task: a new attempt of each one's latest attempt
   against it, as a rejudge makes (`gradings.regrade`), the delayed ones
   held to their entrants' room under the `submissions.max` the save
   published (#445), all kept as one regrade the save started (#448). A
   run task's publication whose plan holds steps done at the save is first
   checked by a save row of its own, which cancels any still checking an
   earlier one (`pipelines.queue_save`); it becomes the task's, and
   regrades, only once that row ends `done` with every run accepted.
7. A task's first publication starts making its place to submit for every
   approved entrant, once the save has committed and without the save
   waiting for it (`places`).
"""

import builtins
import hashlib
import json
import uuid
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import exists, select

from forge.db.tables import Grading
from forge.db.tables.gradings import FRONT
from forge.domain.blocks.catalogue import EXEC
from forge.domain.blocks.evaluate import blocks_in
from forge.domain.content import (
    ConflictToken,
    Edit,
    FileSet,
    Uploaded,
    WrittenEdit,
    check_path,
    has_path,
    named_by,
)
from forge.domain.definitions import (
    ADMIN_ONLY_FILES,
    CONTEST_FILE,
    PUBLIC_FOLDER,
    RUN_FORMAT_KEYS,
    STATEMENT_FILE,
    TASK_FILE,
    ContestDefinition,
    Group,
    Show,
    State,
    TaskDefinition,
    admin_only_changes,
    admin_only_refusal,
    parse_contest,
    parse_task,
)
from forge.domain.errors import (
    AdminOnly,
    ConfirmationRequired,
    Forbidden,
    InvalidInputs,
    InvalidName,
    NotFound,
    ReservedPath,
    UploadNotReady,
)
from forge.domain.grading import PLATFORM_MACHINE, GradingStatus, run_machine
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import PublicationId, TaskId, VersionId, WorkflowId
from forge.domain.plans import (
    PLAN_PATH,
    Compiled,
    Plan,
    Snapshot,
    check_workflow,
    compile_plan,
    grading_changes,
    group_problems,
    is_reserved,
    read_tests,
    spelled,
    test_yaml_paths,
)
from forge.domain.primitives import PrimitiveDeclaration
from forge.domain.publications import Check, Publication, write_note
from forge.domain.regrades import RegradeCause
from forge.domain.release import has_started, in_words, reveal_of
from forge.domain.roles import (
    Role,
    contest_id_of,
    holds,
    task_scope,
)
from forge.domain.run_plan import (
    PROGRAM_USES,
    RELEASE_MOVED,
    FileFact,
    RunPlan,
    files_named,
    pin_moved,
    plan_runs,
    run_plan_changes,
    steps_run_again,
)
from forge.domain.run_task import (
    RUNS_PATH,
    RunsFile,
    RunTask,
    check_run_task,
    fewer_runs,
    file_time,
    hidden_again,
    manifest_files,
    most_shown,
    openness,
    self_reported_question,
    shown_under,
    unreleased,
)
from forge.domain.run_workflow import WorkflowFiles, check_run_workflow
from forge.domain.save_effect import ReadChanges, SaveEffect, as_extra, effect_of
from forge.domain.uploads import (
    ATTRIBUTES_FILE,
    UploadStatus,
    pointer_text,
    read_pointer,
    refuse_pointer,
    upload_info,
)
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    RunWorkflow,
    WorkflowDefinition,
    WorkflowRef,
    parse_workflow_ref,
    read_workflow,
)
from forge.domain.yaml_models import InvalidDefinition, Problem, load_mapping
from forge.log import get_logger
from forge.port.uploads import TaskPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    entries,
    gradings,
    names,
    pipelines,
    places,
    primitives,
    published,
    timelines,
    uploads,
    workflows,
)
from forge.services.access import Organiser, require

log = get_logger(__name__)

SAVE_MESSAGE = "Save"
CHANGED_CONTENT = "new:"
LANDED_UNDER = (
    "Someone else saved this task while yours was being saved, so the task as it now stands "
    "was not checked. Save again to publish it."
)
PIN_ANSWER = "pin"
"""What the answer to a save's question carries when the save moves the
workflow's pin, so only an answer to that question confirms it."""
PIN_MOVED = (
    "This save moves the workflow's pin, so its submissions are graded again on the new "
    "version. Once that regrade runs, its summary lists each value and verdict it changed."
)
SHOWN_WORDS = {
    Show.ALWAYS: "as soon as a grading ends",
    Show.VERDICT: "as an outcome before the reveal",
    Show.AFTER_CLOSE: "at the reveal",
}
GROUPS_KEPT = timedelta(hours=1)
"""How long this process keeps how a publication shows each test group: a
publication never changes, so the time only bounds what is held."""
OPENNESS = {Show.AFTER_CLOSE: 0, Show.VERDICT: 1, Show.ALWAYS: 2}
"""How much of a group each `show` lets contestants see, least first."""


@dataclass(frozen=True, slots=True)
class Published:
    """A save that published: the publication and its number, whether it
    changed how the task grades and what, what the save says of the task
    beside publishing it, its sealed steps and a bounded value its `credit`
    does not name, how many submissions it queued to be graded again, and
    what it did, in the words a running contest asks with before a save;
    `check`, where a run task's publication stands while its save row
    has not let it take over (`pipelines.checks`), none otherwise; and
    `regrade`, the regrade it started, for its summary, none when it
    started none now.
    """

    publication: PublicationId
    number: int
    grading_changed: bool
    changes: tuple[str, ...]
    notes: tuple[str, ...]
    regraded: int
    effect: SaveEffect
    check: Check | None = None
    regrade: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Draft:
    """A save kept as a draft: the version its files were written as, and
    either the errors that kept it from being published, each at its YAML
    path, or, for a valid save kept as a draft, what it would have changed
    about how the task grades.
    """

    version: VersionId
    errors: tuple[Problem, ...]
    held_back: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Checked:
    """What checking a state found: the task's settings and its compiled
    plan, or for a run task its runs (`run_task`) and its plan (`run_plan`),
    when it is valid, and every problem when it is not, with which workflow
    each workflow name it uses is, by the forge's own id for it.
    """

    definition: TaskDefinition | None
    compiled: Compiled | None
    errors: tuple[Problem, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)
    run_task: RunTask | None = None
    run_plan: RunPlan | None = None

    def plan_files(self) -> dict[str, bytes]:
        """What a save of the valid state writes under `plans/`: the plan, and
        for a run task its runs file.
        """
        if self.run_task is not None:
            assert self.run_plan is not None
            return {
                RUNS_PATH: self.run_task.runs_file().to_bytes(),
                PLAN_PATH: self.run_plan.to_bytes(),
            }
        assert self.compiled is not None
        return {PLAN_PATH: self.compiled.plan.to_bytes()}


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
    live = await published.live(ctx, task, made)
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
            if RELEASE_MOVED in changed and not await published.results_released(ctx, task):
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
            await _runs_file_of(ctx, task, newest) if newest is not None else None,
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
        effect = await _effect(
            ctx, task, checked.definition, latest, (), regraded, ReadChanges(), checked.compiled
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
        prior = entries.entered_values(await _definition_of(ctx, task, latest))
        added = entries.entered_values(checked.definition) - prior
        if added:
            await entries.start_again(ctx, task, added, organiser.user.id)
    publication = await ctx.forge.workspaces.publish(task, version, note)
    number = await _number_of(ctx, task, publication)
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
    effect = await _effect(
        ctx,
        task,
        checked.definition,
        latest,
        changed,
        counting,
        read,
        checked.compiled,
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


def _checked_first(checked: Checked) -> bool:
    """Whether a save of a run task is checked by a save row before it takes
    over, and its submissions are graded again only then."""
    return checked.run_plan is not None and bool(checked.run_plan.pipeline("save"))


async def _ask(
    ctx: Context,
    task: TaskId,
    checked: Checked,
    latest: Publication | None,
    question: str | None,
    changed: tuple[str, ...],
    again: tuple[str, ...] | None,
    read: ReadChanges,
    basis: str,
    *,
    anew: bool,
    pinned: bool = False,
) -> None:
    """Refuse a save not yet confirmed with `ConfirmationRequired` when it
    asks first: while its contest runs, before it publishes anything new
    (#449); for a run task, when a column the entrant could set is new or
    set through other inputs than under the latest publication (#504); and
    when it moves the workflow's pin, `pinned`, with submissions to grade
    again (11.8). One question asks all of them, `self_reported` listing
    each such column with the entrant's inputs that let them set it, and
    `asked` being what a confirmation answers it with, `basis`.
    """
    assert checked.definition is not None
    regrades = (
        await gradings.to_regrade(ctx, task, limit=checked.definition.submissions.max)
        if changed
        else 0
    )
    asks: builtins.list[str] = []
    if anew and await _contest_running(ctx, task):
        asks.append("The contest is running, so this save asks before it publishes.")
    if pinned and regrades:
        asks.append(PIN_MOVED)
    if question is not None:
        asks.append(question)
    if not asks:
        return
    effect = await _effect(
        ctx,
        task,
        checked.definition,
        latest,
        changed,
        regrades,
        read,
        checked.compiled,
        steps=again,
        checked_first=_checked_first(checked),
        programs=_programs_again(checked, again),
    )
    marks = checked.run_task.self_reported if question and checked.run_task else {}
    raise ConfirmationRequired(
        " ".join([*asks, "Publish it, or keep it as a draft."]),
        changes=[*changed],
        regrades=regrades,
        **as_extra(effect),
        **(
            {"self_reported": {name: sorted(inputs) for name, inputs in sorted(marks.items())}}
            if marks
            else {}
        ),
        asked=basis,
    )


async def check(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, written: Mapping[str, WrittenEdit]
) -> Checked:
    """Check the state a save leaves, the files at `head` with `written` over
    them, reading what it needs as `as_`, in the order of TASK-FORMAT.md
    section 2. Every problem carries its path in `task.yaml`, or the folder
    or file it is about.
    """
    text = await _content(ctx, as_, task, TASK_FILE, head, written)
    if text is None:
        return Checked(
            None,
            None,
            (
                Problem(
                    path="", message="The task's settings are missing. Add them and save again."
                ),
            ),
        )
    try:
        definition = await _parsed(ctx, as_, text)
    except InvalidDefinition as invalid:
        return Checked(None, None, tuple(invalid.errors))
    present = _present(head, written)
    opened = await _workflow(ctx, as_, definition.workflow)
    found, pins, problems = opened.found, opened.pins, opened.problems
    problems.extend(await _moved_workflows(ctx, task, definition, pins))
    if found is None or problems:
        return Checked(definition, None, tuple(problems), pins)
    workflow = found.definition
    runs = isinstance(workflow, RunWorkflow)
    if runs != definition.runs:
        # The workflow says the format, so the keys of the other are refused
        # naming it.
        try:
            definition = parse_task(text, runs=runs)
        except InvalidDefinition as invalid:
            return Checked(None, None, tuple(invalid.errors), pins)
    kept = await _format_kept(ctx, task, definition)
    if kept:
        return Checked(definition, None, tuple(kept), pins)
    if isinstance(workflow, RunWorkflow):
        return await _check_runs(ctx, as_, task, definition, workflow, found, pins, head, written)
    primitives, unresolved = await _primitives(ctx, as_, definition, workflow)
    if unresolved:
        return Checked(definition, None, unresolved, pins)
    version = [
        _workflow_problem(definition.workflow, problem)
        for problem in check_workflow(workflow, primitives)
    ]
    if version:
        return Checked(definition, None, tuple(version), pins)
    yamls = {
        path: content
        for path in test_yaml_paths(present)
        if (content := await _content(ctx, as_, task, path, head, written)) is not None
    }
    tests, problems = read_tests(present, workflow.test, yamls)
    problems.extend(group_problems(definition, tests, present))
    problems.extend(await _shown_before(ctx, task, definition))
    problems.extend(await _contest_points(ctx, task, definition))
    if problems:
        return Checked(definition, None, tuple(problems), pins)
    try:
        compiled = compile_plan(
            definition,
            workflow,
            primitives,
            present,
            tests,
            secrets=frozenset(),
            machine=PLATFORM_MACHINE,
            harness_image=ctx.settings.harness_image,
        )
    except InvalidDefinition as invalid:
        return Checked(definition, None, tuple(invalid.errors), pins)
    return Checked(definition, compiled, (), pins)


async def _parsed(ctx: Context, as_: Identity, text: bytes) -> TaskDefinition:
    """The `task.yaml` in `text`, read in the format its keys say, or when
    that does not read, in the format of the workflow it names: a task of
    today's format with a stray `scoring:` is then refused for what its
    workflow's format refuses, not for missing `test_groups`.
    `InvalidDefinition` with the problems of the format that applies, or of
    the keys' when the workflow cannot be read.
    """
    try:
        return parse_task(text)
    except InvalidDefinition as invalid:
        guessed = invalid
    try:
        document = load_mapping(TASK_FILE, text)
        named = document.get("workflow")
        ref = parse_workflow_ref(named.strip()) if isinstance(named, str) else None
    except InvalidDefinition, InvalidName:
        raise guessed from None
    found = (await _workflow(ctx, as_, ref)).found if ref is not None else None
    if found is None:
        raise guessed
    runs = isinstance(found.definition, RunWorkflow)
    if runs == any(key in document for key in RUN_FORMAT_KEYS):
        raise guessed
    return parse_task(text, runs=runs)


async def _check_runs(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    definition: TaskDefinition,
    workflow: RunWorkflow,
    read: _Found,
    pins: dict[str, str],
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> Checked:
    """A run task's check (SIMPLER-CORE.md 8 and 9): its workflow checked
    again at the commit its `workflow.yaml` was `read` at, as making the
    version did, then
    the task against it (`check_run_task`), each manifest the task gives
    read from the saved state, the ids of its runs kept from the latest
    publication's runs file; then what was shown is not hidden again, and
    the contest's entry asks nothing a run task cannot give;
    then its plan (`plan_runs`, SIMPLER-CORE.md 11), pinned to that commit,
    with the size and hash of every file a step is given.
    """
    primitives, unresolved = await _primitives(ctx, as_, definition, workflow)
    if unresolved:
        return Checked(definition, None, unresolved, pins)
    ref = definition.workflow
    try:
        files = await workflows.files_at(ctx, as_, read.workflow, read.at, workflow)
    except NotFound, Forbidden:
        return Checked(definition, None, (_unopened(ref),), pins)
    resolved = check_run_workflow(workflow, primitives, files)
    problems = [_workflow_problem(ref, problem) for problem in resolved.problems]
    if problems:
        return Checked(definition, None, tuple(problems), pins)
    present = _present(head, written)
    manifests = {
        path: content
        for path in sorted(manifest_files(definition, resolved) & present)
        if (content := await _content(ctx, as_, task, path, head, written)) is not None
    }
    # Run ids continue from the latest publication, taken over or not, so no
    # id a publication gave is ever given to another key.
    publications = await ctx.forge.workspaces.list_publications(task)
    previous = await _runs_file_of(ctx, task, publications[-1]) if publications else None
    if previous is None and publications:
        before = await _definition_of(ctx, task, publications[-1])
        if before is not None and before.runs:
            # Numbering the runs afresh would give a key another run's id.
            return Checked(definition, None, (Problem(path="", message=RUNS_UNREAD),), pins)
    found = check_run_task(
        definition,
        workflow,
        resolved,
        present,
        manifests,
        previous=previous,
        submitted=await _submitted(ctx, task),
        secrets=frozenset(),
        machine=run_machine(ctx.settings.slot_sizes),
    )
    problems = [
        *found.problems,
        *await _shown_before_runs(ctx, task, definition, present, found.task),
        *await _contest_points(ctx, task, definition),
    ]
    if problems:
        return Checked(definition, None, tuple(problems), pins)
    assert found.task is not None, "check_run_task gives a task whenever it finds nothing wrong"
    named = files_named(workflow, found.task)
    planned = plan_runs(
        definition,
        workflow,
        resolved,
        found.task,
        ref=str(ref),
        workflow_id=str(read.workflow),
        commit=str(read.at),
        task_files=await _task_facts(ctx, as_, task, head, written, present, named.task),
        workflow_files=await _workflow_facts(ctx, as_, read, files, named.workflow),
        machine=run_machine(ctx.settings.slot_sizes),
        programs={use: primitives[use] for use in PROGRAM_USES if use in primitives},
    )
    if planned.plan is None:
        return Checked(definition, None, tuple(planned.problems), pins)
    return Checked(definition, None, (), pins, found.task, planned.plan)


async def _task_facts(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    present: set[str],
    named: Iterable[str],
) -> dict[str, FileFact]:
    """The size and hash of each task file `named` names, a folder's files
    each by itself, an upload's own: a file the save writes from what it
    writes, and the head's read in batches of at most `FACTS_BATCH` bytes,
    so the plan pins every file a step is given by its content.
    """
    paths = sorted({path for entry in named for path in named_by(present, entry)})
    facts: dict[str, FileFact] = {}
    unread: builtins.list[str] = []
    for path in paths:
        if path not in written:
            unread.append(path)
        elif (content := written[path].content) is not None:
            facts[path] = _fact_of(content)
    for batch in _batches(unread, head.sizes):
        blobs = await ctx.forge.content.read_blobs(as_, task, {head.tokens[path] for path in batch})
        for path in batch:
            content = blobs.get(head.tokens[path])
            if content is None:
                content = await _content(ctx, as_, task, path, head, written)
            if content is not None:
                facts[path] = _fact_of(content)
    return facts


FACTS_BATCH = 32 * 1024 * 1024
"""The most bytes of task files the save reads in one batch to hash them."""


def _batches(paths: Sequence[str], sizes: Mapping[str, int]) -> Iterator[builtins.list[str]]:
    """`paths` in order, in batches of at most `FACTS_BATCH` bytes by
    `sizes`, a file larger than that or of no known size in a batch alone.
    """
    batch: builtins.list[str] = []
    held = 0
    for path in paths:
        size = sizes.get(path, FACTS_BATCH)
        if batch and held + size > FACTS_BATCH:
            yield batch
            batch, held = [], 0
        batch.append(path)
        held += size
    if batch:
        yield batch


async def _workflow_facts(
    ctx: Context, as_: Identity, read: _Found, files: WorkflowFiles, named: Iterable[str]
) -> dict[str, FileFact]:
    """The size and hash of each workflow file `named` names at the commit
    the workflow was read at, as `_task_facts` gives a task file's, each
    read but the manifests the check already holds.
    """
    facts: dict[str, FileFact] = {}
    for path in sorted({path for entry in named for path in named_by(files.paths, entry)}):
        content = files.manifests.get(path)
        if content is None:
            found = await ctx.forge.workflows.read_workflow_at(as_, read.workflow, read.at, path)
            content = found.content
        facts[path] = _fact_of(content)
    return facts


def _fact_of(content: bytes) -> FileFact:
    """A file's size and SHA-256: an upload's own, from its pointer, and
    any other file's of its content.
    """
    info = upload_info(content)
    if info is not None:
        return FileFact(size=info.size, sha256=info.digest)
    return FileFact(size=len(content), sha256=hashlib.sha256(content).hexdigest())


RUNS_UNREAD = (
    f"The latest publication's {RUNS_PATH}, which keeps the id of each of the task's runs, is "
    "missing or does not read, so this save cannot keep those ids. Ask Unicon's operators to "
    "restore it."
)


def _marks_digest(task: RunTask | None) -> str:
    """A short digest of the columns `task` marks self-reported and the
    inputs that mark each, and of the values reading them and those `show`
    lists, which a confirmation of them carries back.
    """
    marks = task.self_reported if task is not None else {}
    values = sorted(task.self_reported_values()) if task is not None else []
    shown = sorted(task.self_reported_shown()) if task is not None else []
    text = json.dumps(
        [{name: sorted(inputs) for name, inputs in sorted(marks.items())}, values, shown]
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _workflow_problem(ref: WorkflowRef, problem: Problem) -> Problem:
    return Problem(
        path="workflow",
        message=f"The workflow {ref} has a problem ({problem['path']}): {problem['message']} "
        "Choose another version of it, or ask its owner to fix it.",
    )


def _unopened(ref: WorkflowRef) -> Problem:
    return Problem(
        path="workflow",
        message=f"Unicon cannot open the workflow {ref}: there is no such version, or it is not "
        "shared with you. Check its name and version, or ask its owner to share it.",
    )


async def _runs_file_of(ctx: Context, task: TaskId, publication: Publication) -> RunsFile | None:
    """The publication's runs file, or none when it has none that reads."""
    try:
        found = await ctx.forge.content.read_file(PLATFORM, task, RUNS_PATH, at=publication.version)
    except NotFound:
        return None
    return _runs_file(found.content)


def _runs_file(content: bytes | None) -> RunsFile | None:
    if content is None:
        return None
    try:
        return RunsFile.from_bytes(content)
    except ValueError:
        return None


async def _submitted(ctx: Context, task: TaskId) -> bool:
    """Whether anyone has submitted to the task."""
    return bool(await ctx.db.scalar(select(exists().where(Grading.task_id == task, FRONT))))


async def _format_kept(
    ctx: Context, task: TaskId, definition: TaskDefinition
) -> builtins.list[Problem]:
    """Once anyone has submitted, the task keeps the format of the
    publication it is graded on: a submission to one is laid out as the
    other cannot read.
    """
    publications = await published.live(ctx, task)
    if not publications:
        return []
    before = await _definition_of(ctx, task, publications[-1])
    if before is None or before.runs == definition.runs or not await _submitted(ctx, task):
        return []
    was = "scored by its runs" if before.runs else "graded test by test"
    return [
        Problem(
            path="workflow",
            message=f"Entrants have submitted to this task, which is {was}, so its workflow "
            "must keep that format. Choose a version that does.",
        )
    ]


async def _report(
    ctx: Context, task: TaskId, definition: TaskDefinition, checked: Checked
) -> builtins.list[str]:
    """The note beside what the save's effect says: the task's reveal in words
    when it hides anything (a group not shown at once, or a sealed step).
    Each group's most points, and the boards the save moves, are the
    effect's.
    """
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return []
    entry = contest.entry(name)
    found: builtins.list[str] = []
    compiled = checked.compiled
    if checked.run_task is not None:
        hides = bool(checked.run_task.show.after_close)
    else:
        assert compiled is not None
        hides = bool(compiled.sealed) or any(
            group.shown is not Show.ALWAYS for group in definition.test_groups.values()
        )
    if entry is not None and hides:
        extensions = await timelines.every_extension(ctx, contest_id_of(task_scope(task)))
        found.append(
            f"{name} shows its hidden results on {in_words(reveal_of(contest, entry, extensions))}."
        )
    return found


async def _effect(
    ctx: Context,
    task: TaskId,
    definition: TaskDefinition,
    latest: Publication | None,
    changed: tuple[str, ...],
    regrades: int,
    read: ReadChanges,
    compiled: Compiled | None,
    *,
    steps: tuple[str, ...] | None = None,
    checked_first: bool = False,
    programs: tuple[str, ...] = (),
) -> SaveEffect:
    """What saving `definition` over the latest publication does, in the
    words a running contest asks with before a save and a save that
    published answers with, each reported value named by its title in the
    save or, where the save no longer reports it, in the latest publication;
    for a run task, the steps a submission runs again, whether that waits
    for the save's check, and the values whose program runs again.
    """
    name = (await names.names_of(ctx, [task])).get(task)
    before = await _definition_of(ctx, task, latest) if latest is not None else None
    meanings = compiled.meanings if compiled is not None else ()
    titles = {
        meaning.name: meaning.title
        for meaning in (*(latest.meanings if latest is not None else ()), *meanings)
        if meaning.title is not None
    }
    return effect_of(
        name or definition.name,
        before,
        definition,
        await _contest(ctx, task),
        changes=changed,
        regrades=regrades,
        graded=bool(await _graded_under(ctx, task)),
        read=read,
        titles=titles,
        submitted=await _submitted(ctx, task),
        unread=latest is not None and before is None,
        steps=steps,
        checked_first=checked_first,
        programs=programs,
    )


def _programs_again(checked: Checked, steps: tuple[str, ...] | None) -> tuple[str, ...]:
    """The values whose `exec` program a submission runs again, each in a
    container of its own, of the steps that run again."""
    plan = checked.run_plan
    if plan is None or not steps:
        return ()
    return tuple(
        value.name
        for step in steps
        if step in plan.steps
        and (value := plan.steps[step].value) is not None
        and value.report is not None
    )


async def _differing(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    published: FileSet,
) -> set[str]:
    """Every file outside `plans/` that differs between the state a save
    leaves and the latest publication: one only in either, and one in both
    with other content. A file the save writes is compared by its content,
    any other by its token, which is the same for the same content.
    """
    state = {path for path in _present(head, written) if not is_reserved(path)}
    then = {path for path in published.tokens if not is_reserved(path)}
    differ = state ^ then
    for path in state & then:
        if path in written:
            content = written[path].content
            before = await ctx.forge.content.read_file(as_, task, path, at=published.version)
            if before.content != content:
                differ.add(path)
        elif head.tokens[path] != published.tokens[path]:
            differ.add(path)
    return differ


async def _graded_under(ctx: Context, task: TaskId) -> set[str]:
    """The publications a graded submission of the task ran under: once
    there is one, T7 and T10 hold.
    """
    return set(
        (
            await ctx.db.scalars(
                select(Grading.publication_id)
                .where(Grading.task_id == task, Grading.status == GradingStatus.DONE, FRONT)
                .distinct()
            )
        ).all()
    )


async def _shown_before(
    ctx: Context, task: TaskId, definition: TaskDefinition
) -> builtins.list[Problem]:
    """T7 and T10: once the task has a graded submission, a group shown
    `always` or `verdict` under any publication a graded submission ran
    under is not hidden again, nor `always` made `verdict`, so a group
    removed and added back cannot come back hidden; and a group the save
    adds to the latest publication's says its `show`.
    """
    graded = await _graded_under(ctx, task)
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
    problems: builtins.list[Problem] = []
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
) -> builtins.list[Problem]:
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
    counted = await _graded_under(ctx, task) | await _served(ctx, task, publications)
    if not counted:
        return []
    since = await published.released_since(ctx, task)
    shown_by_release = (
        set() if since is None else _own_since(await published.live(ctx, task, publications), since)
    )
    rank = openness(released=since is not None, revealed=await _task_revealed(ctx, task))
    shown = []
    read = []
    for publication in publications:
        if publication.id not in counted:
            continue
        found = await _definition_of(ctx, task, publication)
        if found is None or found.scoring is None:
            continue
        held = await ctx.forge.content.list_files(PLATFORM, task, at=publication.version)
        each = shown_under(found.scoring, held.tokens.keys())
        if publication.id not in shown_by_release:
            each = unreleased(each)
        shown.append(each)
        if run_task is not None:
            runs = await _runs_file_of(ctx, task, publication)
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
    return _own_since(await published.live(ctx, task, publications), opened)


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


async def _groups_of(ctx: Context, task: TaskId, publication: Publication) -> Mapping[str, Group]:
    """The test groups of the publication's `task.yaml`, or none when it
    does not read, which a save never publishes.
    """
    found = await _definition_of(ctx, task, publication)
    return found.test_groups if found is not None else {}


async def _definition_of(
    ctx: Context, task: TaskId, publication: Publication
) -> TaskDefinition | None:
    """The publication's `task.yaml`, read once per process, or none when
    it does not read.
    """

    async def read() -> TaskDefinition | None:
        try:
            found = await ctx.forge.content.read_file(
                PLATFORM, task, TASK_FILE, at=publication.version
            )
            return parse_task(found.content)
        except NotFound, InvalidDefinition:
            return None

    key = f"publications.definition.{task}.{publication.version}"
    return await ctx.memo.remembered(key, GROUPS_KEPT, read)


async def _contest_points(
    ctx: Context, task: TaskId, definition: TaskDefinition
) -> builtins.list[Problem]:
    """Check 10's last part: a task that gives no points is refused while
    its contest's entry for it gives it a `worth` or a `due`, naming the
    contest's line, since every save of the contest would be refused there
    from then on; a run task may have a `due`, which its values read as
    `seconds_late`, but not a `late_per_day`.
    """
    if definition.gives_points:
        return []
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return []
    problems: builtins.list[Problem] = []
    for entry in contest.tasks:
        if entry.id != name:
            continue
        said = []
        if entry.worth is not None:
            said.append(f"gives it a worth of {spelled(entry.worth)}")
        if entry.due is not None and not definition.runs:
            said.append("gives it a due time")
        if entry.late_per_day is not None and definition.runs:
            said.append("takes a share of its points for each late day")
        if definition.runs:
            problems.extend(
                Problem(
                    path="scoring",
                    message=f"{name} is scored by its runs and gives no points, since a "
                    f"leaderboard reads its values, but the contest {line}. Remove that from the "
                    "contest first.",
                )
                for line in said
            )
            continue
        problems.extend(
            Problem(
                path="test_groups",
                message=f"No test group gives points, so {name} gives no points, but the "
                f"contest {line}. Remove that from the contest first.",
            )
            for line in said
        )
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


async def _content(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    path: str,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> bytes | None:
    """A file's content in the state: the save's when it writes the file,
    otherwise the head's, or none when neither has it or the save removes
    it.
    """
    if path in written:
        return written[path].content
    if path not in head.tokens:
        return None
    return (await ctx.forge.content.read_file(as_, task, path, at=head.version)).content


@dataclass(frozen=True, slots=True)
class _Found:
    """A workflow as read: with or without a test block, the forge's id for
    it, and the commit its version stood at when it was read, which every
    later read of its files in the same check is pinned to.
    """

    definition: WorkflowDefinition | RunWorkflow
    workflow: WorkflowId
    at: VersionId


@dataclass(frozen=True, slots=True)
class _Opened:
    """The workflow a task names, none when it cannot be read or is in an
    old format, with the problem saying so, and which workflow its name is,
    by the forge's own id for it.
    """

    found: _Found | None
    pins: dict[str, str]
    problems: builtins.list[Problem]


async def _workflow(ctx: Context, as_: Identity, ref: WorkflowRef) -> _Opened:
    """The workflow `ref` names, its version resolved once to a commit and
    `workflow.yaml` read there as `as_`.
    """
    workflow = await names.workflow_id(ctx, ref)
    try:
        at = await ctx.forge.workflows.workflow_version(as_, workflow, ref.version)
        file = await ctx.forge.workflows.read_workflow_at(as_, workflow, at, WORKFLOW_FILE)
        pins = {f"{ref.owner}/{ref.name}": await ctx.forge.workflows.workflow_key(workflow)}
    except NotFound, Forbidden:
        return _Opened(None, {}, [_unopened(ref)])
    try:
        definition = read_workflow(file.content)
    except InvalidDefinition as invalid:
        message = (
            f"The workflow {ref} is written in an older form that Unicon no longer reads, so "
            f"its owner needs to make a new version of it. {invalid.detail}"
        )
        return _Opened(None, pins, [Problem(path="workflow", message=message)])
    return _Opened(_Found(definition, workflow, at), pins, [])


async def _moved_workflows(
    ctx: Context, task: TaskId, definition: TaskDefinition, pins: Mapping[str, str]
) -> builtins.list[Problem]:
    """A problem at each workflow name the publication the task is graded on
    used for another workflow than the one it names now.
    """
    publications = await published.live(ctx, task)
    pinned = publications[-1].workflows if publications else {}
    ref = definition.workflow
    name = f"{ref.owner}/{ref.name}"
    if name in pinned and name in pins and pins[name] != pinned[name]:
        message = (
            f"{name} is now a different workflow from the one this task was published "
            "with, as happens when its owner is renamed and someone else takes the name. "
            "Give the workflow you mean under its owner's current name."
        )
        return [Problem(path="workflow", message=message)]
    return []


async def _primitives(
    ctx: Context,
    as_: Identity,
    definition: TaskDefinition,
    workflow: WorkflowDefinition | RunWorkflow,
) -> tuple[dict[str, PrimitiveDeclaration], tuple[Problem, ...]]:
    """The declaration of every primitive a step of the task's workflow
    uses, read at its version as `as_`, and those a value's `exec` program
    is compiled and run with (`PROGRAM_USES`) when a value has one, and a
    problem at the `workflow` line for each `use:` that cannot be read, is
    not a primitive, or does not validate, or at `scoring` for one of those.
    """
    found: dict[str, PrimitiveDeclaration] = {}
    problems: builtins.list[Problem] = []
    tried: set[str] = set()
    for step in workflow.steps:
        use = str(step.use)
        if use in tried:
            continue
        tried.add(use)
        where = f"In the workflow {definition.workflow}, the step {step.id} cannot run: "
        declaration, problem = await primitives.declaration(ctx, as_, step.use)
        if problem is not None:
            problems.append(Problem(path="workflow", message=where + problem))
        elif declaration is not None:
            found[use] = declaration
    values = definition.scoring.values if definition.scoring is not None else {}
    if any(blocks_in(tree, EXEC) for tree in values.values()):
        for use in PROGRAM_USES:
            if use in found or use in tried:
                continue
            declaration, problem = await primitives.declaration(ctx, as_, parse_workflow_ref(use))
            if problem is not None:
                problems.append(
                    Problem(
                        path="scoring",
                        message=f"A value's exec program cannot run: {problem}",
                    )
                )
            elif declaration is not None:
                found[use] = declaration
    return found, tuple(problems)


async def _published_snapshot(
    ctx: Context, as_: Identity, task: TaskId, latest: Publication
) -> tuple[Snapshot, FileSet]:
    """What the latest publication's grading depends on, and its files."""
    files = await ctx.forge.content.list_files(as_, task, at=latest.version)
    plans = {
        path: (await ctx.forge.content.read_file(as_, task, path, at=latest.version)).content
        for path in sorted(files.tokens)
        if is_reserved(path)
    }
    named: tuple[str, ...] = ()
    if PLAN_PATH in plans:
        reader = RunPlan if RUNS_PATH in plans else Plan
        try:
            named = reader.from_bytes(plans[PLAN_PATH]).task_paths()
        except ValueError:
            named = ()
    data = {path: str(token) for each in named for path, token in _under(files, each).items()}
    return Snapshot(plans=plans, data=data), files


def _under(files: FileSet, path: str) -> Mapping[str, ConflictToken]:
    """The files `path` names in `files`: the one file, or every file under a
    folder.
    """
    if path.endswith("/"):
        return files.under(path)
    return {path: files.tokens[path]} if path in files.tokens else {}


async def _saved_snapshot(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    checked: Checked,
    published: FileSet,
) -> Snapshot:
    """What the saved state's grading would depend on. A data file the save
    does not write compares by its token at the head; one it writes compares
    by the token it had at the publication when its content is the same, and
    otherwise by a digest of its content that no token equals; one it
    removes is not there, so the comparison names it removed.
    """
    plan = checked.run_plan or (checked.compiled.plan if checked.compiled is not None else None)
    assert plan is not None
    data: dict[str, str] = {}
    for named in plan.task_paths():
        for path, token in _under(head, named).items():
            if path not in written:
                data[path] = str(token)
        for path, edit in written.items():
            if edit.content is not None and has_path((path,), named):
                data[path] = await _digest(ctx, as_, task, path, edit.content, published)
    return Snapshot(plans={PLAN_PATH: plan.to_bytes()}, data=data)


async def _digest(
    ctx: Context, as_: Identity, task: TaskId, path: str, content: bytes, published: FileSet
) -> str:
    if path in published.tokens:
        before = await ctx.forge.content.read_file(as_, task, path, at=published.version)
        if before.content == content:
            return str(before.token)
    return f"{CHANGED_CONTENT}{hashlib.sha256(content).hexdigest()}"


async def _contest(ctx: Context, task: TaskId) -> ContestDefinition | None:
    """The settings of the task's contest, read as the platform, since an
    organiser of the task alone may not read the contest; none when they do
    not read.
    """
    contest = contest_id_of(task_scope(task))
    try:
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
        return parse_contest(found.content)
    except NotFound, InvalidDefinition:
        return None


async def _contest_running(ctx: Context, task: TaskId) -> bool:
    """Whether the task's contest has started and is not archived."""
    contest = await _contest(ctx, task)
    return contest is not None and has_started(contest, ctx.now)


async def _plan_files(
    ctx: Context, as_: Identity, task: TaskId, head: FileSet, checked: Checked
) -> dict[str, bytes | None]:
    """The plan the save writes, or a run task's runs file, when it is new
    or differs from the head's, and none for every other file under
    `plans/` at the head, which the save removes.
    """
    compiled = checked.plan_files()
    files: dict[str, bytes | None] = {}
    for path, content in compiled.items():
        if await _content(ctx, as_, task, path, head, {}) != content:
            files[path] = content
    for path in head.tokens:
        if is_reserved(path) and path not in compiled:
            files[path] = None
    return files


async def _write(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    plans: Mapping[str, bytes | None],
    message: str,
) -> VersionId:
    """Write the organiser's files and the plans as one change, and return
    its version; with nothing to write, the head is the version. The
    platform writes it, the only account a task's files are written by at
    the forge (#443), with the organiser as its author, so the history is
    theirs; `save` has checked they may.
    """
    files: dict[str, bytes | None] = {path: edit.content for path, edit in written.items()}
    files.update(plans)
    if not files:
        return head.version
    expected: dict[str, ConflictToken | None] = {path: head.tokens.get(path) for path in plans}
    expected.update({path: edit.token for path, edit in written.items()})
    return await ctx.forge.content.save_files(
        PLATFORM, task, files, expected=expected, message=message, author=organiser.user.id
    )


async def _landed_under(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    version: VersionId,
    written: Collection[str],
) -> bool:
    """Whether the change at `version` sits on something other than `head`:
    a file this save did not write differs from the head it checked, so
    another save landed between the check and the write.
    """
    after = await ctx.forge.content.list_files(as_, task, at=version)
    return any(
        head.tokens.get(path) != after.tokens.get(path)
        for path in head.tokens.keys() | after.tokens.keys()
        if path not in written
    )


def _draft(
    organiser: Organiser,
    task: TaskId,
    version: VersionId,
    errors: tuple[Problem, ...],
    held_back: tuple[str, ...],
) -> Draft:
    log.info(
        "publications.draft",
        task=task,
        version=version,
        errors=len(errors),
        held_back=len(held_back),
        user_id=organiser.user.id,
    )
    return Draft(version, errors, held_back)


async def _number_of(ctx: Context, task: TaskId, publication: PublicationId) -> int:
    for found in await ctx.forge.workspaces.list_publications(task):
        if found.id == publication:
            return found.number
    raise NotFound(f"the publication {publication} of {task} is not listed")


@dataclass(frozen=True, slots=True)
class DeclaredInput:
    """One input a workflow declares, as the task form shows it: its id, its
    type, whether the contestant gives it, its options on an enum, whether
    it is given once per test, whether a task may leave it out, and the
    title, line of help and unit the workflow gives the organiser for it,
    each none when it gives none. A workflow gives no defaults: a
    contestant input's `default` is the task's, in `task.yaml`.
    """

    id: str
    type: str
    contestant: bool
    options: tuple[str, ...] | None
    per_test: bool
    optional: bool
    title: str | None = None
    help: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class DeclaredField:
    """One field every test of the workflow has: its name, its type and its
    options on an enum.
    """

    name: str
    type: str
    options: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class WorkflowForm:
    """What the task form is built from: the workflow the task's `task.yaml`
    names, as written, the inputs it declares and its test fields, in the
    order the workflow gives them, or `problem`, the reason there are none:
    no `task.yaml` or one that does not read as YAML, no workflow named, or
    one that cannot be read or is in an old format; `graded`, whether
    the task has a graded submission, from when on a save refuses a test
    group it adds without its `show` (T10); and `newer`, the workflow's
    latest version when it comes after the one the task names, which the
    task keeps until someone saves it naming another (Task 10.6.5); and
    `runs`, whether the workflow describes one run, so the task is in the
    run format (SIMPLER-CORE.md 8), its inputs given by the task, the
    entrant or `repeat`, and no test fields.
    """

    workflow: str | None
    inputs: tuple[DeclaredInput, ...] = ()
    test: tuple[DeclaredField, ...] = ()
    problem: str | None = None
    graded: bool = False
    newer: str | None = None
    runs: bool = False


@action
async def workflow_form(ctx: Context, organiser: Organiser, task: TaskId) -> WorkflowForm:
    """The inputs and test fields of the workflow the task's `task.yaml`
    names as it is saved now, a draft included, read at its version as the
    organiser, the way a save reads it. Needs the observer role at the task.
    A task that names no workflow, or one that cannot be read, is answered
    with the reason and nothing else, since the form is how it is mended.
    Either way it says whether the task has a graded submission.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    graded = bool(await _graded_under(ctx, task))
    return replace(await _workflow_form(ctx, organiser, task), graded=graded)


async def _workflow_form(ctx: Context, organiser: Organiser, task: TaskId) -> WorkflowForm:
    try:
        found = await ctx.forge.content.read_file(organiser.identity, task, TASK_FILE)
    except NotFound:
        return WorkflowForm(
            None, problem="The task's settings are missing. Add them and save again."
        )
    try:
        named = load_mapping(TASK_FILE, found.content).get("workflow")
    except InvalidDefinition as invalid:
        return WorkflowForm(None, problem=invalid.errors[0]["message"])
    if not isinstance(named, str):
        return WorkflowForm(None, problem="The task does not name a workflow yet. Choose one.")
    try:
        ref = parse_workflow_ref(named.strip())
    except InvalidName as invalid:
        return WorkflowForm(named, problem=invalid.detail)
    newer = await workflows.newer_version(ctx, organiser.identity, ref)
    opened = await _workflow(ctx, organiser.identity, ref)
    workflow = opened.found.definition if opened.found is not None else None
    problems = opened.problems
    if workflow is None:
        return WorkflowForm(
            str(ref), problem=problems[0]["message"] if problems else None, newer=newer
        )
    if isinstance(workflow, RunWorkflow):
        # Who gives a run workflow's input, and whether it repeats, are the
        # task's, so each reads as given once by the task until it says.
        return WorkflowForm(
            str(ref),
            newer=newer,
            runs=True,
            inputs=tuple(
                DeclaredInput(
                    id=name,
                    type=declared.type.value,
                    contestant=False,
                    options=declared.options,
                    per_test=False,
                    optional=declared.optional,
                    title=declared.title,
                    help=declared.help,
                    unit=declared.unit,
                )
                for name, declared in workflow.inputs.items()
            ),
        )
    return WorkflowForm(
        str(ref),
        newer=newer,
        inputs=tuple(
            DeclaredInput(
                id=name,
                type=declared.type.value,
                contestant=declared.contestant,
                options=declared.options,
                per_test=declared.per_test,
                optional=declared.optional,
                title=declared.title,
                help=declared.help,
                unit=declared.shown_unit,
            )
            for name, declared in workflow.inputs.items()
        ),
        test=tuple(
            DeclaredField(name=name, type=declared.type.value, options=declared.options)
            for name, declared in workflow.test.items()
        ),
    )


@action
async def list(ctx: Context, organiser: Organiser, task: TaskId) -> tuple[Publication, ...]:
    """Every publication of the task, oldest first, each saying whether it
    changed how the task grades, and a run task's that has not taken over
    where it stands (`pipelines.checks`), and one refused each check that
    failed it. Needs the observer role at the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    made = await ctx.forge.workspaces.list_publications(task)
    found = await pipelines.checks(ctx, task, made)
    failed = await pipelines.failed(
        ctx, task, [name for name, check in found.items() if check == "refused"]
    )
    return tuple(
        replace(each, check=found.get(each.id), failed_checks=failed.get(each.id, ()))
        for each in made
    )


async def _held_pointer(
    ctx: Context, organiser: Organiser, task: TaskId, path: str, content: bytes
) -> bytes | None:
    """The pointer `content` is, written afresh, when it is exactly one the
    platform writes and the task's store holds its object, asked as the
    organiser; none otherwise.
    """
    named = read_pointer(content)
    if named is None or path == ATTRIBUTES_FILE or path.endswith(f"/{ATTRIBUTES_FILE}"):
        return None
    digest, size = named
    if not await uploads.holds_object(ctx, TaskPlace(task), organiser.identity, digest, size):
        return None
    return pointer_text(digest, size)


async def _resolve(
    ctx: Context, organiser: Organiser, task: TaskId, changes: Mapping[str, Edit]
) -> Mapping[str, WrittenEdit]:
    """Every edit as bytes the write can carry: an edit naming an upload
    becomes the pointer to it, and the upload is marked as taken by this
    save, while typed content that would read as a pointer is refused,
    unless it is exactly a pointer to an object the task already holds, as
    a rollback or a file saved back unchanged writes. A removal passes as
    it is.

    A pointer names bytes by their hash alone, and a grading machine serves
    one from the org's shared store without asking the forge, so the only
    pointers in any repository are the ones written here, for objects the
    forge has confirmed belong to it.
    """
    named = {
        path: edit.content.upload
        for path, edit in changes.items()
        if isinstance(edit.content, Uploaded)
    }
    rows = await uploads.for_save(ctx, organiser.identity.user_id, task, set(named.values()))
    resolved: dict[str, WrittenEdit] = {}
    for path, edit in changes.items():
        if edit.content is None:
            resolved[path] = WrittenEdit(None, edit.token)
            continue
        if isinstance(edit.content, Uploaded):
            row = rows.get(edit.content.upload)
            if row is None or row.status not in (UploadStatus.WAITING, UploadStatus.VERIFIED):
                raise InvalidInputs(
                    "That upload is not one of yours for this task.",
                    errors=[{"input": path, "message": "There is no such upload."}],
                )
            if row.repo_path != path:
                raise InvalidInputs(
                    f"That upload was made for {row.repo_path}, not {path}. Upload the file "
                    "again here.",
                    errors=[{"input": path, "message": "This upload was made for another file."}],
                )
            if not await uploads.holds(ctx, TaskPlace(task), organiser.identity, row):
                raise UploadNotReady(
                    "The file has not finished uploading. Save again once it has.",
                    uploads=[str(row.id)],
                )
            row.status = UploadStatus.CONSUMED
            resolved[path] = WrittenEdit(pointer_text(row.digest, row.size), edit.token)
            continue
        held = await _held_pointer(ctx, organiser, task, path, edit.content)
        if held is None:
            refuse_pointer(path, edit.content)
        resolved[path] = WrittenEdit(held or edit.content, edit.token)
    if named:
        await ctx.db.flush()
    return resolved
