from collections.abc import Mapping, Sequence

from forge.domain.content import named_by
from forge.domain.plan_basics import PLAN_PATH, Snapshot
from forge.domain.run_plan.models import EntrantEntry, Item, RunPlan, RunStep
from forge.domain.run_plan.plan_helpers import _leaves
from forge.domain.run_plan.records import RELEASE_MOVED

# What a save changes


def run_plan_changes(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """What changed in a run task's plan between the previous publication and
    this one, in words a person reads, for the save's summary only: the
    workflow's version and files, each step added, removed or given other
    inputs, a release moving a primitive's image in place, runs added or
    removed, a task file a step is given, and what is shown during the
    contest, before a release or while one stands, moving which runs are
    early. Empty for a first publication.
    What a regrade reruns is not decided from this list.
    """
    if before is None:
        return ()
    new = RunPlan.from_bytes(after.plans[PLAN_PATH])
    try:
        old = RunPlan.from_bytes(before.plans[PLAN_PATH]) if PLAN_PATH in before.plans else None
    except ValueError:
        old = None
    if old is None:
        return ("The way the task grades changed.",)
    changes: list[str] = []
    if old.workflow.ref != new.workflow.ref:
        changes.append(f"The workflow changed from {old.workflow.ref} to {new.workflow.ref}.")
    elif old.workflow.commit != new.workflow.commit:
        changes.append(f"The workflow {new.workflow.ref} moved to another commit.")
    changes += _compared(old.workflow.files, new.workflow.files, "The workflow's file {}")
    old_inner = {pin.ref: pin for pin in old.inner_workflows}
    new_inner = {pin.ref: pin for pin in new.inner_workflows}
    for ref in sorted(set(old_inner) | set(new_inner)):
        if ref not in old_inner:
            changes.append(f"The workflow {ref} is now used.")
        elif ref not in new_inner:
            changes.append(f"The workflow {ref} is no longer used.")
        elif old_inner[ref].commit != new_inner[ref].commit:
            changes.append(f"The workflow {ref} moved to another commit.")
        else:
            changes += _compared(
                old_inner[ref].files, new_inner[ref].files, f"The workflow {ref}'s file {{}}"
            )
    # A check's copy grades no submission, so it changes nothing here.
    graded_old = [name for name, step in old.steps.items() if step.check is None]
    graded_new = [name for name, step in new.steps.items() if step.check is None]
    for step_id in [*graded_new, *(each for each in graded_old if each not in graded_new)]:
        was, now = old.steps.get(step_id), new.steps.get(step_id)
        if was is None:
            changes.append(f"The step {step_id} was added.")
        elif now is None:
            changes.append(f"The step {step_id} was removed.")
        elif was.primitive != now.primitive:
            changes.append(f"The step {step_id} now uses {now.primitive}.")
        elif was.image != now.image:
            changes.append(f"A release updated {now.primitive.partition('/')[2]} in place.")
        elif _inputs(was, now.items) != _inputs(now, was.items):
            changes.append(f"The inputs of the step {step_id} changed.")
    runs_before = {run.id: run for run in old.runs}
    runs_after = {run.id: run for run in new.runs}
    added = [run for run in runs_after if run not in runs_before]
    removed = [run for run in runs_before if run not in runs_after]
    if added:
        changes.append(f"{_counted(added)} added.")
    if removed:
        changes.append(f"{_counted(removed)} removed.")
    changes += _sizes(old, new)
    changes += _compared(before.data, after.data, "The file {}")
    moved = any(
        runs_before[run].early != runs_after[run].early for run in runs_before if run in runs_after
    ) or {step.step for step in old.pipeline("early") if step.runs is None} != {
        step.step for step in new.pipeline("early") if step.runs is None
    }
    if moved:
        changes.append("What is shown during the contest moved, so other runs are graded early.")
    released_before = {run.id: run.early for run in old.for_split(True).runs}
    released_after = {run.id: run.early for run in new.for_split(True).runs}
    if not moved and any(
        released_before[run] != released_after[run]
        for run in released_before
        if run in released_after
    ):
        changes.append(RELEASE_MOVED)
    return tuple(dict.fromkeys(changes))


def pin_moved(before: Snapshot | None, after: Snapshot) -> bool:
    """Whether a save moves the workflow's pin (SIMPLER-CORE.md 11.8): the
    task names another version or another workflow than the previous
    publication's plan, or the same at another commit. A first
    publication, or one whose plan does not read, moves none."""
    if before is None or PLAN_PATH not in before.plans:
        return False
    try:
        old = RunPlan.from_bytes(before.plans[PLAN_PATH])
    except ValueError:
        return False
    new = RunPlan.from_bytes(after.plans[PLAN_PATH])
    if (old.workflow.ref, old.workflow.commit) != (new.workflow.ref, new.workflow.commit):
        return True
    old_pins = {pin.ref: pin.commit for pin in old.inner_workflows}
    new_pins = {pin.ref: pin.commit for pin in new.inner_workflows}
    return old_pins != new_pins


def steps_run_again(before: Snapshot | None, after: Snapshot) -> tuple[str, ...]:
    """The steps a submission graded under the previous publication runs
    again under this one, in the order its pipelines run them, for the
    save's effect: each
    step of the early and late pipelines that was added, uses another
    primitive or image, is given other inputs, reads a task or workflow
    file whose content changed, or reads an output of a step that runs
    again, since the store runs again only what changed by key. A run added
    or removed moves no step's other runs. A step done at the save runs
    once, at the save, and is left out. Every graded step when the previous
    plan no longer reads; none for a first publication.
    """
    if before is None:
        return ()
    new = RunPlan.from_bytes(after.plans[PLAN_PATH])
    graded = dict.fromkeys(each.step for kind in ("early", "late") for each in new.pipeline(kind))
    try:
        old = RunPlan.from_bytes(before.plans[PLAN_PATH]) if PLAN_PATH in before.plans else None
    except ValueError:
        old = None
    if old is None:
        return tuple(graded)
    again: set[str] = set()
    moved = True
    while moved:
        moved = False
        for step_id, now in new.steps.items():
            if step_id not in again and _runs_again(old, new, step_id, now, again):
                again.add(step_id)
                moved = True
    return tuple(step_id for step_id in graded if step_id in again)


def _runs_again(old: RunPlan, new: RunPlan, step_id: str, now: RunStep, again: set[str]) -> bool:
    was = old.steps.get(step_id)
    if was is None or was.primitive != now.primitive or was.image != now.image:
        return True
    if _inputs(was, now.items) != _inputs(now, was.items):
        return True
    inputs = (
        [now.inputs]
        if now.inputs is not None
        else [each.inputs for each in (now.items or {}).values()]
    )
    for value in _leaves(value for given in inputs for value in given.values()):
        if "task" in value:
            paths = named_by(new.task_files, value["task"]) | named_by(
                old.task_files, value["task"]
            )
            if any(old.task_files.get(path) != new.task_files.get(path) for path in paths):
                return True
        elif "workflow" in value:
            pin = value.get("pin", 0)
            try:
                ours, theirs = new.pinned(pin).files, old.pinned(pin).files
            except IndexError:
                return True
            paths = named_by(ours, value["workflow"]) | named_by(theirs, value["workflow"])
            if any(theirs.get(path) != ours.get(path) for path in paths):
                return True
        elif "step" in value and value["step"] in again:
            return True
        elif "runs" in value and (
            old.reports != new.reports or any(new.steps[each].value is None for each in again)
        ):
            # A program reads its runs' verdicts and reports, which a step
            # run again, or other reports, may change.
            return True
    return False


def _inputs(step: RunStep, other: Mapping[str, Item] | None) -> object:
    """What a step is given, each run's by its id among those `other`, the
    same step in the other plan, holds too, for comparing: a run added or
    removed is no change of the step's inputs.
    """
    if step.inputs is not None:
        return step.inputs
    return {run: item.inputs for run, item in (step.items or {}).items() if run in (other or {})}


def _compared(before: Mapping[str, object], after: Mapping[str, object], named: str) -> list[str]:
    found: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            found.append(f"{named.format(key)} was added.")
        elif key not in after:
            found.append(f"{named.format(key)} was removed.")
        elif before[key] != after[key]:
            found.append(f"{named.format(key)} changed.")
    return found


def _sizes(before: RunPlan, after: RunPlan) -> list[str]:
    """A part's largest size that moved. A title is not a change, so a
    titles-only save says nothing here.
    """
    found: list[str] = []
    for name in sorted(set(before.entrant) | set(after.entrant)):
        was, now = before.entrant.get(name), after.entrant.get(name)
        if was is None or now is None:
            continue
        count = max(_count(was), _count(now))
        old_sizes = _part_sizes(was, count)
        new_sizes = _part_sizes(now, count)
        if old_sizes == new_sizes:
            continue
        if was.parts is None and now.parts is None:
            found.append(f"The largest size of {name} changed.")
            continue
        for index, (old_size, new_size) in enumerate(zip(old_sizes, new_sizes, strict=True)):
            if old_size != new_size:
                found.append(f"The largest size of part {index + 1} of {name} changed.")
    return found


def _count(entry: EntrantEntry) -> int:
    if entry.part_details:
        return len(entry.part_details)
    return 1 if entry.max_size is not None else 0


def _part_sizes(entry: EntrantEntry, count: int) -> tuple[int | None, ...]:
    """Each part's size, `count` long. An entry with no part details gives
    its one size to every part, which is what a reader that knows nothing
    of parts uses. A shorter list of parts is filled out so a save that
    adds a part can say which one moved.
    """
    if entry.part_details:
        sizes: list[int | None] = [part.max_size for part in entry.part_details]
    elif entry.max_size is not None:
        sizes = [entry.max_size] * count
    else:
        sizes = []
    sizes.extend([None] * (count - len(sizes)))
    return tuple(sizes)


def _counted(runs: Sequence[str]) -> str:
    if len(runs) == 1:
        return f"The run {runs[0]} was"
    return f"The runs {', '.join(runs)} were"
