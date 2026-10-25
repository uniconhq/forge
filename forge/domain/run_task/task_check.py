from collections.abc import Collection, Iterable, Mapping

from forge.domain.blocks.catalogue import EXEC
from forge.domain.blocks.evaluate import blocks_in
from forge.domain.checks import check_checks
from forge.domain.definitions import ENTRANT, TaskDefinition
from forge.domain.grading import Machine
from forge.domain.program_manifest import PROGRAM_FILE
from forge.domain.run_task.binder import _Binder, manifest_inputs
from forge.domain.run_task.models import Run, RunsFile, RunTask, RunTaskCheck
from forge.domain.run_task.show_rules import Shown, read_later
from forge.domain.run_workflow import RunWorkflowCheck
from forge.domain.workflow_definition import RunWorkflow
from forge.domain.yaml_models import Problem, path_text


def fewer_runs(
    before: Iterable[tuple[Shown, Mapping[str, frozenset[str]]]], task: RunTask
) -> list[Problem]:
    """A problem for each group or value shown during the contest under an
    earlier publication, as `before` holds each with the runs its runs file
    says each name read, that `task` now has read fewer of the runs it
    still has (SIMPLER-CORE.md 16). Shown during the contest is under
    `now`, or under `on_release` where `before` still holds it.
    """
    present = {run.id for run in task.runs}
    read: dict[str, set[str]] = {}
    for shown, reads in before:
        for name, time in shown.names.items():
            if time in ("now", "on_release") and name in reads:
                read.setdefault(name, set()).update(reads[name] & present)
    problems: list[Problem] = []
    for name, then in sorted(read.items()):
        now = task.runs_of(name)
        if now is None:
            continue
        missing = then - now
        if missing:
            where = "groups" if name in task.groups else "values"
            problems.append(
                Problem(
                    path=path_text(("scoring", where, name)),
                    message=f"Entrants have been shown {name} during the contest over "
                    f"{len(then)} {'run' if len(then) == 1 else 'runs'} the task still has, "
                    f"and it would now read {len(missing)} fewer. What they have been shown "
                    "cannot be hidden again, so keep those runs in it.",
                )
            )
    return problems


def manifest_files(definition: TaskDefinition, checked: RunWorkflowCheck) -> set[str]:
    """The task files the task fills a manifest input with, and the
    `program.yaml` of each `exec` block's program, which the save reads as
    `program.yaml` files before it checks the task.
    """
    wanted = manifest_inputs(checked)
    found: set[str] = set()
    for tree in definition.scoring.values.values():
        for node in blocks_in(tree, EXEC):
            folder = node.get("program")
            if isinstance(folder, str) and folder.endswith("/"):
                found.add(f"{folder}{PROGRAM_FILE}")
    for name in wanted:
        given = definition.inputs.get(name)
        if isinstance(given, str) and given != ENTRANT:
            found.add(given)
    for axis in definition.repeat:
        for index, name in enumerate(axis.inputs):
            if name in wanted:
                for row in axis.rows:
                    value = row[index] if index < len(row) else None
                    if isinstance(value, str) and value != ENTRANT:
                        found.add(value)
    return found


def check_run_task(
    definition: TaskDefinition,
    workflow: RunWorkflow,
    checked: RunWorkflowCheck,
    paths: Collection[str],
    manifests: Mapping[str, bytes],
    *,
    previous: RunsFile | None,
    submitted: bool,
    secrets: Collection[str],
    machine: Machine,
) -> RunTaskCheck:
    """Check the task `definition` against its workflow, as
    `check_run_workflow` resolved it at its version, `paths` the task's
    files and `manifests` the content of each task file a manifest input is
    filled with (`manifest_files`) that is there. `previous` is the latest
    publication's runs file, `submitted` whether anyone has submitted to
    the task, and `secrets` the org's.
    """
    binder = _Binder(definition, workflow, checked, paths, secrets, machine)
    binder.bind()
    runs = binder.make_runs(previous)
    groups = binder.groups(runs)
    values = binder.scoring(runs, manifests)
    binder.show(manifests)
    for at, message in read_later(definition.scoring, values):
        binder.refuse(at, message)
    programs = binder.programs(groups, manifests, values)
    binder.limits(runs)
    binder.manifests(runs, manifests)
    binder.manifest_types(manifests)
    marks = binder.self_reported()
    if previous is not None and submitted:
        binder.entrant_kept(previous)
    refused, checks = check_checks(
        definition.checks,
        binder.entrant,
        paths,
        definition.scoring.values,
        values.kinds,
    )
    binder.problems += refused
    if binder.problems:
        return RunTaskCheck(binder.problems, None)
    task = RunTask(
        tuple(runs),
        groups,
        values,
        definition.scoring.show,
        binder.entrant,
        binder.settings,
        marks,
        binder.ids,
        binder.manifest_outputs(manifests),
        programs,
        checks,
    )
    early = task.early()
    task = RunTask(
        tuple(Run(run.id, run.key, run.id in early, run.titles) for run in runs),
        task.groups,
        task.values,
        task.show,
        task.entrant,
        task.settings,
        task.self_reported,
        task.ids,
        task.manifest_outputs,
        task.programs,
        task.checks,
    )
    return RunTaskCheck([], task)
