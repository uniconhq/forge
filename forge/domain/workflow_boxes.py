"""A run workflow that uses another as a step, expanded into one flat list of
primitive steps (SIMPLER-CORE.md 7.4).

A box's `use:` names a run workflow at a version. `flatten` walks every box
depth first and writes one `RunWorkflow` whose steps are the primitive
steps, each named by its path (`build/compile`). A workflow with no box is
left as it was written. The checker then reads that flat workflow, and
`placed` maps a problem back onto the written file: one inside a box lands
on the outermost box, with `step` holding the path.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from forge.domain.port_values import scalar_to_text
from forge.domain.primitives import Port
from forge.domain.run_workflow import RunWorkflowCheck
from forge.domain.workflow_definition import (
    MAX_USES,
    BadReference,
    Reference,
    RunInput,
    RunReport,
    RunStep,
    RunWorkflow,
    references,
    whole_reference,
)
from forge.domain.yaml_models import Problem, path_text

_STEP_PATH = re.compile(r"^steps\[(\d+)\](.*)$")
_DROP = object()


@dataclass(frozen=True, slots=True)
class BoxGiven:
    """One box as the checker reads its `with:`: its path, the written step
    it hangs from, the flat index of its first primitive step, its inner
    inputs as ports, and the `with:` rewritten into the flat workflow.
    """

    path: str
    place: int
    first: int
    use: str
    inputs: dict[str, Port]
    given: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FlatWorkflow:
    """A run workflow expanded into primitive steps. `places` maps each flat
    step's path to the index of the outermost written step it comes from.
    `pins` is the `use:` of pins 1 onwards, in the order each version was
    first met.
    """

    workflow: RunWorkflow
    places: dict[str, int]
    boxes: tuple[BoxGiven, ...]
    pins: tuple[str, ...]


def port_of(given: RunInput) -> Port:
    """The port a box's input is checked as: its type, options and whether
    it may be left out. Who gives it is the task's, and is not part of this."""
    return Port(type=given.type, options=given.options, optional=given.optional)


class _Rewrite:
    """Rewrites one workflow's references into the flat workflow's names."""

    def __init__(
        self,
        workflow: RunWorkflow,
        prefix: str,
        pin: int,
        given: Mapping[str, object] | None,
        inners: Mapping[str, RunWorkflow],
        pins: Mapping[str, int],
        stack: tuple[str, ...] = (),
    ) -> None:
        self.workflow = workflow
        self.prefix = prefix
        self.pin = pin
        self.given = given
        self.inners = inners
        self.pins = pins
        self.stack = stack
        self.problems: list[Problem] = []

    def step_path(self, step_id: str) -> str:
        return f"{self.prefix}/{step_id}" if self.prefix else step_id

    def value(self, raw: object) -> object:
        if isinstance(raw, str):
            return self.text(raw)
        if isinstance(raw, dict):
            kept = {}
            for key, item in raw.items():
                rewritten = self.value(item)
                if rewritten is not _DROP:
                    kept[key] = rewritten
            return kept
        if isinstance(raw, list):
            return [item for item in (self.value(each) for each in raw) if item is not _DROP]
        return raw

    def text(self, raw: str) -> object:
        try:
            whole = whole_reference(raw, runs=True)
        except BadReference as bad:
            self._bad(str(bad))
            return raw
        if whole is not None:
            return self.whole(whole, raw)
        try:
            found = references(raw, runs=True)
        except BadReference as bad:
            self._bad(str(bad))
            return raw
        if not found:
            return raw
        parts: list[str] = []
        last = 0
        for match, reference in found:
            parts.append(raw[last : match.start()])
            parts.append(self.in_text(reference, match.group(0)))
            last = match.end()
        parts.append(raw[last:])
        return "".join(parts)

    def whole(self, reference: Reference, raw: str) -> object:
        if reference.kind == "inputs":
            return self._input(reference.name, raw, whole=True)
        if reference.kind == "steps":
            return self._step(reference, raw)
        if reference.kind == "workflow" and self.pin:
            return str(replace(reference, pin=self.pin))
        return raw

    def in_text(self, reference: Reference, raw: str) -> str:
        if reference.kind == "inputs":
            given = self._input(reference.name, raw, whole=False)
            return "" if given is _DROP else str(given)
        if reference.kind == "steps":
            rewritten = self._step(reference, raw)
            return rewritten if isinstance(rewritten, str) else raw
        if reference.kind == "workflow" and self.pin:
            return str(replace(reference, pin=self.pin))
        return raw

    def _input(self, name: str, raw: str, *, whole: bool) -> object:
        if self.given is None:
            return raw
        if name not in self.given:
            return _DROP if whole else ""
        given = self.given[name]
        if whole:
            return given
        if isinstance(given, str):
            try:
                if whole_reference(given, runs=True, flat=True) is not None:
                    return given
            except BadReference:
                pass
        return scalar_to_text(given)

    def _step(self, reference: Reference, raw: str) -> object:
        step_id = reference.name
        use = self._use(step_id)
        if use is not None and use in self.inners:
            if reference.program is not None:
                self._bad(f"A box gives only its outputs, as ${{{{ steps.{step_id}.<output> }}}}.")
                return raw
            named = self.inners[use].outputs.get(reference.output or "")
            if not isinstance(named, str):
                listed = ", ".join(sorted(self.inners[use].outputs)) or "none"
                self._bad(f"{self.step_path(step_id)} gives only the outputs of {use}: {listed}.")
                return raw
            return self._output(use, self.step_path(step_id), named)
        path = self.step_path(step_id)
        if path == step_id:
            return raw
        return str(replace(reference, name=path))

    def _output(self, use: str, box_path: str, named: str) -> str:
        if box_path in self.stack:
            self._bad(f"{box_path}'s outputs refer to themselves.")
            return named
        nested = _Rewrite(
            self.inners[use],
            box_path,
            self.pins[use],
            None,
            self.inners,
            self.pins,
            (*self.stack, box_path),
        )
        rewritten = nested.text(named)
        self.problems.extend(nested.problems)
        return rewritten if isinstance(rewritten, str) else named

    def _use(self, step_id: str) -> str | None:
        for step in self.workflow.steps:
            if step.id == step_id:
                return str(step.use)
        return None

    def _bad(self, message: str) -> None:
        self.problems.append(Problem(path="", message=message))


def flatten(
    outer: RunWorkflow,
    inners: Mapping[str, RunWorkflow],
    pins: Mapping[str, int],
) -> FlatWorkflow | list[Problem]:
    """The flat workflow of `outer`, or the problems that refuse it. `inners`
    maps a box's `use:` to the run workflow it names, and `pins` maps that
    `use:` to its pin, from 1 in the order the versions were first met. A
    workflow with no box is returned as it was written.
    """
    places: dict[str, int]
    if not any(str(step.use) in inners for step in outer.steps):
        places = {step.id: index for index, step in enumerate(outer.steps)}
        return FlatWorkflow(outer, places, (), ())

    problems: list[Problem] = []
    steps: list[RunStep] = []
    places = dict[str, int]()
    boxes: list[BoxGiven] = []

    def walk(
        workflow: RunWorkflow,
        prefix: str,
        pin: int,
        given: Mapping[str, object] | None,
        place: int,
        *,
        own: bool,
    ) -> None:
        for index, step in enumerate(workflow.steps):
            written = index if own else place
            use = str(step.use)
            path = f"{prefix}/{step.id}" if prefix else step.id
            at = path_text(("steps", written, "with"))
            if use in inners:
                refused, raw_given = _box_with(step, inners[use], use, path, written)
                problems.extend(refused)
                if refused:
                    continue
                parent = _Rewrite(workflow, prefix, pin, given, inners, pins)
                prepared: dict[str, object] = {}
                missed = False
                for key, value in raw_given.items():
                    rewritten = parent.value(value)
                    if rewritten is _DROP:
                        if not inners[use].inputs[key].optional:
                            problems.append(
                                Problem(
                                    path=path_text(("steps", written, "with", key)),
                                    message=f"{path} gives no value for {key}, which {use} needs.",
                                )
                            )
                            missed = True
                        continue
                    prepared[key] = rewritten
                problems.extend(_located(parent.problems, at))
                if parent.problems or missed:
                    continue
                boxes.append(
                    BoxGiven(
                        path,
                        written,
                        len(steps),
                        use,
                        {name: port_of(port) for name, port in inners[use].inputs.items()},
                        prepared,
                    )
                )
                walk(inners[use], path, pins[use], prepared, written, own=False)
                continue
            writer = _Rewrite(workflow, prefix, pin, given, inners, pins)
            copied = writer.value(step.with_)
            problems.extend(_located(writer.problems, at))
            if not isinstance(copied, dict):
                continue
            places[path] = written
            if path == step.id and copied == step.with_:
                steps.append(step)
            else:
                steps.append(RunStep.model_construct(id=path, use=step.use, with_=copied))

    walk(outer, "", 0, None, 0, own=True)
    report_writer = _Rewrite(outer, "", 0, None, inners, pins)
    output_writer = _Rewrite(outer, "", 0, None, inners, pins)
    reports = _reports(outer, report_writer)
    outputs = _outputs(outer, output_writer)
    problems.extend(report_writer.problems)
    problems.extend(output_writer.problems)
    if problems:
        return problems
    flat = RunWorkflow.model_construct(
        inputs=outer.inputs,
        steps=tuple(steps),
        reports=reports,
        outputs=outputs,
    )
    order = tuple(use for use, _pin in sorted(pins.items(), key=lambda item: item[1]))
    return FlatWorkflow(flat, places, tuple(boxes), order)


def _box_with(
    step: RunStep, inner: RunWorkflow, use: str, path: str, place: int
) -> tuple[list[Problem], dict[str, object]]:
    """The keys of the box's `with:` against the inner inputs. The values
    are still as the box wrote them; the caller rewrites them."""
    problems: list[Problem] = []
    names = set(inner.inputs)
    for key in step.with_:
        if key not in names:
            listed = ", ".join(sorted(names)) or "none"
            problems.append(
                Problem(
                    path=path_text(("steps", place, "with", key)),
                    message=f"{key} is not an input of {use}, whose inputs are {listed}.",
                )
            )
    given: dict[str, object] = {}
    for name, port in inner.inputs.items():
        if name not in step.with_:
            if not port.optional:
                problems.append(
                    Problem(
                        path=path_text(("steps", place, "with", name)),
                        message=f"{path} gives no value for {name}, which {use} needs.",
                    )
                )
            continue
        given[name] = step.with_[name]
    return problems, given


def _reports(outer: RunWorkflow, writer: _Rewrite) -> dict[str, str | RunReport]:
    reports: dict[str, str | RunReport] = {}
    for name, entry in outer.reports.items():
        raw = entry if isinstance(entry, str) else entry.from_
        start = len(writer.problems)
        rewritten = writer.text(raw)
        _stamp(writer.problems, start, path_text(("reports", name)))
        if not isinstance(rewritten, str):
            continue
        if isinstance(entry, str):
            reports[name] = entry if rewritten == raw else rewritten
        elif rewritten == entry.from_:
            reports[name] = entry
        else:
            reports[name] = entry.model_copy(update={"from_": rewritten})
    return reports


def _outputs(outer: RunWorkflow, writer: _Rewrite) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for name, raw in outer.outputs.items():
        if not isinstance(raw, str):
            continue
        start = len(writer.problems)
        rewritten = writer.text(raw)
        _stamp(writer.problems, start, path_text(("outputs", name)))
        if isinstance(rewritten, str):
            outputs[name] = rewritten
    return outputs


def _stamp(problems: list[Problem], start: int, path: str) -> None:
    for problem in problems[start:]:
        if not problem["path"]:
            problem["path"] = path


def _located(problems: list[Problem], path: str) -> list[Problem]:
    return [
        Problem(path=problem["path"] or path, message=problem["message"]) for problem in problems
    ]


def place(flat: FlatWorkflow, path: str) -> str:
    """The written file's path for a path into the flat workflow. One under
    a step inside a box is the outermost box's step and nothing after it,
    since the box has no `with` of that inner key. A box's own `with:` keeps
    its key. Inputs, reports and `steps` as a whole are unchanged.
    """
    found = _STEP_PATH.match(path)
    if found is None:
        return path
    index = int(found.group(1))
    rest = found.group(2)
    kept = _box_input(flat, index, rest)
    if kept is not None:
        return kept
    if index >= len(flat.workflow.steps):
        return path
    step = flat.workflow.steps[index]
    written = flat.places.get(step.id, index)
    if "/" not in step.id:
        return f"steps[{written}]{rest}"
    return f"steps[{written}]"


def _box_input(flat: FlatWorkflow, index: int, rest: str) -> str | None:
    """The written path of a box's own `with:` key, checked at its first
    flat step. None when `rest` is a key of the step inside the box.
    """
    if not rest.startswith(".with."):
        return None
    key = rest.removeprefix(".with.").split(".", 1)[0]
    for box in flat.boxes:
        if box.first == index and key in box.inputs:
            return f"steps[{box.place}]{rest}"
    return None


def placed(check: RunWorkflowCheck, flat: FlatWorkflow) -> RunWorkflowCheck:
    """`check` with every path mapped back onto the written file. A problem
    at a step inside a box names that step, and its words start with the path.
    """
    return RunWorkflowCheck(
        [_placed_problem(flat, problem) for problem in check.problems],
        check.steps,
        check.reports,
        {
            name: [replace(use, path=place(flat, use.path)) for use in uses]
            for name, uses in check.uses.items()
        },
        [replace(item, path=place(flat, item.path)) for item in check.deferred],
        [replace(item, path=place(flat, item.path)) for item in check.time_limits],
        check.outputs,
    )


def _placed_problem(flat: FlatWorkflow, problem: Problem) -> Problem:
    found = _STEP_PATH.match(problem["path"])
    if found is None:
        return problem
    index = int(found.group(1))
    rest = found.group(2)
    if _box_input(flat, index, rest) is not None:
        return Problem(path=place(flat, problem["path"]), message=problem["message"])
    if index >= len(flat.workflow.steps):
        return problem
    step = flat.workflow.steps[index]
    if "/" not in step.id:
        return Problem(path=place(flat, problem["path"]), message=problem["message"], step=step.id)
    return Problem(
        path=place(flat, problem["path"]),
        message=f"In `{step.id}`: {problem['message']}",
        step=step.id,
    )


def over_cap(count: int) -> Problem | None:
    """The expansion's cap, 32 distinct `use:` references at every level
    together. None when `count` is within it.
    """
    if count <= MAX_USES:
        return None
    return Problem(
        path=path_text(("steps",)),
        message=(
            f"Expanded, this workflow uses {count} different primitive and workflow "
            f"versions, and at most {MAX_USES} are allowed. Use fewer."
        ),
    )
