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
   task has a graded submission (T7, T10). A workflow
   is named `<owner>/<name>`, the owner an org by its name or a person by their
   username; when the latest publication used a workflow of
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
   not listed later or dropped; and the task keeps
   every value a leaderboard of its contest reads, by its name, and gives
   no column a reason to be refused across files, such as a value a pick
   chooses by now reading a mark (`board_checks.across`), each problem
   naming the board and the column.
3. What the save changes about how the task grades is worked out against
   the latest publication: its plan and the digests of the task's files the
   plan names. Groups, rule weights, `show`, `credit` and `submissions` are
   read on every read, and change nothing that grades. A save asked
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
   each scoring change, and the boards reading a value it moves or, on a
   first publication, reading the task; and notes the
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

from forge.services.task_save.checking import _ask, _check_runs, _checked_first, _parsed, check
from forge.services.task_save.effect import _differing, _programs_again, _report, _save_effect
from forge.services.task_save.guards import (
    _leaderboard_reads,
    _own_since,
    _present,
    _refuse_admin_only,
    _refuse_reserved,
    _refuse_untokened_removals,
    _served,
    _shown_before,
    _shown_before_runs,
    _task_revealed,
)
from forge.services.task_save.listing import _held_pointer, _resolve, list_publications
from forge.services.task_save.records import (
    CHANGED_CONTENT,
    GROUPS_KEPT,
    LANDED_UNDER,
    OPENNESS,
    PIN_ANSWER,
    PIN_MOVED,
    SAVE_MESSAGE,
    SHOWN_WORDS,
    Checked,
    Draft,
    Published,
)
from forge.services.task_save.save_flow import _regrades, save
from forge.services.task_save.sources import (
    _content,
    _definition_of,
    _digest,
    _Found,
    _graded_under,
    _groups_of,
    _moved_workflows,
    _number_of,
    _Opened,
    _primitives,
    _published_snapshot,
    _runs_file,
    _runs_file_of,
    _saved_snapshot,
    _submitted,
    _under,
    _unopened,
    _workflow,
)
from forge.services.task_save.stored_facts import (
    FACTS_BATCH,
    RUNS_UNREAD,
    _batches,
    _fact_of,
    _format_kept,
    _marks_digest,
    _task_facts,
    _workflow_facts,
    _workflow_problem,
)
from forge.services.task_save.task_form import (
    DeclaredField,
    DeclaredInput,
    WorkflowForm,
    _workflow_form,
    workflow_form,
)
from forge.services.task_save.writing import (
    _contest,
    _contest_running,
    _draft,
    _landed_under,
    _plan_files,
    _write,
)

__all__ = [
    "CHANGED_CONTENT",
    "FACTS_BATCH",
    "GROUPS_KEPT",
    "LANDED_UNDER",
    "OPENNESS",
    "PIN_ANSWER",
    "PIN_MOVED",
    "RUNS_UNREAD",
    "SAVE_MESSAGE",
    "SHOWN_WORDS",
    "Checked",
    "DeclaredField",
    "DeclaredInput",
    "Draft",
    "Published",
    "WorkflowForm",
    "_Found",
    "_Opened",
    "_ask",
    "_batches",
    "_check_runs",
    "_checked_first",
    "_content",
    "_contest",
    "_contest_running",
    "_definition_of",
    "_differing",
    "_digest",
    "_draft",
    "_fact_of",
    "_format_kept",
    "_graded_under",
    "_groups_of",
    "_held_pointer",
    "_landed_under",
    "_leaderboard_reads",
    "_marks_digest",
    "_moved_workflows",
    "_number_of",
    "_own_since",
    "_parsed",
    "_plan_files",
    "_present",
    "_primitives",
    "_programs_again",
    "_published_snapshot",
    "_refuse_admin_only",
    "_refuse_reserved",
    "_refuse_untokened_removals",
    "_regrades",
    "_report",
    "_resolve",
    "_runs_file",
    "_runs_file_of",
    "_save_effect",
    "_saved_snapshot",
    "_served",
    "_shown_before",
    "_shown_before_runs",
    "_submitted",
    "_task_facts",
    "_task_revealed",
    "_under",
    "_unopened",
    "_workflow",
    "_workflow_facts",
    "_workflow_form",
    "_workflow_problem",
    "_write",
    "check",
    "list_publications",
    "save",
    "workflow_form",
]
