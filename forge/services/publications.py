"""The save of a task, which is also how it is published. There is no publish
button: every save of a task, from a form or from the file editor, runs these
steps in order, and refuses before anything is written.

1. A save that writes inside `plans/`, where only the compiler writes, is
   refused with `ReservedPath`. A manager's save that changes an admin-only
   key of `task.yaml` or touches `statement.md` is refused with `AdminOnly`,
   naming each.
2. The state being saved, the files at the head with the save's files over
   them, is checked (TASK-FORMAT.md section 2): `task.yaml` validates; the
   workflow it names is read at its version as the organiser saving, with
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
   `due`; and every board covering the task gets what it asks of it, beside
   the other covered tasks as their latest publications stand (check 10,
   T8, `boards.check_task`). A workflow is named `<owner>/<name>`, the owner an org by its name or a
   person by their username; when the latest publication used a workflow of
   that name and it is now another workflow, by the forge's own id for it,
   the state is refused at that line, since the owner may have been renamed
   and the name taken by someone else. No org holds a secret yet, so a
   value given as `{secret: <name>}` is refused naming it. A state that
   fails is kept as a draft: the organiser's files are written as one
   change, nothing is published, and the last publication keeps grading.
   The errors are not stored; `check` works them out again whenever the
   task's state is asked for.
3. What the save changes about how the task grades is worked out against
   the latest publication: its plan and the digests of the task's files the
   plan names. Groups, rule weights, `show`, `credit` and `submissions` are
   scoring, read on every read, and change nothing that grades. A save asked
   to be kept as a draft is written as one here, saying what it held back,
   and publishes nothing, whatever else holds. Once the contest has started,
   and until it is archived, a save that changes how the task grades is
   refused with `ConfirmationRequired`, listing the changes, unless the
   caller confirms it.
4. The organiser's files and `plans/plan.json` are written as one change, as
   the organiser, so the history is theirs and a publication never catches a
   task half saved.
5. That change is named as the next publication, as the platform, with a
   note saying whether it changed how the task grades and what, which
   workflow each workflow name was, what the task's sealed steps hold back
   until its reveal, and what each value it reports means. The save reports
   each group's most points, the task's reveal, the boards it joins or
   moves, those whose scope a `show` change moves, and each board ranking
   `points` that counts nothing from it (T9). When another
   save landed between the check and the write, the change holds files this
   save never checked, so it is kept as a draft instead, saying so, and the
   next save checks and publishes the task as it then stands.
6. A publication that changed how the task grades regrades every
   submission to the task: a new attempt of each one's latest attempt
   against it, as a rejudge makes (`gradings.regrade`).
7. A task's first publication starts making its place to submit for every
   approved contestant, once the save has committed and without the save
   waiting for it (`places`).
"""

import builtins
import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta

from sqlalchemy import select

from forge.db.tables import Grading
from forge.domain.board_checks import covered, group_max
from forge.domain.boards import in_scope
from forge.domain.content import (
    ConflictToken,
    Edit,
    FileSet,
    Uploaded,
    WrittenEdit,
    check_path,
    has_path,
)
from forge.domain.definitions import (
    ADMIN_ONLY_FILES,
    CONTEST_FILE,
    DEFAULT_WORTH,
    TASK_FILE,
    ContestDefinition,
    Group,
    Leaderboard,
    Over,
    Show,
    TaskDefinition,
    admin_only_changes,
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
from forge.domain.grading import PLATFORM_MACHINE, GradingStatus
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import PublicationId, TaskId, VersionId
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
from forge.domain.publications import Publication, write_note
from forge.domain.release import has_started, reveal_of
from forge.domain.roles import (
    Role,
    contest_id_of,
    holds,
    task_scope,
)
from forge.domain.scoring import ZERO, exact, written
from forge.domain.uploads import (
    ATTRIBUTES_FILE,
    UploadStatus,
    pointer_text,
    read_pointer,
    refuse_pointer,
)
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    WorkflowDefinition,
    WorkflowRef,
    parse_workflow,
    parse_workflow_ref,
)
from forge.domain.yaml_models import InvalidDefinition, Problem, load_mapping
from forge.log import get_logger
from forge.port.uploads import TaskPlace
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    boards,
    gradings,
    names,
    places,
    primitives,
    timelines,
    uploads,
    workflows,
)
from forge.services.access import Organiser, require

log = get_logger(__name__)

SAVE_MESSAGE = "Save"
CHANGED_CONTENT = "new:"
LANDED_UNDER = (
    "Another save landed while this one was written, so the task as it now stands "
    "was not checked; save again to publish it."
)
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
    does not name, and how many submissions it queued to be graded again.
    """

    publication: PublicationId
    number: int
    grading_changed: bool
    changes: tuple[str, ...]
    notes: tuple[str, ...] = ()
    regraded: int = 0


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
    plan when it is valid, and every problem when it is not, with which
    workflow each workflow name it uses is, by the forge's own id for it.
    """

    definition: TaskDefinition | None
    compiled: Compiled | None
    errors: tuple[Problem, ...]
    workflows: Mapping[str, str] = field(default_factory=dict)


@action
async def save(
    ctx: Context,
    organiser: Organiser,
    task: TaskId,
    changes: Mapping[str, Edit],
    *,
    confirm: bool = False,
    keep_as_draft: bool = False,
    message: str | None = None,
) -> Published | Draft:
    """Save the task's files, each path with its new content and the token
    it was read with, and publish the result when it is valid. Needs the
    manager role at the task. `keep_as_draft` writes the files and publishes
    nothing, on any save; otherwise `confirm` publishes a change to how the
    task grades while its contest runs. A token that has moved is
    `Conflict`, with nothing written.
    """
    scope = task_scope(task)
    require(organiser, scope, Role.MANAGER)
    _refuse_reserved(changes)
    as_ = organiser.identity
    written = await _resolve(ctx, organiser, task, changes)
    head = await ctx.forge.content.list_files(as_, task)
    if not holds(organiser.grants, scope, Role.ADMIN):
        await _refuse_admin_only(ctx, organiser, task, head, written)
    said = message or SAVE_MESSAGE

    await timelines.hold_rules(ctx, contest_id_of(scope))
    checked = await check(ctx, as_, task, head, written)
    if checked.errors:
        version = await _write(ctx, as_, task, head, written, {}, said)
        return _draft(organiser, task, version, checked.errors, ())

    publications = await ctx.forge.workspaces.list_publications(task)
    latest = publications[-1] if publications else None
    changed: tuple[str, ...] = ()
    if latest is not None:
        before, published_files = await _published_snapshot(ctx, as_, task, latest)
        after = await _saved_snapshot(ctx, as_, task, head, written, checked, published_files)
        changed = grading_changes(before, after)
    if keep_as_draft:
        version = await _write(ctx, as_, task, head, written, {}, said)
        return _draft(organiser, task, version, (), changed)
    if changed and not confirm and await _contest_running(ctx, task):
        raise ConfirmationRequired(
            "The contest is running and this save changes how the task grades. Send it "
            "again confirmed to publish it, or keep it as a draft.",
            changes=[*changed],
        )

    plans = await _plan_files(ctx, as_, task, head, checked)
    version = await _write(ctx, as_, task, head, written, plans, said)
    touched = {*written, *plans}
    if version != head.version and await _landed_under(ctx, as_, task, head, version, touched):
        return _draft(organiser, task, version, (Problem(path="", message=LANDED_UNDER),), ())
    assert checked.compiled is not None and checked.definition is not None
    notes = checked.compiled.notes
    if latest is not None and version == latest.version:
        return Published(latest.id, latest.number, latest.grading_changed, latest.changes, notes)
    notes = (
        *notes,
        *await _report(ctx, task, checked.definition, checked.compiled, latest, bool(changed)),
    )
    note = write_note(
        bool(changed),
        changed,
        checked.workflows,
        checked.compiled.held,
        checked.compiled.measures,
    )
    publication = await ctx.forge.workspaces.publish(task, version, note)
    number = await _number_of(ctx, task, publication)
    regraded = (await gradings.regrade(ctx, task, publication)).queued if changed else 0
    if latest is None:
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
    return Published(publication, number, bool(changed), changed, notes, regraded)


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
        return Checked(None, None, (Problem(path="", message="The task has no task.yaml."),))
    try:
        definition = parse_task(text)
    except InvalidDefinition as invalid:
        return Checked(None, None, tuple(invalid.errors))
    present = {*head.tokens, *written}
    workflow, pins, problems = await _workflow(ctx, as_, definition.workflow)
    problems.extend(await _moved_workflows(ctx, task, definition, pins))
    if workflow is None or problems:
        return Checked(definition, None, tuple(problems), pins)
    primitives, unresolved = await _primitives(ctx, as_, definition, workflow)
    if unresolved:
        return Checked(definition, None, unresolved, pins)
    at = f"In {definition.workflow}, "
    version = [
        Problem(path="workflow", message=f"{at}{problem['path']}: {problem['message']}")
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
    refused = await _boards(ctx, task, definition, compiled)
    if refused:
        return Checked(definition, None, tuple(refused), pins)
    return Checked(definition, compiled, (), pins)


async def _boards(
    ctx: Context, task: TaskId, definition: TaskDefinition, compiled: Compiled
) -> builtins.list[Problem]:
    """T8: what each board covering the task asks of it as saved."""
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return []
    shape = covered(compiled.measures, definition.test_groups)
    return await boards.check_task(ctx, contest_id_of(task_scope(task)), contest, name, shape)


async def _report(
    ctx: Context,
    task: TaskId,
    definition: TaskDefinition,
    compiled: Compiled,
    latest: Publication | None,
    changed: bool,
) -> builtins.list[str]:
    """T9: each group's most points, the task's reveal, which boards the
    save moves and whose scope a `show` change moves, and each board
    ranking `points` that counts nothing from the task.
    """
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return []
    entry = contest.entry(name)
    found: builtins.list[str] = []
    if entry is not None and definition.gives_points:
        worth = exact(entry.worth if entry.worth is not None else DEFAULT_WORTH) or ZERO
        most = group_max(definition.test_groups, worth)
        found.append(
            "Each group's most points: "
            + ", ".join(f"{group} {written(points)}" for group, points in most.items())
            + "."
        )
    if entry is not None:
        extensions = await timelines.every_extension(ctx, contest_id_of(task_scope(task)))
        found.append(f"{name} reveals at {reveal_of(contest, entry, extensions).isoformat()}.")
    before = await _definition_of(ctx, task, latest) if latest is not None else None
    for board in contest.leaderboards:
        if board.tasks is not None and not board.covers(name):
            continue
        if before is None:
            found.append(f"{name} joins {board.name}.")
            continue
        if (
            changed
            or before.test_groups != definition.test_groups
            or before.credit != definition.credit
        ):
            found.append(f"This save moves {board.name}.")
        if board.over is not Over.ALL and _scope_moved(board, before, definition):
            found.append(f"A show change moves what {board.name} counts of {name}.")
    found += boards.task_notes(contest, name, covered(compiled.measures, definition.test_groups))
    return found


def _scope_moved(board: Leaderboard, before: TaskDefinition, after: TaskDefinition) -> bool:
    """Whether a group of the task moved into or out of the board's scope."""
    scope = in_scope(board.over)
    return any(
        name in before.test_groups and scope(before.test_groups[name].shown) != scope(group.shown)
        for name, group in after.test_groups.items()
    )


async def _graded_under(ctx: Context, task: TaskId) -> set[str]:
    """The publications a graded submission of the task ran under: once
    there is one, T7 and T10 hold.
    """
    return set(
        (
            await ctx.db.scalars(
                select(Grading.publication_id)
                .where(Grading.task_id == task, Grading.status == GradingStatus.DONE)
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
                    message=f"{name} is new and states no show: say always to show it at "
                    "once, or verdict or after_close.",
                )
            )
            continue
        then = most_open.get(name)
        if then is not None and OPENNESS[group.shown] < OPENNESS[then]:
            problems.append(
                Problem(
                    path=f"{at}.show",
                    message=f"{name} was shown {then}: what was shown cannot be hidden again.",
                )
            )
    return problems


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
    from then on.
    """
    if definition.gives_points:
        return []
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return []
    problems: builtins.list[Problem] = []
    for index, entry in enumerate(contest.tasks):
        if entry.id != name:
            continue
        said = []
        if entry.worth is not None:
            said.append(f"tasks[{index}].worth gives {name} worth {spelled(entry.worth)}")
        if entry.due is not None:
            said.append(f"tasks[{index}].due gives {name} a due")
        problems.extend(
            Problem(
                path="test_groups",
                message=f"No group has a rule weight, so {name} gives no points, and "
                f"contest.yaml {line}; remove it first.",
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
        raise AdminOnly(f"Only an admin of {scope.name} may change {', '.join(keys)}.", keys=keys)


def _refuse_reserved(written: Mapping[str, Edit]) -> None:
    for path in written:
        check_path(path)
    reserved = sorted(path for path in written if is_reserved(path))
    if reserved:
        raise ReservedPath(
            "Only the compiler writes inside plans/; save the task's other files.",
            paths=reserved,
        )


async def _content(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    path: str,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
) -> bytes | None:
    """A file's content in the state: the save's when it writes the file,
    otherwise the head's, or none when neither has it.
    """
    if path in written:
        return written[path].content
    if path not in head.tokens:
        return None
    return (await ctx.forge.content.read_file(as_, task, path, at=head.version)).content


async def _workflow(
    ctx: Context, as_: Identity, ref: WorkflowRef
) -> tuple[WorkflowDefinition | None, dict[str, str], builtins.list[Problem]]:
    """The workflow `ref` names, read at its version as `as_`, which
    workflow its name is, by the forge's own id for it, and a problem when
    it cannot be read or is not a workflow in the current format.
    """
    workflow = await names.workflow_id(ctx, ref)
    try:
        file = await ctx.forge.workflows.read_workflow_file(
            as_, workflow, ref.version, WORKFLOW_FILE
        )
        pins = {f"{ref.owner}/{ref.name}": await ctx.forge.workflows.workflow_key(workflow)}
    except NotFound, Forbidden:
        message = (
            f"The workflow {ref} cannot be read: it is not there at that version, "
            "or it is not shared with you."
        )
        return None, {}, [Problem(path="workflow", message=message)]
    try:
        return parse_workflow(file.content), pins, []
    except InvalidDefinition as invalid:
        message = (
            f"The workflow {ref} is not in the current format, so its owner tags a new "
            f"version: {invalid.detail}"
        )
        return None, pins, [Problem(path="workflow", message=message)]


async def _moved_workflows(
    ctx: Context, task: TaskId, definition: TaskDefinition, pins: Mapping[str, str]
) -> builtins.list[Problem]:
    """A problem at each workflow name the latest publication used for
    another workflow than the one it names now.
    """
    publications = await ctx.forge.workspaces.list_publications(task)
    pinned = publications[-1].workflows if publications else {}
    ref = definition.workflow
    name = f"{ref.owner}/{ref.name}"
    if name in pinned and name in pins and pins[name] != pinned[name]:
        message = (
            f"{name} now names a different workflow than the one this task was "
            "published with, as happens when its owner is renamed and someone else "
            "takes the name. Name the workflow you mean by its owner's name now."
        )
        return [Problem(path="workflow", message=message)]
    return []


async def _primitives(
    ctx: Context,
    as_: Identity,
    definition: TaskDefinition,
    workflow: WorkflowDefinition,
) -> tuple[dict[str, PrimitiveDeclaration], tuple[Problem, ...]]:
    """The declaration of every primitive a step of the task's workflow
    uses, read at its version as `as_`, and a problem at the `workflow` line
    for each `use:` that cannot be read, is not a primitive, or does not
    validate.
    """
    found: dict[str, PrimitiveDeclaration] = {}
    problems: builtins.list[Problem] = []
    tried: set[str] = set()
    for index, step in enumerate(workflow.steps):
        use = str(step.use)
        if use in tried:
            continue
        tried.add(use)
        where = f"In {definition.workflow}, steps[{index}].use: "
        declaration, problem = await primitives.declaration(ctx, as_, step.use)
        if problem is not None:
            problems.append(Problem(path="workflow", message=where + problem))
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
        try:
            named = Plan.from_bytes(plans[PLAN_PATH]).task_paths()
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
    otherwise by a digest of its content that no token equals.
    """
    assert checked.compiled is not None
    plan = checked.compiled.plan
    data: dict[str, str] = {}
    for named in plan.task_paths():
        for path, token in _under(head, named).items():
            if path not in written:
                data[path] = str(token)
        for path, edit in written.items():
            if has_path((path,), named):
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
    """The plan the save writes, when it is new or differs from the head's,
    and none for every other file under `plans/` at the head, which the save
    removes.
    """
    assert checked.compiled is not None
    compiled = {PLAN_PATH: checked.compiled.plan.to_bytes()}
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
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    plans: Mapping[str, bytes | None],
    message: str,
) -> VersionId:
    """Write the organiser's files and the plans as one change as `as_`, and
    return its version; with nothing to write, the head is the version.
    """
    files: dict[str, bytes | None] = {path: edit.content for path, edit in written.items()}
    files.update(plans)
    if not files:
        return head.version
    expected: dict[str, ConflictToken | None] = {path: head.tokens.get(path) for path in plans}
    expected.update({path: edit.token for path, edit in written.items()})
    return await ctx.forge.content.save_files(as_, task, files, expected=expected, message=message)


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
    it is given once per test, and whether a task may leave it out. A
    workflow gives no defaults: a contestant input's `default` is the
    task's, in `task.yaml`.
    """

    id: str
    type: str
    contestant: bool
    options: tuple[str, ...] | None
    per_test: bool
    optional: bool


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
    task keeps until someone saves it naming another (Task 10.6.5).
    """

    workflow: str | None
    inputs: tuple[DeclaredInput, ...] = ()
    test: tuple[DeclaredField, ...] = ()
    problem: str | None = None
    graded: bool = False
    newer: str | None = None


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
        return WorkflowForm(None, problem="The task has no task.yaml.")
    try:
        named = load_mapping(TASK_FILE, found.content).get("workflow")
    except InvalidDefinition as invalid:
        return WorkflowForm(
            None, problem=f"task.yaml does not read: {invalid.errors[0]['message']}"
        )
    if not isinstance(named, str):
        return WorkflowForm(None, problem="task.yaml names no workflow.")
    try:
        ref = parse_workflow_ref(named.strip())
    except InvalidName as invalid:
        return WorkflowForm(named, problem=invalid.detail)
    newer = await workflows.newer_version(ctx, organiser.identity, ref)
    workflow, _, problems = await _workflow(ctx, organiser.identity, ref)
    if workflow is None:
        return WorkflowForm(
            str(ref), problem=problems[0]["message"] if problems else None, newer=newer
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
    changed how the task grades. Needs the observer role at the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    return await ctx.forge.workspaces.list_publications(task)


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
    a rollback or a file saved back unchanged writes.

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
        if isinstance(edit.content, Uploaded):
            row = rows.get(edit.content.upload)
            if row is None or row.status not in (UploadStatus.WAITING, UploadStatus.VERIFIED):
                raise InvalidInputs(
                    "That upload is not one of yours for this task to write.",
                    errors=[{"input": path, "message": "No such upload."}],
                )
            if row.repo_path != path:
                raise InvalidInputs(
                    f"That upload was asked for {row.repo_path}, not {path}.",
                    errors=[{"input": path, "message": "The upload is for another path."}],
                )
            if not await uploads.holds(ctx, TaskPlace(task), organiser.identity, row):
                raise UploadNotReady(
                    "The file has not arrived yet; it cannot be saved until it has.",
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
