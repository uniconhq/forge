from typing import Annotated, Any, Literal

from pydantic import Field, PlainValidator

from forge.domain.plan_basics import FLAT_STEP_ID
from forge.domain.roles import PRIMITIVE_OWNER
from forge.domain.run_task import RUN_ID
from forge.domain.yaml_models import is_number

SCHEMA_VERSION: Literal[6] = 6
StepClass = Literal["save_once", "save_per_run", "grading_once", "grading_per_run"]
PipelineKind = Literal["save", "early", "late"]
PIPELINES: tuple[PipelineKind, ...] = ("save", "early", "late")
GradingKind = Literal["early", "late"]
"""The pipelines of a grading, which a release may lay out otherwise."""
RELEASE_MOVED = (
    "What a release shows moved, so a grading made while results are released runs other runs "
    "early."
)
"""The words a save says when it moves only the released split, which
grades nothing again while the task's results are not released."""
EXEC_USE = "unicon/exec@v2"
PROGRAM_USES = (EXEC_USE,)
"""The primitive a value's `exec` program is compiled and run with, and a
source of the task's own is checked with at the save, by its `build` form,
which a save reads when a value has one or the workflow uses it."""
RETIRED = f"{PRIMITIVE_OWNER}/compile and {PRIMITIVE_OWNER}/exec@v1 are retired; use {EXEC_USE}."
"""What a step naming a retired primitive is refused with."""
BUILT = ("source", "language", "entry")
"""The inputs a program is compiled from, which the `build` form of
`EXEC_USE` takes as the step it checks was given them."""
RUNS_FILE = "runs.json"
"""The name a value's program reads its group's runs under."""
RUN_ENTRY_KB = 16
"""The most one run takes in a program's runs file, which bounds the file
as its box holds it."""
ONCE: tuple[StepClass, ...] = ("save_once", "grading_once")
CLASS_WORDS: dict[StepClass, str] = {
    "save_once": "once, at the save",
    "save_per_run": "once per run, at the save",
    "grading_once": "once per grading",
    "grading_per_run": "once per run of each grading",
}
_READS: dict[StepClass, tuple[bool, bool]] = {
    "save_once": (False, False),
    "save_per_run": (False, True),
    "grading_once": (True, False),
    "grading_per_run": (True, True),
}
"""What a step of each class reads: the entrant's inputs, and a repeated
input."""
SHOWN_DURING = ("now", "on_release")
"""The times of `show` that count as during the contest: a release has no
time, so it is refused as if shown `now`, though it makes no run early."""
BOXED: dict[str, tuple[str, ...]] = {
    "unicon/exec": ("source", "files", "loads"),
}
"""The primitives that run programs in isolate boxes, by name, with the
ports whose files each copies into a box's tmpfs, which is charged to the
container and not the box: `exec`'s own ports, and each program's `source`
and `files` of its programs form (`loads` is only the one form's). A
`source` is compiled in a box of its own, and what is copied into each run
box is the program, at most `PROGRAM_MB`."""
PROGRAM_MB = 32
"""The most a compiled program may hold, exec's cap, which bounds the copy
of it each run box takes."""
PAGE_KB = 4
PRIMITIVE_ROOM_MB = 112
"""What a boxed primitive's container keeps for the step wrapper, the
primitive itself and isolate beside its boxes' memory limits and the files
it copies into them: an item needing more of the container than its
memory, less this, is refused before any program runs (`STEP_MEMORY_KB` in
`exec`, and its README). The planner gives each
container at least the three together."""
BOX_MEMORY = "memory_limit"
"""The input, of a boxed primitive or of each of its programs, that gives a
box's memory limit in MB."""
COMPILE_ERROR = "compile_error"
COMPILED = "compiled"
_UNSHOWN = ", which entrants are not shown during the contest, so {entry} could tell them about it"
PIPELINE_WORDS: dict[PipelineKind, str] = {
    "save": "The steps done at the save",
    "early": "The steps of a grading's early pipeline",
    "late": "The steps of a grading's late pipeline",
}
SHA256 = r"^[0-9a-f]{64}$"
COMMIT = r"^[0-9a-f]{40}([0-9a-f]{24})?$"
OUTPUT = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$"
PIPELINE_STEP = r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*(\.[A-Za-z0-9_-]+)?$"


def _file_value(value: object) -> bool:
    """A task file, or a workflow file of the outer pin or of pin k."""
    if not isinstance(value, dict):
        return False
    keys = set(value)
    if keys == {"task"} or keys == {"workflow"}:
        return isinstance(value.get("task", value.get("workflow")), str)
    pin = value.get("pin")
    return (
        keys == {"workflow", "pin"}
        and isinstance(value.get("workflow"), str)
        and isinstance(pin, int)
        and not isinstance(pin, bool)
        and pin >= 1
    )


def _entrant_value(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("entrant"), str):
        return False
    part = value.get("part")
    return set(value) == {"entrant"} or (
        set(value) == {"entrant", "part"}
        and isinstance(part, int)
        and not isinstance(part, bool)
        and part >= 1
    )


def _output_value(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"step", "output"}
        and all(isinstance(value[key], str) for key in value)
    )


def _runs_value(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"runs"}
        and isinstance(value["runs"], list)
        and bool(value["runs"])
        and all(isinstance(each, str) and RUN_ID.fullmatch(each) for each in value["runs"])
        and len(set(value["runs"])) == len(value["runs"])
    )


def _run_value(value: object) -> dict[str, Any]:
    """One value of a step's inputs, in exactly one of the plan's shapes."""
    if not isinstance(value, dict):
        raise ValueError("a value is a mapping")
    keys = set(value)
    if keys == {"value"} and (isinstance(value["value"], str | bool) or is_number(value["value"])):
        return value
    if _file_value(value) or _entrant_value(value) or _output_value(value):
        return value
    if keys == {"secret"} and isinstance(value["secret"], str):
        return value
    if (
        keys == {"template", "parts"}
        and isinstance(value["template"], str)
        and isinstance(value["parts"], list)
        and value["parts"]
        and all(_entrant_value(part) for part in value["parts"])
    ):
        return value
    if keys == {"files"} and isinstance(value["files"], dict):
        for each in value["files"].values():
            if not (
                _file_value(each)
                or _entrant_value(each)
                or _output_value(each)
                or _runs_value(each)
            ):
                raise ValueError(f"{each!r} is not a file a plan holds")
        return value
    if keys == {"programs"} and isinstance(value["programs"], dict) and value["programs"]:
        for inputs in value["programs"].values():
            if not isinstance(inputs, dict):
                raise ValueError("a program's inputs are a mapping")
            for each in inputs.values():
                _run_value(each)
        return value
    if keys == {"links"} and isinstance(value["links"], list):
        return value
    raise ValueError(f"{value!r} is not a value a plan holds")


def _taint(value: object) -> dict[str, Any]:
    if isinstance(value, dict) and set(value) == {"beside"} and _file_value(value["beside"]):
        return value
    if isinstance(value, dict) and set(value) == {"reads"} and isinstance(value["reads"], str):
        return value
    raise ValueError(f"{value!r} is not why a step is tainted")


Value = Annotated[dict[str, Any], PlainValidator(_run_value)]
Inputs = dict[str, Value]
Taint = Annotated[dict[str, Any], PlainValidator(_taint)]
RunId = Annotated[str, Field(pattern=RUN_ID.pattern)]
StepId = Annotated[str, Field(pattern=FLAT_STEP_ID)]


def retired(owner: str, name: str, version: str) -> bool:
    """Whether a primitive version is one `RETIRED` names."""
    return owner == PRIMITIVE_OWNER and (name == "compile" or (name == "exec" and version == "v1"))
