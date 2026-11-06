"""A task checked against its run workflow, as its save checks it.

`binder` fills each input and numbers the runs, `show_rules` says what is
shown when, and `task_check` reports what does not fit. See SIMPLER-CORE.md
8 and 9.
"""

from forge.domain.blocks.catalogue import FIRST_NOT_PASSED
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
    RUN_FIELDS,
    ShowNames,
    ShownUnder,
    canonical,
    covers,
    draft_problems,
    hidden_again,
    holds,
    listed,
    listed_words,
    members,
    most_shown,
    once_problems,
    openness,
    picks,
    read_later,
    results,
    show_problems,
    shown_under,
    shown_when,
    unreleased,
    value_moment,
)
from forge.domain.run_task.task_check import check_run_task, fewer_runs, manifest_files
from forge.domain.run_task.task_helpers import key_json

__all__ = [
    "FIRST_NOT_PASSED",
    "MOST_RUNS",
    "RELEASED_OPENNESS",
    "RUNS_PATH",
    "RUN_FIELDS",
    "RUN_ID",
    "SHOWN_WORDS",
    "EntrantInput",
    "Part",
    "ProgramValue",
    "Run",
    "RunTask",
    "RunTaskCheck",
    "RunsFile",
    "ShowNames",
    "ShownUnder",
    "canonical",
    "check_run_task",
    "covers",
    "draft_problems",
    "fewer_runs",
    "hidden_again",
    "holds",
    "key_json",
    "listed",
    "listed_words",
    "manifest_files",
    "members",
    "most_shown",
    "once_problems",
    "openness",
    "picks",
    "read_later",
    "results",
    "self_reported_question",
    "show_problems",
    "shown_under",
    "shown_when",
    "unreleased",
    "value_moment",
]
