"""A run task checked against its run workflow (SIMPLER-CORE.md 8 and 9), as
its save checks it, and what the check learns that the planner and the
pages read after it.

The task fills each workflow input once: in `inputs`, as a setting or as
the entrant's (`code: entrant`, or `{entrant: <form details>}`), or by one
axis of `repeat`. The runs are every combination of the axes' tuples, the
first axis outermost, each keyed by its repeated values, where the entrant
fills one its tuple's position, "Part 1" (`Part`). Each run has an opaque id,
`r<n>`, kept with its key across the task's versions and a new key taking
the next number, read from the latest publication's `plans/runs.json`
(`RunsFile`), so adding tuples renumbers nothing.

Checked here (8.5, 9.5): every input filled at most once and each one not
optional exactly once; at least one the entrant's; each setting of its
input's type, a file or folder there, an enum within its options; no two
runs with one key; an input raising a limit, or a manifest the save reads,
the task's; a secret given only whole to a secret port, and only once the
org holds secrets, which none does yet; a program the entrant controls
given a manifest only where its primitive can mark its reports as the
entrant's; a judge's time limit longer than its peers'; each step within
the machine; each report of a manifest the task gives against that file;
once anyone has submitted, the entrant's inputs and their types as they
were. Then `scoring`: each group picks runs by values of repeated inputs the
task fills and holds at least one; group and value names one set, none a
path in the task; each value's blocks (`forge.domain.blocks.check`); and
each `show` entry naming a group, a value, a fact a grading gives
(`compiled`), a report, a file or a folder, at one time only, `compiled`
no later than a group, whose runs' verdicts tell it; the planner refuses a
report done for each run, since only what the steps done once report is
listed by itself (stage 5 section 6 item 30).

A run is early when a group shown `now` holds it, or a value shown `now`
reads a group holding it, directly or through other values (11.3);
`compiled` shown `now`, or a value shown `now` reading it, makes the
compile early even when no run is.
`on_release` is late until staff release the task's results, so
`RunTask.early` takes whether they are, and the plan holds both splits
(`run_plan.ReleasedSplit`). A report is
self-reported when an entrant input reaches the program that wrote it, and
the run's `verdict` and `passed` when one reaches a program given a
manifest (#504).

A value's `exec` block names a program of the task (`ProgramValue`): its
folder holds a `program.yaml` declaring one number report, never left
out, with which way is better, and its group holds runs. Its program runs
early when its value is shown during the contest, or read by one that is
(`RunTask.early_programs`), and a value it works out is self-reported
when any column is, since it reads its runs whole.
"""

from forge.domain.run_task.models import (
    EntrantInput,
    ProgramValue,
    Run,
    RunsFile,
    RunTask,
    RunTaskCheck,
    self_reported_question,
)
from forge.domain.run_task.records import (
    MOST_RUNS,
    RELEASED_OPENNESS,
    RUN_ID,
    RUNS_PATH,
    SHOWN_WORDS,
    Part,
)
from forge.domain.run_task.show_rules import (
    Shown,
    canonical,
    file_time,
    hidden_again,
    holds,
    most_shown,
    openness,
    read_later,
    show_names,
    shown_times,
    shown_under,
    unreleased,
)
from forge.domain.run_task.task_check import check_run_task, fewer_runs, manifest_files
from forge.domain.run_task.task_helpers import key_json

__all__ = [
    "MOST_RUNS",
    "RELEASED_OPENNESS",
    "RUNS_PATH",
    "RUN_ID",
    "SHOWN_WORDS",
    "EntrantInput",
    "Part",
    "ProgramValue",
    "Run",
    "RunTask",
    "RunTaskCheck",
    "RunsFile",
    "Shown",
    "canonical",
    "check_run_task",
    "fewer_runs",
    "file_time",
    "hidden_again",
    "holds",
    "key_json",
    "manifest_files",
    "most_shown",
    "openness",
    "read_later",
    "self_reported_question",
    "show_names",
    "shown_times",
    "shown_under",
    "unreleased",
]
