"""A task checked against its run workflow, as its save checks it.

`binder` fills each input and numbers the runs, `show_rules` says what is
shown when, and `task_check` reports what does not fit. See SIMPLER-CORE.md
8 and 9.
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
