import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from forge.domain.content import named_by
from forge.domain.definitions import DEFAULT_MAX_SIZE, Scoring, TaskDefinition
from forge.domain.grading import WALL_CEILING, Machine, wall_of
from forge.domain.plan_basics import ReportEntry, StepLimits
from forge.domain.port_values import escaped, scalar_to_text
from forge.domain.primitives import (
    BATCH_SCALED,
    LIMIT_NAMES,
    PROGRAMS,
    SOURCE,
    FromInput,
    FromMax,
    FromSum,
    Port,
    PrimitiveDeclaration,
)
from forge.domain.run_plan.models import (
    EntrantEntry,
    FileFact,
    Item,
    PartDetails,
    PipelineStep,
    RunStep,
)
from forge.domain.run_plan.plan_helpers import _declared, _number, _pages, _references_in, _words
from forge.domain.run_plan.records import (
    _READS,
    _UNSHOWN,
    BOX_MEMORY,
    BOXED,
    BUILT,
    COMPILE_ERROR,
    COMPILED,
    EXEC_USE,
    ONCE,
    PIPELINE_WORDS,
    PIPELINES,
    PRIMITIVE_ROOM_MB,
    PROGRAM_MB,
    RUN_ENTRY_KB,
    RUNS_FILE,
    SHOWN_DURING,
    PipelineKind,
    StepClass,
)
from forge.domain.run_task import SHOWN_WORDS, Part, RunTask, file_time, show_names
from forge.domain.run_workflow import ResolvedStep, RunWorkflowCheck
from forge.domain.types import FILES, Type
from forge.domain.workflow_definition import SELF_REPORTED, RunWorkflow, references, whole_reference
from forge.domain.yaml_models import Path, Problem, is_number, path_text


def _last(steps: Sequence[PipelineStep]) -> tuple[str, ...]:
    """The steps of a pipeline no other step of it waits for."""
    waited = {name for held in steps for name in held.depends_on}
    return tuple(held.name for held in steps if held.name not in waited)


@dataclass(frozen=True, slots=True)
class _Copy:
    """A file a boxed primitive copies into a box: the kilobytes it takes
    in the box's tmpfs, whole pages, what it is in words, and the task's
    input that gives it, none for the workflow's file or a step's output.
    """

    kb: int
    words: str
    input: str | None


@dataclass
class _Bound:
    """One run of a step, or its one run when done once: its inputs, every
    hidden file its outputs carry, why it is tainted, and its limits.
    """

    inputs: dict[str, Any]
    hidden: list[dict[str, Any]]
    tainted: dict[str, Any] | None
    limits: dict[str, int] = field(default_factory=dict)
    copies: list[_Copy] = field(default_factory=list)


_ABSENT = object()


class _Planner:
    def __init__(
        self,
        definition: TaskDefinition,
        scoring: Scoring,
        workflow: RunWorkflow,
        checked: RunWorkflowCheck,
        task: RunTask,
        task_files: Mapping[str, FileFact],
        workflow_files: Mapping[str, FileFact],
        machine: Machine,
        inner_files: Mapping[int, Mapping[str, FileFact]] | None = None,
    ) -> None:
        self.definition = definition
        self.scoring = scoring
        self.workflow = workflow
        self.checked = checked
        self.task = task
        self.task_files = task_files
        self.workflow_files = workflow_files
        self.machine = machine
        self.inner_files = inner_files or {}
        self.entrant = frozenset(task.entrant)
        self.repeated = frozenset(task.runs[0].key) if task.runs else frozenset()
        """The inputs whose value differs per run: each repeated one, the
        entrant's given in parts among them."""
        self.classes: dict[str, StepClass] = {}
        self.reads: dict[str, list[str]] = {}
        """The steps each step reads directly, in the order it names them."""
        self.raising: dict[str, list[str]] = {}
        """The task's inputs each step raises its time from."""
        self.bound: dict[tuple[str, str | None], _Bound] = {}
        self.given_by: dict[str, str] = {}
        """The task's input each task file value is given by."""
        self.output_mb: dict[str, int] = {}
        """The most any run of each step may write, which bounds a file it
        gives another step."""
        self.once_ms: dict[str, int] = {}
        """The time each step's container takes once before its items, as
        its declaration gives it (`once_time_ms`)."""
        self.compiling: set[str] = set()
        """The steps that compile the entrant's code and give a compile
        error the same for every item, which `compiled` reads."""
        self.leaking: dict[str, str | None] = {}
        """The steps that would be among `compiling` but for a compile
        error that could say what a hidden file holds, each with the run of
        such an item, None for a step done once."""
        self.program_steps: dict[str, RunStep] = {}
        """The steps the planner adds to the workflow's, by id: the build
        of each source of the task's own at the save (`builds`), and each
        value's `exec` program (`programs`)."""

    # Binding

    def bind(self, step_id: str, given: Mapping[str, Any]) -> None:
        resolved = self.checked.steps[step_id]
        found = _references_in(given.values())
        read = list(dict.fromkeys(each.name for each in found if each.kind == "steps"))
        inputs = {each.name for each in found if each.kind == "inputs"}
        entrant = bool(inputs & self.entrant) or any(_READS[self.classes[s]][0] for s in read)
        repeated = bool(inputs & self.repeated) or any(_READS[self.classes[s]][1] for s in read)
        kind = next(name for name, flags in _READS.items() if flags == (entrant, repeated))
        self.classes[step_id] = kind
        self.reads[step_id] = read
        self.raising[step_id] = self._raising(resolved, given)
        runs_entrant = any(
            program.controlled_by & self.entrant
            for program in (resolved.program, *resolved.programs.values())
        )
        self_reported = self._self_reported(resolved)
        declaration = resolved.declaration
        assert declaration.limits is not None
        self.once_ms[step_id] = declaration.limits.once_time_ms or 0
        for run in self.task.runs if kind not in ONCE else [None]:
            values: dict[str, Any] = {}
            hidden: list[dict[str, Any]] = []
            beside: dict[str, Any] | None = None
            for name, raw in given.items():
                port = declaration.inputs[name]
                value, carried, open_ = self._bind(raw, port, run)
                if value is _ABSENT:
                    continue
                values[name] = value
                hidden.extend(each for each in carried if each not in hidden)
                beside = beside or (open_[0] if open_ else None)
            if self_reported:
                values[SELF_REPORTED] = {"value": True}
            tainted: dict[str, Any] | None = None
            if runs_entrant and beside is not None:
                tainted = {"beside": beside}
            else:
                tainted = next(
                    ({"reads": each} for each in read if self._of(each, run).tainted), None
                )
            bound = _Bound(values, hidden, tainted)
            bound.limits, bound.copies = self._item_limits(resolved, values)
            self.bound[(step_id, run.id if run is not None else None)] = bound
        self.output_mb[step_id] = max(
            bound.limits["output_mb"] for (each, _), bound in self.bound.items() if each == step_id
        )
        if self._entrant_source(given) and COMPILE_ERROR in self._once_outcomes(step_id):
            leaked = self._leaked(step_id, resolved.use)
            if leaked is False:
                self.compiling.add(step_id)
            else:
                self.leaking[step_id] = leaked

    def _leaked(self, step_id: str, use: str) -> str | Literal[False] | None:
        """The run of an item of `step_id` whose compile error could say what
        a hidden file holds, None for a step done once; False when none can.
        A step that runs the entrant's code beside a hidden file is one,
        unless it is `EXEC_USE`, whose compile box sees only the source; one
        that reads a tainted step's output always is.
        """
        for (each, run), bound in self.bound.items():
            tainted = bound.tainted
            if each == step_id and tainted is not None and ("reads" in tainted or use != EXEC_USE):
                return run
        return False

    def _of(self, step_id: str, run: Any) -> _Bound:
        """The run of `step_id` that `run` reads: its own, or the one run of a
        step done once.
        """
        if self.classes[step_id] in ONCE or run is None:
            return self.bound[(step_id, None)]
        return self.bound[(step_id, run.id)]

    def _bind(
        self, raw: object, port: Port, run: Any
    ) -> tuple[object, list[dict[str, Any]], list[dict[str, Any]]]:
        """A port's value in `run`, `_ABSENT` for an optional input the task
        leaves out; the hidden files it carries; and those of them it gives on
        a port not marked secret.
        """
        if port.type is Type.PROGRAMS:
            assert isinstance(raw, dict)
            programs: dict[str, dict[str, Any]] = {}
            hidden: list[dict[str, Any]] = []
            open_: list[dict[str, Any]] = []
            inner = port.inputs or {}
            for name, values in raw.items():
                assert isinstance(values, dict)
                bound: dict[str, Any] = {}
                for key, each in values.items():
                    value, carried, given_open = self._bind(each, inner[key], run)
                    if value is _ABSENT:
                        continue
                    bound[key] = value
                    hidden += carried
                    open_ += given_open
                programs[str(name)] = bound
            return {"programs": programs}, hidden, open_
        if port.type is Type.LINKS:
            assert isinstance(raw, list)
            return {"links": list(raw)}, [], []
        if port.type is Type.NAMED_FILES:
            assert isinstance(raw, dict)
            files: dict[str, Any] = {}
            hidden = []
            for name, each in raw.items():
                value, carried = self._plain(each, port, run)
                files[str(name)] = value
                hidden += carried
            return {"files": files}, hidden, [] if port.secret else hidden
        value, hidden = self._plain(raw, port, run)
        return value, hidden, [] if port.secret else hidden

    def _plain(self, raw: object, port: Port, run: Any) -> tuple[object, list[dict[str, Any]]]:
        if isinstance(raw, bool) or is_number(raw):
            return {"value": scalar_to_text(raw) if port.type is Type.TEXT else raw}, []
        assert isinstance(raw, str)
        found = whole_reference(raw, flat=True)
        if found is None:
            return self._written(raw, run), []
        if found.kind == "workflow":
            value: dict[str, Any] = {"workflow": found.name}
            if found.pin:
                value["pin"] = found.pin
            return value, [value]
        if found.kind == "steps":
            assert found.port is not None
            return {"step": found.name, "output": found.port}, list(
                self._of(found.name, run).hidden
            )
        name = found.name
        declared = self.workflow.inputs[name]
        if name in self.entrant:
            entrant = self._entrant(name, run)
            if port.type is Type.TEXT and declared.type is not Type.TEXT:
                return {"template": "{0}", "parts": [entrant]}, []
            return entrant, []
        given = self._given(name, run)
        if given is _ABSENT:
            return _ABSENT, []
        if declared.type in FILES:
            assert isinstance(given, str)
            value = {"task": given}
            self.given_by.setdefault(given, name)
            return value, [value] if self._hidden(given) else []
        return {"value": scalar_to_text(given) if port.type is Type.TEXT else given}, []

    def _written(self, raw: str, run: Any) -> dict[str, Any]:
        """Text with references written into it: the task's scalars written
        in now, the entrant's as template parts.
        """
        text = ""
        parts: list[dict[str, Any]] = []
        last = 0
        for match, found in references(raw, flat=True):
            text += escaped(raw[last : match.start()])
            last = match.end()
            if found.name in self.entrant:
                text += f"{{{len(parts)}}}"
                parts.append(self._entrant(found.name, run))
            else:
                given = self._given(found.name, run)
                text += escaped(scalar_to_text(given) if given is not _ABSENT else "")
        text += escaped(raw[last:])
        if not parts:
            return {"value": text.replace("{{", "{").replace("}}", "}")}
        return {"template": text, "parts": parts}

    def _entrant(self, name: str, run: Any) -> dict[str, Any]:
        part = run.key.get(name) if run is not None else None
        if isinstance(part, Part):
            return {"entrant": name, "part": part.number}
        return {"entrant": name}

    def _given(self, name: str, run: Any) -> object:
        """What the task fills an input with in `run`: its repeated value or
        its setting, or `_ABSENT` for an optional input left out.
        """
        if run is not None and name in run.key:
            return run.key[name]
        if name in self.task.settings:
            given = self.task.settings[name]
            assert isinstance(given, str | bool) or is_number(given), (
                "a secret is refused at the save until org secrets exist"
            )
            return given
        return _ABSENT

    def _hidden(self, path: str) -> bool:
        """Whether entrants are not shown a task file during the contest."""
        return file_time(self.scoring, path) not in SHOWN_DURING

    def _self_reported(self, resolved: ResolvedStep) -> bool:
        """Whether the entrant's code reaches the step's own program given a
        manifest, where the primitive takes `self_reported` to say so.
        """
        program = resolved.program
        return (
            SELF_REPORTED in resolved.declaration.inputs
            and program.manifest is not None
            and bool(program.controlled_by & self.entrant)
        )

    def _raising(self, resolved: ResolvedStep, given: Mapping[str, Any]) -> list[str]:
        """The task's inputs a step's time is raised from."""
        found: list[str] = []
        programs = given.get(PROGRAMS)
        for name, source in resolved.declaration.limits_from.items():
            if name not in BATCH_SCALED:
                continue
            if isinstance(source, FromInput):
                raws = [given.get(source.input)]
            elif isinstance(source, FromSum | FromMax) and isinstance(programs, dict):
                key = source.program_input
                raws = [each.get(key) for each in programs.values() if isinstance(each, dict)]
            else:
                raws = []
            for each in _references_in(raws):
                if each.kind == "inputs" and each.name not in self.entrant:
                    found.append(each.name)
        return list(dict.fromkeys(found))

    # Limits

    def _item_limits(
        self, resolved: ResolvedStep, inputs: Mapping[str, Any]
    ) -> tuple[dict[str, int], list[_Copy]]:
        """One run's limits, raised from its numbers, and for a boxed
        primitive the files it copies into its boxes, its memory what its
        declaration gives, its boxes' memory limits and the room it adds,
        with those files on top, and at least its boxes' memory limits,
        those files and `PRIMITIVE_ROOM_MB`. A source is compiled in a box
        of its own, before any run, and the program it makes is what a run
        box takes, at most `PROGRAM_MB`. The primitive caps its step at
        this figure, handed to it in the step's job, and does not work it
        out again.
        """
        declaration = resolved.declaration
        numbers = {
            name: value["value"]
            for name, value in inputs.items()
            if isinstance(value, dict) and set(value) == {"value"} and _number(value["value"])
        }
        held = inputs.get(PROGRAMS)
        given: dict[str, dict[str, Any]] = held["programs"] if isinstance(held, dict) else {}
        programs = {
            name: {
                key: value["value"]
                for key, value in values.items()
                if set(value) == {"value"} and _number(value["value"])
            }
            for name, values in given.items()
        }
        limits = declaration.item_limits(numbers, programs)
        copied = BOXED.get(resolved.use.partition("@")[0])
        copies: list[_Copy] = []
        if copied is not None:
            copies = [
                copy
                for values in (inputs, *given.values())
                for port in copied
                for copy in (
                    [_Copy(PROGRAM_MB * 1024, "the compiled program", None)]
                    if port == SOURCE and port in values
                    else self._copies(values.get(port))
                )
            ]
            kb = sum(copy.kb for copy in copies)
            boxes_mb = sum(
                each[BOX_MEMORY]
                for each in (numbers, *programs.values())
                if _number(each.get(BOX_MEMORY))
            )
            least = math.ceil(boxes_mb + kb / 1024) + PRIMITIVE_ROOM_MB
            limits["memory_mb"] = max(limits["memory_mb"] + math.ceil(kb / 1024), least)
        return limits, copies

    def _copies(self, value: object) -> list[_Copy]:
        """The files a value copies into a box, each to whole pages: a task
        or workflow file by its size, an entrant's by the most its input
        may hold, and an earlier step's by the most that step may write.
        """
        if not isinstance(value, dict):
            return []
        if "files" in value:
            return [copy for each in value["files"].values() for copy in self._copies(each)]
        if "task" in value:
            given = self.given_by.get(value["task"])
            return [
                _Copy(_pages(self.task_files[path].size), f"`{path}`", given)
                for path in sorted(named_by(self.task_files, value["task"]))
            ]
        if "workflow" in value:
            pin = value.get("pin", 0)
            files = self.inner_files[pin] if pin else self.workflow_files
            return [
                _Copy(_pages(files[path].size), f"the workflow's `{path}`", None)
                for path in sorted(named_by(files, value["workflow"]))
            ]
        if "entrant" in value:
            name = value["entrant"]
            part = value["part"] if isinstance(value.get("part"), int) else None
            return [_Copy(_pages(self._max_size(name, part)), f"the entrant's {name}", name)]
        if "step" in value:
            step = value["step"]
            return [_Copy(self.output_mb[step] * 1024, f"what {step} writes", None)]
        return []

    def _max_size(self, name: str, part: int | None = None) -> int:
        """The most a part of an entrant file may hold. A part with its own
        size uses that; otherwise the input's form, otherwise the default.
        """
        entrant = self.task.entrant[name]
        if entrant.type not in FILES:
            return 0
        if (
            part is not None
            and entrant.part_details is not None
            and 1 <= part <= len(entrant.part_details)
        ):
            return entrant.part_details[part - 1][1]
        return entrant.form.max_size or DEFAULT_MAX_SIZE

    def _largest(self, name: str) -> int:
        """The most any part of the input may hold, which is what an old
        reader of `max_size` still sees.
        """
        entrant = self.task.entrant[name]
        if entrant.type not in FILES:
            return 0
        if entrant.part_details:
            return max(size for _, size in entrant.part_details)
        return entrant.form.max_size or DEFAULT_MAX_SIZE

    def _part_details(self, name: str) -> tuple[PartDetails, ...] | None:
        entrant = self.task.entrant[name]
        if not entrant.part_details:
            return None
        return tuple(
            PartDetails(title=title, max_size=size) for title, size in entrant.part_details
        )

    # The steps the planner adds

    def builds(self, exec_: PrimitiveDeclaration) -> list[Problem]:
        """A `build-<step>` step at the save for each `EXEC_USE` step whose
        source is the task's own: given only the workflow's files, the
        task's settings and words written in, never the entrant's nor a
        repeated input, so the same for every grading. It compiles that
        source with the `build` form of `exec_` and runs nothing, so a
        source that does not compile refuses the publication before it
        takes over. A programs-form step's own program of the task's
        is `build-<step>-<program>`. A workflow step of that name is refused.
        A step done at the save is not built: its own run refuses the save.
        """
        problems: list[Problem] = []
        for step in self.workflow.steps:
            if self.checked.steps[step.id].use != EXEC_USE or self.classes[step.id] in (
                "save_once",
                "save_per_run",
            ):
                continue
            given = step.with_.get(PROGRAMS)
            sources = (
                {f"build-{step.id}-{name}": values for name, values in given.items()}
                if isinstance(given, dict)
                else {f"build-{step.id}": step.with_}
            )
            for build, values in sources.items():
                if not self._own_source(values):
                    continue
                if build in self.classes or build in self.program_steps:
                    taken = (
                        f"The workflow has a step called {build}"
                        if build in self.classes
                        else f"Another source of the workflow's is checked as {build}"
                    )
                    problems.append(
                        Problem(
                            path="workflow",
                            message=f"{taken}, the name the check that {step.id}'s source "
                            "compiles takes. Rename that step.",
                        )
                    )
                    continue
                ports = self.checked.steps[step.id].declaration.inputs
                if isinstance(given, dict):
                    ports = ports[PROGRAMS].inputs or {}
                inputs: dict[str, Any] = {}
                for key in BUILT:
                    if key in values:
                        value, _ = self._plain(values[key], ports[key], None)
                        if value is not _ABSENT:
                            inputs[key] = value
                if SOURCE in inputs:
                    self._build(build, exec_, inputs, None)
        return problems

    def _entrant_source(self, given: Mapping[str, Any]) -> bool:
        """Whether a `source` the step gives, its own or a program's of its
        programs port, reads an entrant input, directly or through an
        earlier step that does: the code a compile error of the step is the
        entrant's.
        """
        programs = given.get(PROGRAMS)
        held = programs.values() if isinstance(programs, dict) else ()
        sources = [
            given.get(SOURCE),
            *(each.get(SOURCE) for each in held if isinstance(each, dict)),
        ]
        return any(
            (each.kind == "inputs" and each.name in self.entrant)
            or (each.kind == "steps" and _READS[self.classes[each.name]][0])
            for each in _references_in(sources)
        )

    def once_problems(self) -> list[Problem]:
        """A step whose declaration gives an output or an outcome the same
        for every item of a batch, given a `source`, `language` or `entry`,
        its own or a program's, that differs by run: its batch would build
        programs that differ, and tell the first one's for all of them.
        """
        problems: list[Problem] = []
        for step in self.workflow.steps:
            outputs = self.checked.steps[step.id].outputs
            if not self._once_outcomes(step.id) and not any(
                port.once is True for port in outputs.values()
            ):
                continue
            programs = step.with_.get(PROGRAMS)
            held = (
                [each for each in programs.values() if isinstance(each, dict)]
                if isinstance(programs, dict)
                else []
            )
            built = [given.get(key) for given in (step.with_, *held) for key in BUILT]
            if any(
                (each.kind == "inputs" and each.name in self.repeated)
                or (each.kind == "steps" and _READS[self.classes[each.name]][1])
                for each in _references_in(built)
            ):
                problems.append(
                    Problem(
                        path="workflow",
                        message=f"{step.id} is given a source, language or entry that differs by "
                        "run, but tells its compile once for every run. Give it one the same for "
                        "every run.",
                    )
                )
        return problems

    def _own_source(self, given: Mapping[str, Any]) -> bool:
        """Whether a program's `source`, `language` and `entry`, as the
        workflow gives them, are the task's own: they read no step, no
        entrant input and no repeated input."""
        found = _references_in([given.get(key) for key in BUILT])
        return SOURCE in given and all(
            each.kind == "workflow"
            or (each.kind == "inputs" and each.name not in self.entrant | self.repeated)
            for each in found
        )

    def _build(
        self, step: str, exec_: PrimitiveDeclaration, inputs: dict[str, Any], value: str | None
    ) -> None:
        """The step `step`, done once at the save, compiling `inputs` with
        the `build` form of `exec_`, for the value `value` when it is that
        value's program's."""
        stepped = exec_.for_step(tuple(inputs), EXEC_USE)
        assert isinstance(stepped, PrimitiveDeclaration) and stepped.limits is not None
        self.bound[(step, None)] = _Bound(inputs, [], None, stepped.item_limits({}))
        self.raising[step] = []
        self.once_ms[step] = stepped.limits.once_time_ms or 0
        self.program_steps[step] = RunStep.model_validate(
            {
                "class": "save_once",
                "primitive": EXEC_USE,
                "image": stepped.image,
                "network": stepped.network,
                "cache": stepped.cache,
                "boxes": 1,
                "outputs": _declared(stepped),
                "folders": (SOURCE,),
                **({"value": {"name": value}} if value is not None else {}),
                "inputs": inputs,
            }
        )

    def programs(self, exec_: PrimitiveDeclaration) -> list[Problem]:
        """The two steps of each value's `exec` program: `build-<value>`,
        compiling the folder at the save, and `exec-<value>`, compiling it
        again, from the machine's cache when it can, and running it on the
        JSON file of its group's runs the forge builds (`RUNS_FILE`), its
        manifest the folder's `program.yaml`, under its limits, with room
        for what it copies into its box. A workflow step of either name is
        refused.
        """
        problems: list[Problem] = []
        for name, program in self.task.programs.items():
            ids = self.task.groups[program.group]
            taken = [
                step
                for step in (f"build-{name}", f"exec-{name}")
                if step in self.classes or step in self.program_steps
            ]
            if taken:
                has = (
                    f"The workflow has a step called {taken[0]}"
                    if taken[0] in self.classes
                    else f"The check that a workflow step's source compiles is called {taken[0]}"
                )
                problems.append(
                    Problem(
                        path=path_text(("scoring", "values", name)),
                        message=f"{has}, the name {name}'s program step takes. Rename the value "
                        "or the step.",
                    )
                )
                continue
            source = {SOURCE: {"task": program.folder}, "language": {"value": program.language}}
            self._build(f"build-{name}", exec_, source, name)
            inputs: dict[str, Any] = {
                **source,
                "files": {"files": {RUNS_FILE: {"runs": list(ids)}}},
                "manifest": {"task": program.manifest},
                "time_limit": {"value": program.time_limit},
                "memory_limit": {"value": program.memory_limit},
            }
            run = exec_.for_step(tuple(inputs), EXEC_USE)
            assert isinstance(run, PrimitiveDeclaration) and run.limits is not None
            copies = [
                _Copy(PROGRAM_MB * 1024, f"the program of {name}", None),
                _Copy(_pages(self.task_files[program.manifest].size), program.manifest, None),
                _Copy(RUN_ENTRY_KB * len(ids), f"the runs {name} reads", None),
            ]
            limits = run.item_limits(
                {"time_limit": program.time_limit, "memory_limit": program.memory_limit}
            )
            kb = sum(copy.kb for copy in copies)
            least = (
                math.ceil(Decimal(program.memory_limit) + Decimal(kb) / 1024) + PRIMITIVE_ROOM_MB
            )
            limits["memory_mb"] = max(limits["memory_mb"] + math.ceil(kb / 1024), least)
            self.bound[(f"exec-{name}", None)] = _Bound(inputs, [], None, limits, copies)
            self.raising[f"exec-{name}"] = []
            self.once_ms[f"exec-{name}"] = run.limits.once_time_ms or 0
            self.program_steps[f"exec-{name}"] = RunStep.model_validate(
                {
                    "class": "grading_once",
                    "primitive": EXEC_USE,
                    "image": run.image,
                    "network": run.network,
                    "cache": run.cache,
                    "boxes": 1,
                    "outputs": {**_declared(run), program.report: Type.NUMBER.value},
                    "folders": (SOURCE,),
                    "value": {"name": name, "report": program.report},
                    "inputs": inputs,
                }
            )
        return problems

    def _program_pipelines(
        self, found: dict[PipelineKind, tuple[PipelineStep, ...]], *, released: bool
    ) -> dict[PipelineKind, tuple[PipelineStep, ...]]:
        """`found` with the steps the planner adds after the rest: each
        build at the save, and each value's program's run in the early
        pipeline when its value is early (`RunTask.early_programs`), else
        the late one, each waiting for every step before it, so the runs a
        program reads are done.
        """
        early = self.task.early_programs(released=released)
        held = dict(found)
        for step, made in self.program_steps.items():
            kind: PipelineKind = "save"
            if made.value is not None and made.value.report is not None:
                kind = "early" if made.value.name in early else "late"
            before = held.get(kind, ())
            waited = {waits for each in before for waits in each.depends_on}
            last = tuple(each.name for each in before if each.name not in waited)
            placed = PipelineStep(
                name=step, step=step, depends_on=last, limits=self._limits(step, None)
            )
            held[kind] = (*before, placed)
        return {kind: held[kind] for kind in PIPELINES if kind in held}

    # Pipelines

    def pipelines(self, *, released: bool = False) -> dict[PipelineKind, tuple[PipelineStep, ...]]:
        """The pipelines of a grading made before any release, or with
        `released`, of one made while the task's results are released."""
        early = self.task.early(released=released)
        early_runs = tuple(run.id for run in self.task.runs if run.id in early)
        late_runs = tuple(run.id for run in self.task.runs if run.id not in early)
        early_once = self._early_once(bool(early_runs), released=released)
        held: dict[PipelineKind, list[tuple[str, tuple[str, ...] | None]]] = {
            kind: [] for kind in PIPELINES
        }
        every = tuple(run.id for run in self.task.runs)
        for step in self.workflow.steps:
            placed = self.classes[step.id]
            if placed == "save_once":
                held["save"].append((step.id, None))
            elif placed == "save_per_run":
                held["save"].append((step.id, every))
            elif placed == "grading_once":
                held["early" if step.id in early_once else "late"].append((step.id, None))
            else:
                if early_runs:
                    held["early"].append((step.id, early_runs))
                if late_runs:
                    held["late"].append((step.id, late_runs))
        found: dict[PipelineKind, tuple[PipelineStep, ...]] = {}
        for kind, steps in held.items():
            made = self._chained(self._pipeline(steps))
            if made:
                found[kind] = made
        return self._program_pipelines(found, released=released)

    def whole(self) -> tuple[PipelineStep, ...]:
        """Every step graded per submission laid out as one pipeline over
        every run, each value's program after the rest: a check's grading,
        done in the save's pipeline (`_check_copies`)."""
        every = tuple(run.id for run in self.task.runs)
        held: list[tuple[str, tuple[str, ...] | None]] = []
        for step in self.workflow.steps:
            placed = self.classes[step.id]
            if placed == "grading_once":
                held.append((step.id, None))
            elif placed == "grading_per_run":
                held.append((step.id, every))
        made = self._chained(self._pipeline(held))
        for name in self.task.programs:
            program = f"exec-{name}"
            if program in self.program_steps:
                made = (
                    *made,
                    PipelineStep(
                        name=program,
                        step=program,
                        depends_on=_last(made),
                        limits=self._limits(program, None),
                    ),
                )
        return made

    def _early_once(self, any_early: bool, *, released: bool = False) -> set[str]:
        """The steps done once per grading in the early pipeline: every one
        when a run is early, since every run's verdict reads each; when none
        is but `compiled`, or a value reading it, is shown `now`, those
        done once per grading that compile the entrant's code, which
        `compiled` reads, and the steps done once per grading they read; a
        step that compiles it only with the runs is refused then
        (`refusals`); otherwise none.
        """
        once = [step.id for step in self.workflow.steps if self.classes[step.id] == "grading_once"]
        if any_early:
            return set(once)
        if not self.task.compile_early(released=released):
            return set()
        found: set[str] = set()
        waiting = [step for step in self._compilers() if self.classes[step] == "grading_once"]
        while waiting:
            step = waiting.pop()
            if step in found:
                continue
            found.add(step)
            waiting += [each for each in self.reads[step] if self.classes[each] == "grading_once"]
        return found

    def _outcomes(self, step_id: str) -> set[str]:
        outcome = self.checked.steps[step_id].declaration.outputs.get("outcome")
        return set(outcome.outcomes or {}) if outcome is not None else set()

    def _once_outcomes(self, step_id: str) -> tuple[str, ...]:
        """The outcomes the step's declaration says it gives the same for
        every item of a batch."""
        outcome = self.checked.steps[step_id].declaration.outputs.get("outcome")
        return outcome.once_outcomes if outcome is not None else ()

    def _pipeline(self, held: list[tuple[str, tuple[str, ...] | None]]) -> tuple[PipelineStep, ...]:
        """The Woodpecker steps of one pipeline from the workflow steps it
        holds, each with the runs it holds of it.
        """
        names: dict[str, dict[str | None, str]] = {}
        """Each workflow step's Woodpecker step by the run it holds, none
        for one holding every run of the step it holds."""
        made: list[PipelineStep] = []
        for step_id, runs in held:
            declaration = self.checked.steps[step_id].declaration
            if runs is None or declaration.batch:
                groups: list[tuple[str, tuple[str, ...] | None]] = [(step_id, runs)]
                names[step_id] = {None: step_id}
            else:
                groups = [(f"{step_id}.{run}", (run,)) for run in runs]
                names[step_id] = {run: f"{step_id}.{run}" for run in runs}
            for name, group in groups:
                depends: list[str] = []
                for read in self.reads[step_id]:
                    held_names = names.get(read)
                    if held_names is None:
                        continue  # in an earlier pipeline
                    if None in held_names:
                        depends.append(held_names[None])
                    else:
                        depends += [held_names[run] for run in group or () if run in held_names]
                made.append(
                    PipelineStep(
                        name=name,
                        step=step_id,
                        runs=group,
                        depends_on=tuple(dict.fromkeys(depends)),
                        limits=self._limits(step_id, group),
                    )
                )
        return tuple(made)

    def _chained(self, steps: tuple[PipelineStep, ...]) -> tuple[PipelineStep, ...]:
        """The pipeline in stages for the smallest slot that holds its
        largest step: a stage takes the next step while their memory adds up
        within the slot, their boxes, one for an `exec` step and one for each
        program of an `exec` programs-form step, number no more than the slot's cores,
        and both run in boxes, and each step of a stage waits for every step
        of the stage before that it does not wait for already. A step that
        runs no boxes, whose container may use the whole slot, is a stage
        alone, and so is one with more boxes than the slot has cores. With
        no slot, as without one that holds the largest step, which `fit`
        refuses, it is as it was.
        """
        largest = max((step.limits.memory_mb for step in steps), default=0)
        slot = next((each for each in self.machine.slots if each.memory_mb >= largest), None)
        if slot is None:
            return steps
        stages: list[list[PipelineStep]] = []
        held = boxes = 0
        open_ = False
        for step in steps:
            resolved = self.checked.steps[step.step]
            boxed = resolved.use.partition("@")[0] in BOXED
            memory = step.limits.memory_mb
            given = len(resolved.programs) or 1
            if open_ and boxed and held + memory <= slot.memory_mb and boxes + given <= slot.cores:
                stages[-1].append(step)
                held += memory
                boxes += given
            else:
                stages.append([step])
                held, boxes = memory, given
            open_ = boxed
        waits: dict[str, set[str]] = {}
        made: list[PipelineStep] = []
        for number, stage in enumerate(stages):
            for step in stage:
                found = {each for name in step.depends_on for each in (name, *waits.get(name, ()))}
                added = [
                    each.name
                    for each in (stages[number - 1] if number else ())
                    if each.name not in found
                ]
                waits[step.name] = found | set(added)
                made.append(step.model_copy(update={"depends_on": (*step.depends_on, *added)}))
        return tuple(made)

    def _limits(self, step_id: str, runs: tuple[str, ...] | None) -> StepLimits:
        """A container's limits over the runs it holds: time summed, with
        what the step takes once before them added once, the rest the most
        of any run.
        """
        items = [self.bound[(step_id, run)].limits for run in (runs or (None,))]
        limits = {
            name: sum(item[name] for item in items)
            if name in BATCH_SCALED
            else max(item[name] for item in items)
            for name in LIMIT_NAMES
        }
        limits["time_ms"] += self.once_ms.get(step_id, 0)
        return StepLimits(**limits)

    def fit(self, pipelines: Mapping[PipelineKind, Sequence[PipelineStep]]) -> list[Problem]:
        """Each pipeline within the run ceiling, refused at what raises its
        longest step's time, and each step's memory, with the files it
        copies, on the machine, refused at what gives its largest file.
        """
        problems: list[Problem] = []
        ceiling = WALL_CEILING.total_seconds()
        for kind, steps in pipelines.items():
            total = wall_of([step.limits.time_ms for step in steps])
            if total <= ceiling:
                continue
            longest = max(steps, key=lambda step: step.limits.time_ms)
            raised = [name for step in (longest, *steps) for name in self.raising[step.step]]
            problems.append(
                Problem(
                    path=path_text(self._where(raised[0])) if raised else "workflow",
                    message=f"{PIPELINE_WORDS[kind]} take {math.ceil(total / 60)} minutes, but a "
                    f"pipeline may take at most {int(ceiling // 60)}. The longest, "
                    f"{longest.name}, takes {math.ceil(longest.limits.time_ms / 60_000)}.",
                )
            )
        told: set[str] = set()
        for steps in pipelines.values():
            for step in steps:
                wanted = step.limits.memory_mb
                if wanted <= self.machine.memory_mb or step.step in told:
                    continue
                told.add(step.step)
                copies = [
                    copy
                    for run in step.runs or (None,)
                    for copy in self.bound[(step.step, run)].copies
                ]
                largest = max(copies, key=lambda copy: copy.kb, default=None)
                such = (
                    f", the largest {largest.words} at {math.ceil(largest.kb / 1024)} MB,"
                    if largest is not None
                    else ""
                )
                problems.append(
                    Problem(
                        path=path_text(self._where(largest.input))
                        if largest is not None and largest.input is not None
                        else "workflow",
                        message=f"The step {step.step} needs {wanted} MB of memory, with the "
                        f"files it copies in beside its programs{such} but no machine this task "
                        f"may run on has more than {self.machine.memory_mb}.",
                    )
                )
        return problems

    def _where(self, name: str) -> Path:
        """Where in `task.yaml` the task fills an input."""
        if name in self.definition.inputs:
            return ("inputs", name)
        for index, axis in enumerate(self.definition.repeat):
            if name in axis.inputs:
                return ("repeat", index, "inputs", axis.inputs.index(name))
        return ("inputs",)

    # What the save refuses and notes

    def refusals(self) -> list[Problem]:
        """A value or group shown during the contest that reads runs behind
        a tainted step done once per grading, which every run reads (11.4);
        and `compiled`, or a value reading it, shown then when it cannot be
        known before the hidden runs, or tells of a tainted compile.
        """
        tainted = next(
            (
                step.id
                for step in self.workflow.steps
                if self.classes[step.id] == "grading_once"
                and self.bound[(step.id, None)].tainted is not None
            ),
            None,
        )
        problems: list[Problem] = []
        for time, index, entry in self._shown():
            if tainted is not None and self._runs_read(entry):
                chain, beside = self._chain(tainted, None)
                why = "every run reads " + ", which reads ".join(f"`{step}`" for step in chain)
                why += f", which runs the entrant's code beside {_words(beside)}"
                why += _UNSHOWN.format(entry=entry)
            elif self.task.reads_compiled(entry):
                why = self._compiled_problem(entry, released=time == "on_release")
            else:
                continue
            if why:
                problems.append(
                    Problem(
                        path=path_text(("scoring", "show", time, index)),
                        message=f"{entry} cannot be shown {SHOWN_WORDS[time]}: {why}. Show "
                        f"{entry} after_close instead.",
                    )
                )
        return problems

    def _compiled_problem(self, entry: str, *, released: bool = False) -> str:
        """Why `compiled`, which `entry` is or reads, cannot be shown during
        the contest, `released` while the task's results are: a step that
        can give a compile error for one run alone runs once per run, so
        it would wait for the hidden runs; no step compiles the entrant's
        code with a compile error the same for every item, so it would wait
        for the whole grading; or every such step compiles it only with the
        runs, and no run is early. None when it can.
        """
        but = "" if entry == COMPILED else f"it reads `{COMPILED}`, but "
        per_run = [
            step.id
            for step in self.workflow.steps
            if self.classes[step.id] == "grading_per_run"
            and COMPILE_ERROR in self._outcomes(step.id)
            and COMPILE_ERROR not in self._once_outcomes(step.id)
        ]
        if per_run:
            return (
                f"{but}`{per_run[0]}`, which can give a compile error, runs once per run, so "
                f"`{COMPILED}` is known only once every run has run, the hidden ones too"
            )
        compilers = self._compilers()
        if not compilers and self.leaking:
            step, run = next(iter(self.leaking.items()))
            chain, beside = self._chain(step, run)
            return (
                ("it comes from " if entry == COMPILED else f"it reads `{COMPILED}`, from ")
                + ", which reads ".join(f"`{each}`" for each in chain)
                + f", which runs the entrant's code beside {_words(beside)}"
                + _UNSHOWN.format(entry=entry)
            )
        if not compilers:
            return (
                f"{but}no step compiles the entrant's code, so `{COMPILED}` is known only once "
                "the whole grading ends, its hidden runs too"
            )
        if self.task.early(released=released) or any(
            self.classes[step] == "grading_once" for step in compilers
        ):
            return ""
        return (
            f"{but}`{compilers[0]}`, which compiles the entrant's code, does so with the runs, "
            f"and no run is graded during the contest, so `{COMPILED}` is known only once the "
            "hidden runs have run"
        )

    def _compilers(self) -> list[str]:
        """The steps that compile the entrant's code and give a compile
        error the same for every item, which `compiled` reads, in the
        workflow's order.
        """
        return [step.id for step in self.workflow.steps if step.id in self.compiling]

    def notes(self) -> list[str]:
        """For each value or group shown during the contest reading runs
        whose own steps are tainted, that the entrant's code can signal a
        little about the hidden input through it (15.4).
        """
        found: list[str] = []
        for _, _, entry in self._shown():
            runs = self._runs_read(entry)
            example = next(
                (
                    (run.id, step.id)
                    for run in self.task.runs
                    if run.id in runs
                    for step in self.workflow.steps
                    if self.classes[step.id] == "grading_per_run"
                    and self.bound[(step.id, run.id)].tainted is not None
                ),
                None,
            )
            if example is None:
                continue
            run_id, step_id = example
            chain, beside = self._chain(step_id, run_id)
            found.append(
                f"{entry} reads runs whose own steps run the entrant's code beside what entrants "
                f"are not shown during the contest, such as `{chain[-1]}` beside "
                f"{_words(beside)} in the run {run_id}, so the entrant's code can signal a "
                f"little about it through what {entry} shows."
            )
        return found

    def _chain(self, step_id: str, run: str | None) -> tuple[list[str], dict[str, Any]]:
        """The steps from a tainted one to the one that runs the entrant's
        code beside a hidden file, and that file.
        """
        chain = [step_id]
        key = run if self.classes[step_id] not in ONCE else None
        tainted = self.bound[(step_id, key)].tainted
        assert tainted is not None
        while "reads" in tainted:
            step_id = tainted["reads"]
            chain.append(step_id)
            key = run if self.classes[step_id] not in ONCE else None
            tainted = self.bound[(step_id, key)].tainted
            assert tainted is not None
        return chain, tainted["beside"]

    def shown_reports(self) -> list[Problem]:
        """A report `show` lists by itself that a step gives for each run,
        which is shown with the runs of the groups `show` lists (stage 5
        section 6 item 30); one a step done once gives, or an output its
        declaration says is the same for every item, is shown by itself. An
        entry naming a group or a value names that.
        """
        names = show_names(self.scoring)
        problems: list[Problem] = []
        for time, entries in self.scoring.show.lists().items():
            for index, entry in enumerate(entries):
                report = self.checked.reports.get(entry)
                if (
                    report is None
                    or entry in names
                    or self.classes[report.step] in ONCE
                    or (report.port is not None and report.port.once is True)
                ):
                    continue
                problems.append(
                    Problem(
                        path=path_text(("scoring", "show", time, index)),
                        message=f"{entry} is reported for each run, so it is shown with the runs "
                        "of the groups show lists. Remove it.",
                    )
                )
        return problems

    def _shown(self) -> Iterable[tuple[str, int, str]]:
        """Each entry of `show` during the contest, with its time and place."""
        lists = self.scoring.show.lists()
        for time in SHOWN_DURING:
            for index, entry in enumerate(lists.get(time, [])):
                yield time, index, entry

    def _runs_read(self, entry: str) -> set[str]:
        if entry in self.task.groups:
            return set(self.task.groups[entry])
        if entry in self.task.values.reads:
            groups = self.task.values.through(entry).groups
            return {run for group in groups for run in self.task.groups.get(group, ())}
        return set()

    # The plan's parts

    def run_steps(self) -> dict[str, RunStep]:
        steps: dict[str, RunStep] = {}
        for step in self.workflow.steps:
            resolved = self.checked.steps[step.id]
            declaration = resolved.declaration
            kind = self.classes[step.id]
            boxed = resolved.use.partition("@")[0]
            common: dict[str, Any] = {
                "class": kind,
                "primitive": resolved.use,
                "image": declaration.image,
                "network": declaration.network,
                "cache": declaration.cache,
                "boxes": (len(resolved.programs) or 1) if boxed in BOXED else None,
                "outputs": {
                    f"{name}?" if port.optional else name: port.type.value
                    for name, port in {
                        **resolved.outputs,
                        **self.task.manifest_outputs.get(step.id, {}),
                    }.items()
                },
                "folders": tuple(
                    sorted(
                        name
                        for name, port in declaration.inputs.items()
                        if port.type is Type.FOLDER and name in step.with_
                    )
                )
                or None,
                "self_reported": True if self._self_reported(resolved) else None,
                "compiled": True if step.id in self.compiling else None,
            }
            if kind in ONCE:
                bound = self.bound[(step.id, None)]
                steps[step.id] = RunStep.model_validate(
                    {**common, "inputs": bound.inputs, "tainted": bound.tainted}
                )
            else:
                common["once_outputs"] = (
                    tuple(name for name, port in resolved.outputs.items() if port.once is True)
                    or None
                )
                common["once_outcomes"] = self._once_outcomes(step.id) or None
                items = {
                    run.id: Item.model_validate(
                        {
                            "inputs": self.bound[(step.id, run.id)].inputs,
                            "tainted": self.bound[(step.id, run.id)].tainted,
                        }
                    )
                    for run in self.task.runs
                }
                steps[step.id] = RunStep.model_validate({**common, "items": items})
        return {**steps, **self.program_steps}

    def entrant_entries(self) -> dict[str, EntrantEntry]:
        return {
            name: EntrantEntry(
                type=each.type,
                options=each.options,
                parts=each.parts,
                max_size=self._largest(name) or None,
                part_details=self._part_details(name),
            )
            for name, each in sorted(self.task.entrant.items())
        }

    def reports(self) -> dict[str, ReportEntry]:
        found: dict[str, ReportEntry] = {}
        for name, report in self.checked.reports.items():
            port = report.port
            resolved = port
            if resolved is None:
                key = f"{report.program}.{report.output}" if report.program else report.output
                resolved = self.task.manifest_outputs.get(report.step, {}).get(key)
            if resolved is not None and resolved.type not in (Type.NUMBER, Type.TEXT):
                continue
            number = resolved is not None and resolved.type is Type.NUMBER
            found[name] = ReportEntry(
                step=report.step,
                output=f"{report.program}.{report.output}" if report.program else report.output,
                better=resolved.better if number and resolved is not None else None,
                at_least=resolved.at_least if number and resolved is not None else None,
                at_most=resolved.at_most if number and resolved is not None else None,
            )
        return found
