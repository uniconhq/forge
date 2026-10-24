"""A run task's gradings as Woodpecker pipelines (SIMPLER-CORE.md 11.3,
11.7, 11.8, phase 4.9).

A run task's grading is a row per pipeline (`forge.services.gradings`). Its
`early` row runs the steps of the runs shown during the contest, and once it
ends `done` its `late` row runs the rest (`gradings.follow`); a plan with
nothing early has an early row with nothing to run, which ends `done` at
once, so the late row is made at once, and a plan with nothing late makes
no late row. A publication whose plan holds steps done at the save is
checked by a `save` row of its own before it takes over (`queue_save`):
until that row ends `done` with every run accepted and every check of the
task given what it expects, the task's publication is the one before
(`published.task`), so entrants keep being graded on it.

Each row runs as a manual pipeline on its org's dispatch repository
(`gradings.start`). `prepare` readies a row's start: only the steps the
store leaves to run (`steps.pipeline_to_run`), each with its door and token
minted, and the report step; and a user for the row, from `USER_FIRST` to
`USER_LAST`, unique among the rows not finished, a row past what its state
may take settled to free its user, which every step but `exec`
runs as, since it makes isolate boxes of its own (phase
4.1). A step left to run whose image the platform has withdrawn
(`Settings.withdrawn_images`) ends the row's start in `system_error` saying
so, and `lookup` answers no pipeline that holds one. A pipeline left with
nothing is not started: its row is concluded at once from what the store
holds, and a late row whose early compile was not accepted so takes that
step's verdict for every run with no pipeline run.

`lookup` answers the CI that asks what a pipeline is as it starts one, from
the row and its minted steps alone, writing nothing: each step's image by
digest, its door and token as settings, the org's cache for a step that
takes it, its user, and what it waits for; then the report step, the step
wrapper's base image in its report mode, which runs whatever the steps
before it ended with. The pipeline's labels are the platform's pool, the
machine shape's when it holds an `exec` step, since a label
picks a machine for a whole pipeline, the size of the smallest slot that
holds what its steps left to run can use at once (`run_plan.slot_of`).
Every slot takes any pipeline, early, late or save, first come, first
served. A pipeline no slot holds, as after the slots were made smaller
than when its task was saved, is not answered.

`concluded` follows a row's report: a `system_error` is tried again once by
itself as any row is (`gradings.notice`); an early row that ends `done`
makes its late row, started in the background, so the report waits for no
start; a late row that ends once its task has revealed is told to its
entrant, whose result reads it then; and a save row that ends `done` with
every run accepted lets its publication take over (`published.live`).
Grading the task's submissions again, when that publication changed how the
task grades, runs in the background too (`take_over`); until it has, the
row's `taken_over_at` is empty, and the next save of the task or
organiser's read of it finishes it, so a take-over whose work failed or
never ran is not lost.

`checks` reads a task's save rows for its organisers: where each
publication not yet taken over stands (`Check`), and `failed` each check
that refused one (`FailedCheck`, SIMPLER-CORE.md 9.6); the lost check and the
retry by itself run over each publication's latest save row as over any
row read, and a take-over still waiting is finished in the background. A
save of the same files as a publication whose checking was refused, or
that is `unchecked` since the save that made it failed after
publishing, checks it again (`check_again`). Nothing refuses that save.
"""

import uuid
from collections.abc import Collection, Sequence
from typing import cast

from sqlalchemy import exists, func, select, text
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Grading, GradingStep
from forge.db.tables.gradings import EARLY, LATE, PASSED, SAVE
from forge.domain.errors import CiRequestRefused, Misconfigured, NotFound, PortError, Unavailable
from forge.domain.grading import (
    BOXED_SHAPE,
    PLATFORM_POOL,
    SIZE_LABEL,
    UNFINISHED,
    GradingStatus,
    PipelineRun,
    PipelineSpec,
    PipelineStepSpec,
)
from forge.domain.ids import OrgId, PublicationId, SubmissionId, TaskId, VersionId
from forge.domain.publication_record import Check, FailedCheck, Publication, failed_checks
from forge.domain.regrades import RegradeCause
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.run_plan import BOXED, PipelineKind, RunPlan, slot_of
from forge.domain.step_data import REPORT_STEP, all_accepted, without
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrant_view, gradings, names, places, steps, timelines

log = get_logger(__name__)

USER_FIRST = 10_000
USER_LAST = 19_999
"""The users a grading's steps run as, below the box users (20000 to
64999) and above any a machine gives a person."""
USER_TRIES = 32
NO_USER = "No user is free for a grading's steps. Try again in a moment."
REFUSED = "The platform does not answer this request."
NO_IMAGE = "The platform names no image for a run task's {name} step."
REPORT_MODE = "report"


WITHDRAWN = (
    "A step of this task runs {image}, an image the platform has withdrawn. Its "
    "gradings run again once the task is saved with a version that has a fixed image."
)


class NoImage(Misconfigured):
    """The platform names no image a run task's pipeline needs."""


class ImageWithdrawn(Misconfigured):
    """A step left to run names an image the platform has withdrawn."""


async def prepare(ctx: Context, row: Grading) -> PipelineRun | None:
    """The pipeline to start for a `queued` run task's row, its steps
    minted and a user taken for it, or none once the row is concluded at
    once since nothing is left to run. `Misconfigured` for a row whose plan
    does not read, `NotFound` when its publication is gone, and
    `Unavailable` when no user is free, `NoImage` when the platform names
    no image for the report or the clone, and `ImageWithdrawn` when a step
    left to run names an image the platform has withdrawn. A late row
    reuses, or runs everything again, as its early row's steps were minted
    to.
    """
    pipeline = _kind(row)
    scratch = row.early_id is not None and await steps.from_scratch_of(ctx, row.early_id)
    left = await steps.pipeline_to_run(ctx, row, pipeline, from_scratch=scratch)
    if not left:
        held = await gradings.find(ctx, row.id, lock=True)
        if held is not None and held.status == GradingStatus.QUEUED:
            await steps.conclude(ctx, held, pipeline)
            await concluded(ctx, held)
            log.info("pipelines.nothing_to_run", grading=str(row.id), pipeline=pipeline)
        return None
    images = {"report": ctx.settings.primitive_base_image, "clone": ctx.settings.plugin_git_image}
    for name, image in images.items():
        if image is None:
            raise NoImage(NO_IMAGE.format(name=name))
    plan, _ = await steps.plan_of(ctx, row)
    for held_step in left:
        image = plan.steps[held_step.step].image
        if ctx.settings.withdraws(image):
            raise ImageWithdrawn(WITHDRAWN.format(image=image))
    for held_step in left:
        await steps.mint(ctx, row, pipeline=pipeline, step=held_step.name, from_scratch=scratch)
    await steps.mint(ctx, row, pipeline=pipeline, step=REPORT_STEP, from_scratch=scratch)
    await _take_user(ctx, row)
    return await _run(ctx, row)


async def lookup(ctx: Context, grading: str | None, org: OrgId) -> tuple[PipelineRun, PipelineSpec]:
    """The platform's half of what decides a pipeline: the pipeline and its
    steps, for a run task's row of `org` whose pipeline is being started.
    `CiRequestRefused` for any other, and `Unavailable` when the forge does
    not answer.
    """
    row = await _named(ctx, grading)
    if (
        row is None
        or gradings.org_of(row) != org
        or row.status != GradingStatus.QUEUED
        or row.run_user is None
    ):
        log.warning("pipelines.config_refused", reason="not_starting", grading=grading)
        raise CiRequestRefused(REFUSED)
    pipeline = _kind(row)
    try:
        plan, _ = await steps.plan_of(ctx, row)
        run = await _run(ctx, row)
    except (Misconfigured, NotFound) as exc:
        log.warning("pipelines.config_refused", reason="plan", grading=grading, error=str(exc))
        raise CiRequestRefused(REFUSED) from None
    except PortError as exc:
        log.warning("pipelines.forge_unavailable", grading=grading, error=type(exc).__name__)
        raise Unavailable("The forge did not answer.") from None
    minted = set(
        await ctx.db.scalars(select(GradingStep.step).where(GradingStep.grading_id == row.id))
    )
    held = plan.pipeline(pipeline)
    left = without(held, [each.name for each in held if each.name not in minted])
    if REPORT_STEP not in minted or not left:
        log.warning("pipelines.config_refused", reason="unminted", grading=grading)
        raise CiRequestRefused(REFUSED)
    found: list[PipelineStepSpec] = []
    boxed = False
    for each in left:
        step = plan.steps[each.step]
        if ctx.settings.withdraws(step.image):
            log.warning("pipelines.config_refused", reason="withdrawn", grading=grading)
            raise CiRequestRefused(REFUSED)
        in_boxes = step.primitive.partition("@")[0] in BOXED
        boxed = boxed or in_boxes
        found.append(
            PipelineStepSpec(
                name=plan.woodpecker_name(each.name),
                image=step.image,
                settings=_settings(
                    steps.access(ctx, row, each.name, plan.woodpecker_name(each.name))
                ),
                depends_on=tuple(plan.woodpecker_name(name) for name in each.depends_on),
                cache=step.cache,
                user=None if in_boxes else row.run_user,
            )
        )
    report = ctx.settings.primitive_base_image
    clone = ctx.settings.plugin_git_image
    if report is None or clone is None:
        log.warning("pipelines.config_refused", reason="images", grading=grading)
        raise CiRequestRefused(REFUSED)
    found.append(
        PipelineStepSpec(
            name=REPORT_STEP,
            image=report,
            settings={
                "mode": REPORT_MODE,
                **_settings(steps.access(ctx, row, REPORT_STEP, REPORT_STEP)),
            },
            depends_on=tuple(plan.woodpecker_name(each.name) for each in left),
            user=row.run_user,
            always=True,
        )
    )
    slot = slot_of(left, ctx.settings.slot_sizes)
    if slot is None:
        log.warning("pipelines.config_refused", reason="slot", grading=grading)
        raise CiRequestRefused(REFUSED)
    labels = (
        PLATFORM_POOL,
        *((BOXED_SHAPE,) if boxed else ()),
        f"{SIZE_LABEL}:{slot.name}",
    )
    # A step reads the submission from the checkout only through an entrant
    # input, so a pipeline of compares alone clones nothing.
    clones = any(plan.files_read(each).entrant for each in left)
    return run, PipelineSpec(clone_image=clone, steps=tuple(found), labels=labels, clones=clones)


async def concluded(ctx: Context, row: Grading) -> None:
    """What follows a row that has just been concluded, in the unit of work
    that concluded it: its retry by itself for a `system_error`, the late
    row for an early row `done`, and the publication taking over for a
    save row `done` with every run accepted; and for a late row once its
    task has revealed, or while its results are released for a grading
    that took the unreleased split, a nudge to its entrant, whose result
    reads it then.
    """
    status = GradingStatus(row.status)
    task = TaskId(row.task_id)
    if row.pipeline == LATE and (
        await _revealed(ctx, task)
        or (not row.released_split and await entrant_view.results_released(ctx, task))
    ):
        gradings.told_late(ctx, row)
    if status is GradingStatus.SYSTEM_ERROR:
        await gradings.notice(ctx, [row])
        return
    if status is not GradingStatus.DONE:
        return
    if row.pipeline == EARLY:
        plan, _ = await steps.plan_of(ctx, row)
        # A late pipeline may hold a value's program alone, its runs early.
        if plan.pipeline("late"):
            made = await gradings.follow(ctx, row)
            log.info(
                "pipelines.late_made",
                grading=str(row.id),
                late=str(made.id) if made is not None else None,
            )
    elif row.pipeline == SAVE and accepted(row):
        _take_over_later(ctx, TaskId(row.task_id))


async def _revealed(ctx: Context, task: TaskId) -> bool:
    """Whether the task has closed for every row of its contest, read as
    an entrant's result reads it; a contest or task that does not read has
    not."""
    contest = contest_id_of(task_scope(task))
    name = (await names.names_of(ctx, [task])).get(task)
    if name is None:
        return False
    try:
        settings = await entrant_view.contest(ctx, contest)
    except PortError as exc:
        log.warning("pipelines.reveal_unread", task=task, error=type(exc).__name__)
        return False
    reveal = await timelines.reveal(ctx, contest, settings, name)
    return reveal is not None and ctx.now >= reveal


def accepted(row: Grading) -> bool:
    """Whether a save row ended `done` with every run accepted, so its
    publication takes over."""
    return row.status == GradingStatus.DONE and all_accepted(row.result)


async def queue_save(
    ctx: Context, task: TaskId, publication: PublicationId, plan: RunPlan, by: int | None
) -> Grading | None:
    """The save row that checks a run task's new `publication` before it
    takes over, none when its plan holds no step done at the save. Every
    save row of the task still unfinished is cancelled first, since a later
    save replaces what it checked. Called under the task's hold.
    """
    checking = (
        await ctx.db.scalars(
            select(Grading)
            .where(
                Grading.task_id == task,
                Grading.pipeline == SAVE,
                Grading.status.in_([status.value for status in UNFINISHED]),
            )
            .with_for_update()
        )
    ).all()
    for row in checking:
        gradings.stop(ctx, row)
    if not plan.pipeline("save"):
        return None
    attempts = await ctx.db.scalar(
        select(func.max(Grading.attempt)).where(
            Grading.pipeline == SAVE, Grading.publication_id == publication
        )
    )
    return gradings.new_row(
        ctx,
        task=task,
        workspace=None,
        submission=None,
        number=None,
        version=None,
        submitted_at=None,
        publication=publication,
        attempt=(attempts or 0) + 1,
        key=None,
        submitted_by=by,
        pipeline=SAVE,
    )


async def checks(ctx: Context, task: TaskId, made: Sequence[Publication]) -> dict[str, Check]:
    """Where each of the task's publications, `made`, that has not taken
    over stands, by its id, for its organisers, from its latest save row:
    the lost check asked of each, and its retry by itself started as any row
    read; a `system_error` still `checking` while that retry may come; and
    `unchecked` for one checked first that has no row, its save having failed
    after it was made. A take-over still waiting is finished in the
    background.
    """
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.task_id == task, Grading.pipeline == SAVE)
                .order_by(Grading.publication_id, Grading.attempt.desc())
                .distinct(Grading.publication_id)
            )
        )
        .scalars()
        .all()
    )
    gone = await gradings.lost(ctx, rows)
    await gradings.notice(ctx, rows, gone)
    if any(accepted(row) and row.taken_over_at is None for row in rows):
        _take_over_later(ctx, task)
    found: dict[str, Check] = {each.id: "unchecked" for each in made if each.checked_first}
    for row in rows:
        check = _check_of(ctx, row, gone)
        if check is None:
            found.pop(row.publication_id, None)
        else:
            found[row.publication_id] = check
    return found


async def failed(
    ctx: Context, task: TaskId, refused: Collection[str]
) -> dict[str, tuple[FailedCheck, ...]]:
    """Each check that refused each publication of the task in `refused`,
    by the publication's id, from its latest save row, for its organisers;
    none for one whose row graded no check."""
    if not refused:
        return {}
    rows = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(
                    Grading.task_id == task,
                    Grading.pipeline == SAVE,
                    Grading.publication_id.in_(sorted(refused)),
                )
                .order_by(Grading.publication_id, Grading.attempt.desc())
                .distinct(Grading.publication_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.publication_id: failed_checks(row.result) for row in rows}


async def check_again(
    ctx: Context, task: TaskId, publication: PublicationId, plan: RunPlan, by: int | None
) -> Check | None:
    """Where a save of the same files as `publication` leaves it: checked
    again, by a new save row, when its checking was refused, replaced or
    `unchecked`, the last when its plan holds steps done at the save and
    it has no row, since the save that made it failed after publishing.
    Called under the task's hold. An `unchecked` publication refuses no
    save.
    """
    latest = await ctx.db.scalar(
        select(Grading)
        .where(Grading.pipeline == SAVE, Grading.publication_id == publication)
        .order_by(Grading.attempt.desc())
        .limit(1)
    )
    if latest is None:
        check: Check | None = "unchecked" if plan.pipeline("save") else None
    else:
        check = _check_of(ctx, latest)
    if check in ("refused", "replaced", "unchecked"):
        await queue_save(ctx, task, publication, plan, by)
        log.info("pipelines.checked_again", task=task, publication=publication)
        return "checking"
    return check


async def checking(ctx: Context, publication: PublicationId) -> bool:
    """Whether a save row still checks `publication` before it takes over,
    as `checks` tells its organisers: its latest row not finished, or a
    system error its retry by itself may still follow. One refused,
    replaced or with no row never takes over by itself."""
    latest = await ctx.db.scalar(
        select(Grading)
        .where(Grading.pipeline == SAVE, Grading.publication_id == publication)
        .order_by(Grading.attempt.desc())
        .limit(1)
    )
    return latest is not None and _check_of(ctx, latest) == "checking"


def _check_of(ctx: Context, row: Grading, lost: Collection[uuid.UUID] = ()) -> Check | None:
    status = gradings.status_of(ctx, row, lost)
    if status in UNFINISHED:
        return "checking"
    if accepted(row):
        return None
    if status is GradingStatus.CANCELLED:
        return "replaced"
    if status is GradingStatus.SYSTEM_ERROR and not row.automatic and ctx.settings.retry_failed:
        return "checking"
    return "refused"


async def take_over(ctx: Context, task: TaskId) -> None:
    """Finish each take-over of the task still waiting: a save row that
    passed and is not marked taken over. The task is graded again against
    its publication when that is still the task's and changed how it
    grades, then the row is marked, in one unit of work under the task's
    hold, so a take-over is done once. The task's first take-over starts
    making its places ahead, as a first publication that needs no check
    does when it is saved (`places.ahead_at`).
    """
    await timelines.hold_task(ctx, task)
    waiting = (
        (
            await ctx.db.execute(
                select(Grading)
                .where(
                    Grading.task_id == task,
                    Grading.pipeline == SAVE,
                    Grading.taken_over_at.is_(None),
                    PASSED,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not waiting:
        return
    first = not await ctx.db.scalar(
        select(
            exists().where(
                Grading.task_id == task,
                Grading.pipeline == SAVE,
                Grading.taken_over_at.is_not(None),
            )
        )
    )
    current = await entrant_view.task(ctx, task)
    for row in waiting:
        row.taken_over_at = ctx.now
        if current is None or current.publication.id != row.publication_id:
            continue
        log.info("pipelines.took_over", task=task, publication=row.publication_id)
        if first:
            places.ahead_at(ctx, task)
        if current.publication.grading_changed:
            await gradings.regrade(
                ctx,
                task,
                current.publication.id,
                cause=RegradeCause.SAVE,
                by=row.submitted_by,
                limit=current.definition.submissions.max,
            )


def _take_over_later(ctx: Context, task: TaskId) -> None:
    async def finish(later: Context) -> None:
        await take_over(later, task)

    ctx.in_background(finish)


async def _take_user(ctx: Context, row: Grading) -> None:
    """Give the row a user no other row not finished holds, the next of a
    counter kept in Postgres, wrapping round the range, a held one skipped;
    a row given one by a start that stopped partway keeps it. A user held
    by a row past what its state may take is freed and taken
    (`gradings.free_user`), so rows nobody reads again do not use the
    range up.
    """
    if row.run_user is not None:
        return
    span = USER_LAST - USER_FIRST + 1
    for _ in range(USER_TRIES):
        number = await ctx.db.scalar(text("SELECT nextval('grading_users')"))
        user = USER_FIRST + int(number) % span
        if await _given(ctx, row, user):
            return
        if await gradings.free_user(ctx, user) and await _given(ctx, row, user):
            return
    raise Unavailable(NO_USER)


async def _given(ctx: Context, row: Grading, user: int) -> bool:
    """Whether the row could take `user`, which no other row not finished
    holds."""
    try:
        async with ctx.db.begin_nested():
            row.run_user = user
            await ctx.db.flush()
        return True
    except IntegrityError:
        row.run_user = None
        return False


async def _run(ctx: Context, row: Grading) -> PipelineRun:
    publication = await gradings.publication_of(ctx, row)
    return PipelineRun(
        grading=row.id,
        pipeline=row.pipeline,
        org=gradings.org_of(row),
        publication_version=publication.version,
        submission=SubmissionId(row.submission_id) if row.submission_id else None,
        submission_version=VersionId(row.submission_version) if row.submission_id else None,
        compute=PLATFORM_POOL,
    )


def _settings(access: steps.StepAccess) -> dict[str, str]:
    return {"data_url": access.data_url, "data_token": access.token}


def _kind(row: Grading) -> PipelineKind:
    """The pipeline a row runs, which its check keeps one."""
    return cast(PipelineKind, row.pipeline)


async def _named(ctx: Context, grading: str | None) -> Grading | None:
    if grading is None:
        return None
    try:
        return await gradings.find(ctx, uuid.UUID(grading))
    except ValueError:
        return None
