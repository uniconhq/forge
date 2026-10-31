"""What a task's Woodpecker step is handed and hands back through its
door (SIMPLER-CORE.md 11.6, 11.9, runner's `job.schema.json`), in the words
the services build it with: the names and limits a job keeps to, where a
step's uploads go, an item's key, what an `outputs.json` must be, the
pipeline left once stored steps are taken out, and result version 6.

**An item's key** is the SHA-256 of `{"image", "inputs"}` as canonical
JSON, sorted keys and no spaces, the inputs as the plan binds them with
every file by its content: a file is `{"file": <sha256>}`, a folder
`{"folder": {<path in it>: <sha256>}}`, an earlier step's output by what it
wrote, a template written out. A run's id and where a file lies are not
in it, so two runs, or two gradings, that would do the same work share a
key. The step's limits are not in it either: they come from its inputs.

**A step's uploads** are `outputs.json` and files under `out/`, each path's
segments never empty, `.` or `..` and holding no control character, kept
under `results/<grading>/steps/<step>/`, so nothing a primitive or an
entrant names reaches outside the step's own prefix.

**An `outputs.json`** is read against the primitive contract's
`outputs_file`, at version 6, then against the step: a batch answers every
run of the job in its order and nothing else, a step that holds no batch
answers with `outputs`, every output is one the step declares, of its type,
outcome among them, a required one present once accepted, and every file
or folder it names one the step uploaded, each name in its path one a
job may place (`NAME`), and no text holding a NUL, which the database
keeps in no text. Anything else, an `error` included, is a fault of the
step: nothing it wrote is kept.

**Result version 6** is built when a pipeline has ended. Each run of the
grading's pipeline goes through every step it reads, in the plan's order,
from the save's steps to its own pipeline's, done once or for that run;
its verdict is the outcome of the first that was not `accepted`, `skipped`
when a part it is made from was not given, and `system_error` when a step
faulted or gave nothing for it with no earlier step stopping it, which
names the first such in `error`. Its number reports sit in the result;
every text, a text report, `label`, `message`, `staff_message`, and the
`extras` folder, is named by the stored output that holds it and its
name, `{"output": <id>, "name": <name>}`, so a 64 KB text on each of a
thousand runs leaves the result small. Reports of steps done once go in
`once` on the grading whose pipeline ran the step, with the outcome each
of those steps returned, which the `compiled` fact reads. So do the
outputs and outcomes a step done per run gives the same for every item of
its batch (`once_outputs`, `once_outcomes`), such as a compile log and a
`compile_error`, read from the first item it gave in the pipeline, and
`accepted` when that item's outcome is not one of them; those outputs are
not each run's reports too.

**A save's checks** (SIMPLER-CORE.md 9.6) are graded in its pipeline by
each check's copies of the steps graded per submission: a check's run goes
through the steps done at the save and its own check's copies, and no
grading's run through any copy. The save's result keeps each check's
runs, steps done once and values' numbers under `checks`, its steps named
as the workflow names them, for its expectations to be read against
(`run_results.check_failing`).

**A value's program** is no step of any run's: it reads its group's runs
whole, as a JSON file the forge builds from their stored outputs
(`runs_file`), each run's id, key, verdict, `passed` and number reports,
and writes the value's number, which the result keeps under `values`. A
program that faulted, gave nothing, or ended other than accepted with its
report is a fault of the grading, named in `error`, never a run's
verdict.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from forge.domain import exact_json
from forge.domain.contracts import violation
from forge.domain.errors import InvalidCallback
from forge.domain.run_plan import (
    PIPELINES,
    RUN_ENTRY_KB,
    PipelineKind,
    PipelineStep,
    RunEntry,
    RunPlan,
    RunStep,
)
from forge.domain.run_workflow import STAFF_OUTPUTS

JOB_VERSION = 1
"""The job shape the door answers, runner's `job.schema.json`."""
CONTRACT_VERSION = 6
"""The primitive contract every task's step speaks."""
RESULT_VERSION = 6
"""The result a task's pipeline makes, `result.schema.json`'s version 6."""
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
"""One segment of a path the job places a file at."""
UPLOAD_PATH = re.compile(r"outputs\.json|out(/(?!\.\.?(/|$))[^/\x00-\x1f\x7f]+)+")
OUTPUTS_FILE = "outputs.json"
OUTPUTS_MAX = 64 * 1024 * 1024
"""The most bytes an `outputs.json` may hold."""
UPLOADS_MAX = 10_000
"""The most files one step may upload."""
UPLOAD_PATH_MAX = 1024
"""The most bytes of an upload's path, in UTF-8."""
LINK_LIFE = timedelta(minutes=15)
"""How long a presigned link in a job works: minted when the step asks, so
it has not waited in a queue."""
BOX_FIRST = 20_000
BOX_LAST = 64_999
BOX_BLOCK = 8
"""A boxed step's box users: a block of `BOX_BLOCK`, enough for exec's programs form's
most programs, from `BOX_FIRST` to `BOX_LAST`, apart from the users phase
4.9 gives gradings below `BOX_FIRST`."""
BOX_BLOCKS = (BOX_LAST - BOX_FIRST + 1) // BOX_BLOCK
OUTCOMES = frozenset(
    {
        "accepted",
        "wrong_answer",
        "time_limit",
        "memory_limit",
        "output_limit",
        "runtime_error",
        "compile_error",
    }
)
TEXT_OUTPUTS = ("label", "message")
REPORT_STEP = "report"
"""The step Unicon adds to every grading's pipeline, which tells the door
the pipeline has ended (`workflow_definition.PIPELINE_STEPS`)."""
STEP_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}(\.[A-Za-z0-9_-]+)?")
"""What a Woodpecker step's name may be: `s<n>` by the step's place, `report`
for the report step, or either with `.<run>` for one run. A step's name
inside forge is its path and is not limited by this."""


def is_step_name(name: str) -> bool:
    """Whether `name` may name a pipeline step (`STEP_NAME`)."""
    return STEP_NAME.fullmatch(name) is not None


def results_key(grading: object, step: str, path: str) -> str:
    """Where one file a step uploaded is kept in the results store."""
    return f"results/{grading}/steps/{step}/{path}"


def is_batch(held: PipelineStep) -> bool:
    """Whether a Woodpecker step is a batch: one holding runs under its
    workflow step's own name, where a step per run is `<step>.<run>`."""
    return held.runs is not None and held.name == held.step


def item_key(image: str, inputs: Mapping[str, Any]) -> str:
    """An item's key from its image and its inputs with every file by its
    content."""
    text = exact_json.dumps({"image": image, "inputs": inputs}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def skipped_runs(plan: RunPlan, inputs: Mapping[str, Any]) -> frozenset[str]:
    """The runs a version 6 submission's `inputs` skips: each made from a
    part of an entrant input that was not given."""
    return frozenset(
        run.id
        for run in plan.runs
        if any(
            isinstance(value, dict)
            and set(value) == {"part"}
            and str(value["part"]) not in (inputs.get(name) or {}).get("parts", {})
            for name, value in run.key.items()
        )
    )


def step_order(plan: RunPlan, kind: PipelineKind) -> tuple[str, ...]:
    """The steps a `kind` pipeline reads, in order: every pipeline's up to
    and including `kind`'s, a value's program's among them."""
    found: dict[str, None] = {}
    for each in PIPELINES[: PIPELINES.index(kind) + 1]:
        for held in plan.pipeline(each):
            found.setdefault(held.step, None)
    return tuple(found)


def chain_of(plan: RunPlan, order: Sequence[str], run: str, check: int | None = None) -> list[Item]:
    """The items a run goes through, in `order`: each workflow step done
    once, and each done for it; never a value's program's. A grading's run
    goes through no check's copy; check `check`'s run through the steps
    done at the save and that check's copies."""
    return [
        (step, None if plan.steps[step].once else run)
        for step in order
        if plan.steps[step].value is None
        and _of_check(plan.steps[step], check)
        and (plan.steps[step].once or run in (plan.steps[step].items or {}))
    ]


def _of_check(step: RunStep, check: int | None) -> bool:
    """Whether a run of check `check`, or of the grading itself for none,
    goes through `step`."""
    return step.check is None or (check is not None and step.check.index == check)


def origin(plan: RunPlan, step: str) -> str:
    """The workflow step a step stands for: the one a check's copy copies,
    else itself."""
    found = plan.steps[step].check
    return found.of if found is not None else step


def holds(plan: RunPlan, kind: PipelineKind, step: str, run: str | None) -> bool:
    """Whether a `kind` pipeline does `step` for `run`, or the step once."""
    return any(
        held.step == step and (held.runs is None or run in held.runs)
        for held in plan.pipeline(kind)
    )


def reads_from(kind: PipelineKind) -> tuple[PipelineKind, ...]:
    """The pipelines whose stored outputs a `kind` pipeline may reuse: an
    early one never reads a late one's, so the order rule holds after
    `show` moves (11.3)."""
    return PIPELINES[: PIPELINES.index(kind) + 1]


def without(steps: Sequence[PipelineStep], left_out: Iterable[str]) -> tuple[PipelineStep, ...]:
    """The pipeline with the steps named in `left_out` taken out, every step
    that waited for one waiting instead for what it waited for, so the order
    holds. The names are the plan's, the path or `<path>.<run>`."""
    gone = set(left_out)
    waits = {held.name: held.depends_on for held in steps}

    def kept(names: Iterable[str]) -> list[str]:
        found: list[str] = []
        for name in names:
            for each in kept(waits.get(name, ())) if name in gone else [name]:
                if each not in found:
                    found.append(each)
        return found

    return tuple(
        held.model_copy(update={"depends_on": tuple(kept(held.depends_on))})
        for held in steps
        if held.name not in gone
    )


def output_type(step: RunStep, name: str) -> tuple[str, bool] | None:
    """The type of a step's output `name`, and whether it may be left out,
    or none when the step declares no such output."""
    if name in step.outputs:
        return step.outputs[name], False
    if f"{name}?" in step.outputs:
        return step.outputs[f"{name}?"], True
    return None


# Uploads


@dataclass(frozen=True, slots=True)
class Upload:
    sha256: str
    size: int


def read_uploads(body: bytes, *, item_mb: int, items: int) -> dict[str, Upload]:
    """The files a step says it will upload, `{"files": [{path, sha256,
    size}]}`. `InvalidCallback` for a path it may not write, one listed
    twice, no `outputs.json`, more than `UPLOADS_MAX` files, a file over
    `item_mb` or `OUTPUTS_MAX` for `outputs.json`, or all of them over
    `item_mb` for each of its `items`.
    """
    try:
        document = exact_json.loads(body)
    except UnicodeDecodeError, ValueError, RecursionError:
        raise InvalidCallback("The upload list is not JSON.") from None
    listed = document.get("files") if isinstance(document, dict) else None
    if not isinstance(listed, list) or set(document) != {"files"}:
        raise InvalidCallback('The upload list is {"files": [...]}.')
    if len(listed) > UPLOADS_MAX:
        raise InvalidCallback(f"A step uploads at most {UPLOADS_MAX} files.")
    found: dict[str, Upload] = {}
    for entry in listed:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise InvalidCallback("Each upload is {path, sha256, size}.")
        path, digest, size = entry["path"], entry["sha256"], entry["size"]
        if (
            not isinstance(path, str)
            or len(path.encode("utf-8", "surrogatepass")) > UPLOAD_PATH_MAX
            or not UPLOAD_PATH.fullmatch(path)
            or any("\ud800" <= char <= "\udfff" for char in path)
        ):
            raise InvalidCallback("A step uploads outputs.json and files under out/ only.")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InvalidCallback(f"The upload {path} has no SHA-256.")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InvalidCallback(f"The upload {path} has no size.")
        most = OUTPUTS_MAX if path == OUTPUTS_FILE else item_mb * 1024 * 1024
        if size > most:
            raise InvalidCallback(f"The upload {path} is over {most} bytes.")
        if path in found:
            raise InvalidCallback(f"The upload {path} is listed twice.")
        found[path] = Upload(digest, size)
    if OUTPUTS_FILE not in found:
        raise InvalidCallback("A step always uploads outputs.json.")
    total = sum(upload.size for path, upload in found.items() if path != OUTPUTS_FILE)
    if total > item_mb * 1024 * 1024 * max(items, 1):
        raise InvalidCallback("The uploads are over what the step may write.")
    return found


# outputs.json


@dataclass(frozen=True, slots=True)
class ItemOutputs:
    """What a step wrote for one item: its run, none for a step done once
    or one holding a single run's item, its outputs, and the uploads they
    name, by path."""

    run: str | None
    outputs: dict[str, Any]
    files: dict[str, Upload] = field(default_factory=dict)


class Fault(Exception):
    """What a step wrote is no outputs: why, in a sentence for staff."""


def read_outputs(
    content: bytes,
    step: RunStep,
    runs: Sequence[str | None],
    batch: bool,
    uploads: Mapping[str, Upload],
) -> list[ItemOutputs]:
    """Each item's outputs in `content`, the `outputs.json` of a step that
    was given the items of `runs`, in order. `Fault` for one that is not
    the step's outputs, an `error` included."""
    try:
        document = exact_json.loads(content)
    except UnicodeDecodeError, ValueError, RecursionError:
        raise Fault("outputs.json is not JSON.") from None
    if _holds_nul(document):
        raise Fault("outputs.json holds a NUL character.")
    broken = violation(document, "primitive", "outputs_file")
    if broken is not None:
        raise Fault(f"outputs.json does not keep the contract {broken}")
    if "error" in document:
        raise Fault(f"The step failed: {document['error']}")
    if batch:
        given = document.get("batch")
        if given is None or [entry["run"] for entry in given] != list(runs):
            raise Fault("outputs.json does not answer the job's runs in their order.")
        pairs = [(entry["run"], entry["outputs"]) for entry in given]
    else:
        if "outputs" not in document:
            raise Fault("outputs.json holds a batch for a step given no batch.")
        pairs = [(runs[0], document["outputs"])]
    return [ItemOutputs(run, outputs, _files(step, outputs, uploads)) for run, outputs in pairs]


def _holds_nul(document: object) -> bool:
    """Whether a text anywhere in `document`, a name included, holds a NUL,
    which the database keeps in no text."""
    left = [document]
    while left:
        value = left.pop()
        if isinstance(value, str):
            if "\x00" in value:
                return True
        elif isinstance(value, dict):
            left.extend(value)
            left.extend(value.values())
        elif isinstance(value, list):
            left.extend(value)
    return False


def _files(
    step: RunStep, outputs: Mapping[str, Any], uploads: Mapping[str, Upload]
) -> dict[str, Upload]:
    outcome = outputs.get("outcome")
    if outcome not in OUTCOMES:
        raise Fault("An item's outputs give no outcome.")
    files: dict[str, Upload] = {}
    for name, value in outputs.items():
        declared = output_type(step, name)
        if declared is None:
            raise Fault(f"The step declares no output {name}.")
        kind = declared[0]
        if not _of_type(kind, value):
            raise Fault(f"The output {name} is not a {kind}.")
        if kind == "file":
            if value["file"] not in uploads:
                raise Fault(f"The output {name} names a file the step did not upload.")
            named = {value["file"]: uploads[value["file"]]}
        elif kind == "folder":
            under = value["folder"].rstrip("/") + "/"
            named = {path: found for path, found in uploads.items() if path.startswith(under)}
        else:
            continue
        for path in named:
            if not all(NAME.fullmatch(segment) for segment in path.split("/")):
                raise Fault(f"The output {name} names {path!r}, which a job cannot place.")
        files.update(named)
    if outcome == "accepted":
        missing = [
            name.rstrip("?")
            for name in step.outputs
            if not name.endswith("?") and name not in outputs
        ]
        if missing:
            raise Fault(f"An accepted item's outputs leave out {missing[0]}.")
    return files


def _of_type(kind: str, value: object) -> bool:
    match kind:
        case "outcome":
            return value in OUTCOMES
        case "text" | "enum":
            return isinstance(value, str)
        case "number":
            return isinstance(value, int | Decimal) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "file" | "folder":
            return (
                isinstance(value, dict)
                and set(value) == {kind}
                and isinstance(value[kind], str)
                and value[kind].startswith("out/")
            )
    return False


# Result version 6


@dataclass(frozen=True, slots=True)
class Found:
    """One item's stored outputs: the row that holds them, and what they
    are."""

    id: str
    outputs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Faulted:
    """A step that faulted for an item, and why."""

    reason: str


Item = tuple[str, str | None]
"""A workflow step and the run it is done for, none for a step done once."""


def result_of(
    plan: RunPlan,
    kind: PipelineKind,
    skipped: frozenset[str],
    found: Mapping[Item, Found | Faulted],
) -> dict[str, Any]:
    """Result version 6 of a `kind` pipeline from what each item it reads
    gave, `found`, an item missing from it having given nothing; for a
    save's pipeline, with what each check's runs got (`checks`), each
    check's `passed` and `failing` left for its expectations to fill
    (`run_results.check_failing`)."""
    graded = _graded(plan, kind, skipped, found, None)
    result: dict[str, Any] = {
        "schema_version": RESULT_VERSION,
        "runs": graded["runs"],
        "once": graded["once"],
        **({"values": graded["values"]} if graded["values"] else {}),
        "staff": graded["staff"],
        "error": graded["error"],
        "run_log": None,
    }
    checks = plan.checks() if kind == "save" else ()
    if checks:
        result["checks"] = [
            {
                **{
                    key: value
                    for key, value in _graded(plan, kind, frozenset(), found, index).items()
                    if key != "values" or value
                },
                "passed": False,
                "failing": [],
            }
            for index in checks
        ]
    return result


def _graded(
    plan: RunPlan,
    kind: PipelineKind,
    skipped: frozenset[str],
    found: Mapping[Item, Found | Faulted],
    check: int | None,
) -> dict[str, Any]:
    """The runs, the steps done once, the values' numbers, what staff see
    and the first error of the grading a `kind` pipeline did, or of check
    `check`'s within a save's, its steps named as the workflow names them."""
    order = step_order(plan, kind)
    runs: list[dict[str, Any]] = []
    staff: list[dict[str, Any]] = []
    staffed: set[Item] = set()
    error: str | None = None
    for run in runs_of(plan, kind):
        chain = chain_of(plan, order, run.id, check)
        entry: dict[str, Any] = {"id": run.id, "key": run.key}
        if run.id in skipped:
            runs.append({**entry, "verdict": "skipped", "passed": 0, "reports": {}, "texts": {}})
            continue
        verdict, reached, why = "accepted", [], None
        for item in chain:
            got = found.get(item)
            if not isinstance(got, Found):
                verdict = "system_error"
                why = got.reason if isinstance(got, Faulted) else _nothing(item)
                break
            reached.append(item)
            if got.outputs["outcome"] != "accepted":
                verdict = got.outputs["outcome"]
                break
        if why is not None and error is None:
            error = why
        reports, texts = _reports(plan, found, [item for item in reached if item[1] is not None])
        runs.append(
            {
                **entry,
                "verdict": verdict,
                "passed": 1 if verdict == "accepted" else 0,
                "reports": reports,
                "texts": texts,
                **_shown(found, reached),
            }
        )
        for item in reached:
            if item not in staffed and holds(plan, kind, *item) and _counted(plan, item, check):
                staffed.add(item)
                staff.extend(_staff(found, item))
    once_items = [
        (step, None)
        for step in order
        if plan.steps[step].once
        and holds(plan, kind, step, None)
        and _counted(plan, (step, None), check)
    ]
    batched = [
        first
        for step in order
        if (plan.steps[step].once_outputs or plan.steps[step].once_outcomes)
        and _counted(plan, (step, None), check)
        and (first := _first(plan, kind, step, found)) is not None
    ]
    once_reports, once_texts = _reports(plan, found, [*once_items, *batched], once=True)
    outcomes = {
        origin(plan, step): got.outputs["outcome"]
        for step, _ in once_items
        if isinstance(got := found.get((step, None)), Found)
    }
    for first in batched:
        got = found[first]
        assert isinstance(got, Found)
        given = got.outputs["outcome"]
        once_outcomes = plan.steps[first[0]].once_outcomes or ()
        outcomes[origin(plan, first[0])] = given if given in once_outcomes else "accepted"
    values: dict[str, Any] = {}
    for step, _ in once_items:
        value = plan.steps[step].value
        if value is None or value.report is None:
            continue
        got = found.get((step, None))
        if (
            isinstance(got, Found)
            and got.outputs["outcome"] == "accepted"
            and value.report in got.outputs
        ):
            values[value.name] = got.outputs[value.report]
            staff.extend(_staff(found, (step, None)))
        elif error is None:
            error = got.reason if isinstance(got, Faulted) else _nothing((step, None))
    return {
        "runs": runs,
        "once": {"reports": once_reports, "texts": once_texts, "outcomes": outcomes},
        "values": values,
        "staff": staff,
        "error": error,
    }


def _first(
    plan: RunPlan, kind: PipelineKind, step: str, found: Mapping[Item, Found | Faulted]
) -> Item | None:
    """The first item of a step done per run that a `kind` pipeline holds
    and that gave its outputs, in the order its batch holds them; none when
    none did."""
    for held in plan.pipeline(kind):
        if held.step != step:
            continue
        for run in held.runs or ():
            if isinstance(found.get((step, run)), Found):
                return step, run
    return None


def _counted(plan: RunPlan, item: Item, check: int | None) -> bool:
    """Whether the result of the grading, or of check `check`, keeps what
    `item` gave of its own: a check's of its copies alone, the grading's of
    no copy."""
    found = plan.steps[item[0]].check
    return found is None if check is None else found is not None and found.index == check


def runs_file(
    plan: RunPlan,
    kind: PipelineKind,
    ids: Sequence[str],
    skipped: frozenset[str],
    found: Mapping[Item, Found],
    check: int | None = None,
) -> bytes | None:
    """The JSON file a value's program reads in a `kind` pipeline: the runs
    `ids` names, in that order, each with its id, its key, its verdict,
    `passed` and its number reports, the reports of steps done once among
    them, as a fold reads it, and nothing else, so no text and nothing for
    staff; none while an item a run goes through has given nothing. A
    number past `MOST_REPORT_CHARACTERS`, and every report of a run whose
    entry would pass `RUN_ENTRY_KB`, is left out, missing to the program,
    so what an entrant reports never faults the grading. A check's copy
    of a program reads that check's runs (`check`).
    """
    order = step_order(plan, kind)
    keys = {run.id: run.key for run in plan.runs}
    runs: list[dict[str, Any]] = []
    for run in ids:
        if run in skipped:
            runs.append(
                {"id": run, "key": keys[run], "verdict": "skipped", "passed": 0, "reports": {}}
            )
            continue
        verdict, reached = "accepted", []
        for item in chain_of(plan, order, run, check):
            got = found.get(item)
            if got is None:
                return None
            reached.append(item)
            if got.outputs["outcome"] != "accepted":
                verdict = got.outputs["outcome"]
                break
        reports, _ = _reports(plan, found, reached)
        entry: dict[str, Any] = {
            "id": run,
            "key": keys[run],
            "verdict": verdict,
            "passed": 1 if verdict == "accepted" else 0,
            "reports": {
                name: number for name, number in reports.items() if _fits_runs_file(number)
            },
        }
        if len(exact_json.dumps(entry, sort_keys=True).encode()) > MOST_RUN_ENTRY:
            # Too large to hand the program: its reports count as missing.
            entry["reports"] = {}
        runs.append(entry)
    return exact_json.dumps({"runs": runs}, sort_keys=True).encode()


MOST_RUN_ENTRY = RUN_ENTRY_KB * 1024 - 64
"""The most bytes one run's entry in a runs file takes, room left for the
file's own brackets and commas, so the file keeps within `RUN_ENTRY_KB` a
run."""
MOST_REPORT_CHARACTERS = 100
"""The longest a number report may be written in a runs file; a longer
one, an entrant's self-reported number among them, is missing to the
program, as a number past `MOST_BITS` is to a block."""


def _fits_runs_file(number: object) -> bool:
    """Whether a number report goes into a runs file as it is."""
    return len(exact_json.dumps(number)) <= MOST_REPORT_CHARACTERS


def program_alone(result: Mapping[str, Any]) -> bool:
    """Whether a result's `error` is a value's program's alone: no run it
    grades is a `system_error`. `result_of` sets `error` only from a run's
    chain, where a workflow step's fault, done once or for the run, is the
    run's verdict, or from a value's program."""
    return all(run.get("verdict") != "system_error" for run in result.get("runs", ()))


def unreported(result: Mapping[str, Any] | None, values: Iterable[str]) -> frozenset[str]:
    """Those of `values`, each one whose program a row's plan runs, that a
    row's result with an `error` gives no number: a program that failed,
    whose value waits for staff."""
    if result is None or result.get("error") is None:
        return frozenset()
    given = result.get("values") or {}
    return frozenset(name for name in values if name not in given)


def all_accepted(result: Mapping[str, Any] | None) -> bool:
    """Whether result version 6 accepts every run it grades, a skipped one
    among them, and every step done once it ran, a value's program's
    compile among them, and whether each check got what it expects: what a
    save's checking needs for its publication to take over."""
    if result is None:
        return False
    outcomes = (result.get("once") or {}).get("outcomes") or {}
    return (
        all(run.get("verdict") in ("accepted", "skipped") for run in result.get("runs", ()))
        and all(outcome == "accepted" for outcome in outcomes.values())
        and all(check.get("passed") is True for check in result.get("checks", ()))
    )


def runs_of(plan: RunPlan, kind: PipelineKind) -> list[RunEntry]:
    """The runs a `kind` pipeline grades: every run for the save's, else
    the early ones or the late ones."""
    if kind == "save":
        return list(plan.runs)
    return [run for run in plan.runs if run.early == (kind == "early")]


def _nothing(item: Item) -> str:
    step, run = item
    return f"The step {step} gave nothing" + (f" for the run {run}." if run else ".")


def _text(found: Found, name: str) -> dict[str, str]:
    return {"output": found.id, "name": name}


def _reports(
    plan: RunPlan,
    found: Mapping[Item, Found | Faulted],
    items: Sequence[Item],
    *,
    once: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The report entries the steps of `items` wrote: numbers as they are,
    texts named. An output a step done per run gives the same for every
    item is read from an item of it only for the reports given `once`, and
    every other output only for the rest."""
    reports: dict[str, Any] = {}
    texts: dict[str, Any] = {}
    for name, entry in sorted(plan.reports.items()):
        for step, run in items:
            got = found.get((step, run))
            if (
                origin(plan, step) != entry.step
                or not isinstance(got, Found)
                or entry.output not in got.outputs
                or (
                    run is not None
                    and (entry.output in (plan.steps[step].once_outputs or ())) != once
                )
            ):
                continue
            declared = output_type(plan.steps[step], entry.output)
            if declared is not None and declared[0] == "number":
                reports[name] = got.outputs[entry.output]
            elif declared is not None and declared[0] == "text":
                texts[name] = _text(got, entry.output)
    return reports, texts


def _shown(found: Mapping[Item, Found | Faulted], reached: Sequence[Item]) -> dict[str, Any]:
    """`label` and `message`: the verdict's step's, else the last step's
    that wrote one."""
    shown: dict[str, Any] = {}
    for item in reached:
        got = found[item]
        assert isinstance(got, Found)
        for name in TEXT_OUTPUTS:
            if name in got.outputs:
                shown[name] = _text(got, name)
    return shown


def _staff(found: Mapping[Item, Found | Faulted], item: Item) -> list[dict[str, Any]]:
    got = found[item]
    assert isinstance(got, Found)
    kept = {name: _text(got, name) for name in STAFF_OUTPUTS if name in got.outputs}
    return [{"step": item[0], "run": item[1], **kept}] if kept else []
