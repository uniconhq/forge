import math
from collections.abc import Mapping, Sequence
from typing import Any

from forge.domain import exact_json
from forge.domain.checks import CHECKS_ROOM, RUNS_KEPT, TaskCheck, check_room, copy_of
from forge.domain.content import named_by
from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE, TaskDefinition
from forge.domain.grading import WALL_CEILING, Machine, wall_of
from forge.domain.port_values import scalar_to_text
from forge.domain.primitives import PROGRAMS, PrimitiveDeclaration
from forge.domain.run_plan.models import (
    CheckStep,
    Confirmation,
    FileFact,
    PinnedWorkflow,
    PipelineStep,
    ReleasedSplit,
    RunEntry,
    RunPlan,
    RunPlanCheck,
    RunStep,
)
from forge.domain.run_plan.plan_checks import cache_problems, order_problems
from forge.domain.run_plan.plan_helpers import _key_json
from forge.domain.run_plan.planner import _last, _Planner
from forge.domain.run_plan.records import COMPILE_USE, EXEC_USE, PIPELINES
from forge.domain.run_task import RunTask
from forge.domain.run_workflow import RunWorkflowCheck
from forge.domain.types import FILES
from forge.domain.workflow_definition import RunWorkflow
from forge.domain.yaml_models import Path, Problem, path_text


def _named(steps: dict[str, RunStep]) -> dict[str, RunStep]:
    """Each step's Woodpecker name, `s<n>` by its place in the plan from 1,
    a check's copies and a value's program steps included."""
    return {
        key: step.model_copy(update={"woodpecker": f"s{place}"})
        for place, (key, step) in enumerate(steps.items(), start=1)
    }


def plan_runs(
    definition: TaskDefinition,
    workflow: RunWorkflow,
    checked: RunWorkflowCheck,
    task: RunTask,
    *,
    ref: str,
    workflow_id: str,
    commit: str,
    task_files: Mapping[str, FileFact],
    workflow_files: Mapping[str, FileFact],
    machine: Machine,
    inner_workflows: tuple[PinnedWorkflow, ...] = (),
    programs: Mapping[str, PrimitiveDeclaration] | None = None,
) -> RunPlanCheck:
    """The plan of the task `definition`, as `check_run_task` checked it
    (`task`) against its workflow resolved at `commit` (`checked`).
    `task_files` and `workflow_files` hold the facts of every file
    `files_named` names, each folder's files each by itself; `machine` is
    the largest a step may take; `programs` the declarations of
    `PROGRAM_USES`, by use, when a value has an `exec` program, which is
    compiled at the save and run once per grading in the pipeline its
    value is shown from, after its group's runs (`_Planner.programs`).
    The plan holds both splits of a grading (9.4): the one made before any
    release, and when a release moves anything, the one made while the
    task's results are released (`RunPlan.released`), each fitted to the
    machine.
    """
    planner = _Planner(
        definition,
        definition.scoring,
        workflow,
        checked,
        task,
        task_files,
        workflow_files,
        machine,
        {index: pin.files for index, pin in enumerate(inner_workflows, start=1)},
    )
    for step in workflow.steps:
        planner.bind(step.id, step.with_)
    problems = [
        *cache_problems(checked),
        *order_problems(planner.classes, planner.reads),
        *planner.shown_reports(),
    ]
    if task.programs:
        assert programs is not None, "a save reads the program primitives when a value has one"
        problems += planner.programs(programs[COMPILE_USE], programs[EXEC_USE])
    if problems:
        return RunPlanCheck(problems, None)
    pipelines = planner.pipelines()
    on_release = planner.pipelines(released=True)
    fitted = planner.fit(pipelines)
    problems = [
        *fitted,
        *(problem for problem in planner.fit(on_release) if problem not in fitted),
        *planner.refusals(),
    ]
    if problems:
        return RunPlanCheck(problems, None)
    early_released = task.early(released=True)
    released = (
        ReleasedSplit(
            early=tuple(run.id for run in task.runs if run.id in early_released),
            pipelines={kind: on_release[kind] for kind in ("early", "late") if kind in on_release},
        )
        if early_released != task.early() or on_release != pipelines
        else None
    )
    copied, save, refused = _check_copies(
        task,
        planner.run_steps(),
        pipelines.get("save", ()),
        planner.whole(),
        task_files,
        len(planner.reports()),
    )
    if refused:
        return RunPlanCheck(refused, None)
    if save:
        pipelines = {
            kind: save if kind == "save" else pipelines[kind]
            for kind in PIPELINES
            if kind == "save" or kind in pipelines
        }
    plan = RunPlan(
        workflow=PinnedWorkflow(
            ref=ref, id=workflow_id, commit=commit, files=dict(sorted(workflow_files.items()))
        ),
        inner_workflows=inner_workflows,
        task_files=dict(sorted(task_files.items())),
        entrant=planner.entrant_entries(),
        runs=tuple(
            RunEntry(id=run.id, key=_key_json(run.key), early=run.early, titles=run.titles)
            for run in task.runs
        ),
        steps=_named({**planner.run_steps(), **copied}),
        pipelines=pipelines,
        reports=planner.reports(),
        notes=(*planner.notes(), *task.release_notes()),
        confirmations=tuple(
            Confirmation(column=column, inputs=tuple(sorted(inputs)))
            for column, inputs in sorted(task.self_reported.items())
        ),
        released=released,
    )
    broken = violation(exact_json.loads(plan.to_bytes()), "plan")
    if broken is not None:
        return RunPlanCheck(
            [
                Problem(
                    path="workflow",
                    message=f"Unicon cannot grade this task with {definition.workflow}: the "
                    f"plan it makes breaks one of the runner's rules ({broken}).",
                )
            ],
            None,
        )
    return RunPlanCheck([], plan)


def _check_copies(
    task: RunTask,
    steps: Mapping[str, RunStep],
    own: Sequence[PipelineStep],
    whole: Sequence[PipelineStep],
    task_files: Mapping[str, FileFact],
    reports: int,
) -> tuple[dict[str, RunStep], tuple[PipelineStep, ...], list[Problem]]:
    """Each check's copies of the steps graded per submission, and the
    save's pipeline with them after its `own` steps, check by check, each
    check's laid out as `whole` lays a grading out; or what refuses them: a
    copy's name already taken, a check's file larger than the entrant
    may give, the pipeline then past the run ceiling, or the checks able
    to keep more of the save's result than `CHECKS_ROOM`. None when the
    task has no checks.
    """
    if not task.checks:
        return {}, (), []
    order = list(dict.fromkeys(held.step for held in whole))
    problems: list[Problem] = []
    copied: dict[str, RunStep] = {}
    save = list(own)
    for check in task.checks:
        at = ("checks", check.index - 1)
        rename = {step: copy_of(check.index, step) for step in order}
        taken = next((name for name in rename.values() if name in steps), None)
        if taken is not None:
            problems.append(
                Problem(
                    path=path_text(at),
                    message=f"Unicon names this check's copy of a step {taken}, which the "
                    "workflow's step of that name already takes. Remove the check, or rename "
                    "that step.",
                )
            )
            continue
        problems += _too_large(task, check, at, task_files)
        for step in order:
            copied[rename[step]] = _copied(steps[step], step, check, rename, task)
        before = _last(save)
        for each in whole:
            waits = tuple(copy_of(check.index, name) for name in each.depends_on)
            save.append(
                each.model_copy(
                    update={
                        "name": copy_of(check.index, each.name),
                        "step": rename[each.step],
                        "depends_on": waits or before,
                    }
                )
            )
    if problems:
        return {}, (), problems
    total = wall_of([held.limits.time_ms for held in save])
    ceiling = WALL_CEILING.total_seconds()
    if total > ceiling:
        problems.append(
            Problem(
                path="checks",
                message=f"The save's steps and its checks take {math.ceil(total / 60)} "
                f"minutes, but a pipeline may take at most {int(ceiling // 60)}. Give fewer "
                "checks.",
            )
        )
        return {}, (), problems
    room = len(task.checks) * check_room([run.key for run in task.runs], reports, len(order))
    if room > CHECKS_ROOM:
        problems.append(
            Problem(
                path="checks",
                message=f"A failed check keeps up to {RUNS_KEPT} of its runs in the save's "
                f"result, so these {len(task.checks)} checks could keep {room} bytes, more than "
                f"the {CHECKS_ROOM} they may. Give fewer checks.",
            )
        )
        return {}, (), problems
    return copied, tuple(save), []


def _too_large(
    task: RunTask, check: TaskCheck, at: Path, task_files: Mapping[str, FileFact]
) -> list[Problem]:
    """A file or folder a check gives that holds more than the entrant may
    give that input."""
    found: list[Problem] = []
    for name, given in check.given.items():
        most = task.entrant[name].form.max_size or DEFAULT_MAX_SIZE
        for path in given.paths:
            size = sum(task_files[each].size for each in named_by(task_files, path))
            if size > most:
                found.append(
                    Problem(
                        path=path_text((*at, "submit", name)),
                        message=f"{path} holds {size} bytes, more than the {most} the entrant "
                        f"may give as {name}.",
                    )
                )
    return found


def _copied(
    step: RunStep, of: str, check: TaskCheck, rename: Mapping[str, str], task: RunTask
) -> RunStep:
    """Check `check`'s copy of `step`, the step `of`: done at the save, for
    every run, given what the check gives where the entrant's inputs were,
    and reading the check's copies of the steps it reads."""

    def given(value: Any) -> Any:
        return _for_check(value, check, rename, task)

    return step.model_copy(
        update={
            "class_": "save_once" if step.once else "save_per_run",
            "check": CheckStep(index=check.index, of=of),
            "inputs": (
                {port: given(value) for port, value in step.inputs.items()}
                if step.inputs is not None
                else None
            ),
            "items": (
                {
                    run: item.model_copy(
                        update={
                            "inputs": {port: given(value) for port, value in item.inputs.items()}
                        }
                    )
                    for run, item in step.items.items()
                }
                if step.items is not None
                else None
            ),
        }
    )


def _for_check(value: Any, check: TaskCheck, rename: Mapping[str, str], task: RunTask) -> Any:
    """A bound value as a check's copy is given it: an entrant's file or
    folder by the check's task path, any other entrant value, or text
    written with one, by the check's value, and a step graded per
    submission by the check's copy of it."""
    if not isinstance(value, dict):
        return value
    if "entrant" in value:
        given = check.part(value["entrant"], value.get("part"))
        return {"task": given} if task.entrant[value["entrant"]].type in FILES else {"value": given}
    if "template" in value:
        parts = [
            scalar_to_text(check.part(part["entrant"], part.get("part"))) for part in value["parts"]
        ]
        return {"value": value["template"].format(*parts)}
    if "step" in value and value["step"] in rename:
        return {**value, "step": rename[value["step"]]}
    if "files" in value:
        return {
            "files": {
                name: _for_check(each, check, rename, task) for name, each in value["files"].items()
            }
        }
    if PROGRAMS in value:
        return {
            PROGRAMS: {
                program: {
                    port: _for_check(each, check, rename, task) for port, each in inputs.items()
                }
                for program, inputs in value[PROGRAMS].items()
            }
        }
    return value
