"""The plan a run task's save makes (SIMPLER-CORE.md 11.1 to 11.4, 15.4 and
15.5), plan schema version 6, and what a save compares to say what changed
in it.

A run task's plan is its run workflow bound to the task's values, run by
run, so the forge never reads a workflow, a primitive's declaration or
`task.yaml` at grade time and fills in only the entrant's values. It pins
the workflow by its commit, and every file a step is given by path, size
and, for an upload, its SHA-256. It is written into the task repo as
`plans/plan.json`, beside `plans/runs.json`, in the commit a publication
names.

Each step is placed by what it reads, directly or through earlier steps
(11.1): a step reading nothing of the entrant's runs at the save, once, or
once per run when it reads a repeated input; one reading the entrant's runs
once per grading, or once per run of each grading. A run's verdict is the
outcome of its first step that was not accepted, so every run reads every
step done once per grading. A grading is two pipelines (11.3): the early one
holds the early runs and, when any run is early, every step done once per
grading; when none is, but a value shown `now` reads `compiled`, the steps
done once per grading that can give a compile error and are not tainted,
with what they read. The late one holds the rest. Steps done at the save are
a pipeline of their own. A grading made while the task's results are
released counts what `on_release` lists as shown during the contest too,
so the plan holds that split as well, where it differs (`released`), and
each grading reads the one it was made with (`RunPlan.for_split`). Each
pipeline lists its Woodpecker steps: one per step done once, one per step
of a primitive that takes a batch holding the pipeline's runs of it in run
order, and one per run of any other step, each with `depends_on` from the
wiring and the container's limits, time summed over its runs and the rest
the most of any, memory with room for the files `exec` copies
into their boxes, since the container holds what a step copies.

A value's `exec` program adds two steps no workflow names, each marked
with its value (`RunStep.value`): `build-<value>`, compiling its folder at
the save, and `exec-<value>`, run once per grading at the end of the
pipeline its value is shown from, after every step there, on the runs
file the forge builds from its group's runs (`RUNS_FILE`), at most
`RUN_ENTRY_KB` a run. Neither is any run's step.

Each pipeline runs in one slot of a grading machine (`Slot`), and the
steps Woodpecker runs side by side share its memory. So a pipeline is laid
out in stages, each step waiting for every step of the stage before it,
for the smallest slot that holds its largest step: a stage holds steps in
order while their memory adds up within the slot, and a step that runs no
boxes, whose container may use the whole slot, is a stage alone
(`_chained`). The pipeline's `size` label is the smallest slot that holds
the most its steps can use at once (`slot_of`).

A task's checks (SIMPLER-CORE.md 9.6) are graded in the save's pipeline:
each check copies every step graded per submission (`RunStep.check`),
named `c<check>-<step>`, given the check's files and values where the
entrant's would be, done for every run, once the save's own steps and the
check before it have finished, so the save's pipeline holds the whole
grading of each check (`_check_copies`). No grading reads a copy, and a
file only a copy is given is not one the grading depends on
(`RunPlan.task_paths`).

A step is tainted (11.4) when it runs the entrant's code and gets, directly
or through earlier steps, a file entrants are not shown during the contest
on a port not marked secret: a task file `show` does not list under `now`
or `on_release`, a run's own files among them, or a file of the workflow. A
step reading a tainted step is tainted too, and scalars never taint. The
save refuses a value or group shown during the contest that reads runs
behind a tainted step done once per grading, and notes one that reads runs
whose own steps are tainted (15.4).
"""

from forge.domain.run_plan.changes import pin_moved, run_plan_changes, steps_run_again
from forge.domain.run_plan.models import (
    FileFact,
    FilesNamed,
    FilesRead,
    Item,
    PinnedWorkflow,
    PipelineStep,
    ReleasedSplit,
    RunEntry,
    RunPlan,
    RunPlanCheck,
    RunStep,
    ValueStep,
)
from forge.domain.run_plan.plan import plan_runs
from forge.domain.run_plan.plan_checks import files_named, order_problems, side_by_side, slot_of
from forge.domain.run_plan.records import (
    BOXED,
    COMPILE_ERROR,
    COMPILE_USE,
    EXEC_USE,
    PIPELINES,
    PRIMITIVE_ROOM_MB,
    PROGRAM_USES,
    RELEASE_MOVED,
    RUN_ENTRY_KB,
    SCHEMA_VERSION,
    GradingKind,
    PipelineKind,
    StepClass,
)

__all__ = [
    "BOXED",
    "COMPILE_ERROR",
    "COMPILE_USE",
    "EXEC_USE",
    "PIPELINES",
    "PRIMITIVE_ROOM_MB",
    "PROGRAM_USES",
    "RELEASE_MOVED",
    "RUN_ENTRY_KB",
    "SCHEMA_VERSION",
    "FileFact",
    "FilesNamed",
    "FilesRead",
    "GradingKind",
    "Item",
    "PinnedWorkflow",
    "PipelineKind",
    "PipelineStep",
    "ReleasedSplit",
    "RunEntry",
    "RunPlan",
    "RunPlanCheck",
    "RunStep",
    "StepClass",
    "ValueStep",
    "files_named",
    "order_problems",
    "pin_moved",
    "plan_runs",
    "run_plan_changes",
    "side_by_side",
    "slot_of",
    "steps_run_again",
]
