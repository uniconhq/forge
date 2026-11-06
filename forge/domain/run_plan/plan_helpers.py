from collections.abc import Collection, Iterable, Mapping
from typing import Any

from forge.domain.primitives import PrimitiveDeclaration
from forge.domain.run_plan.records import _READS, ONCE, PAGE_KB, StepClass
from forge.domain.workflow_definition import BadReference, Reference, RunStep, references
from forge.domain.yaml_models import is_number


def _declared(declaration: PrimitiveDeclaration) -> dict[str, str]:
    """A primitive's own outputs as a plan's step lists them, a `?` after
    the name of one that may be missing."""
    return {
        f"{name}?" if port.optional else name: port.type.value
        for name, port in declaration.outputs.items()
    }


def _references_in(values: Iterable[object]) -> list[Reference]:
    """Every reference in `values`, inside mappings and lists too."""
    found: list[Reference] = []
    for value in values:
        if isinstance(value, str):
            try:
                found.extend(reference for _, reference in references(value, flat=True))
            except BadReference:
                continue
        elif isinstance(value, dict):
            found.extend(_references_in(value.values()))
        elif isinstance(value, list):
            found.extend(_references_in(value))
    return found


def step_class(
    given: Mapping[str, Any],
    entrant: Collection[str],
    repeated: Collection[str],
    classes: Mapping[str, StepClass],
) -> tuple[StepClass, list[str]]:
    """The class of a step given `given`, and the steps it reads directly,
    in the order it names them: whether it reads one of the `entrant`'s
    inputs and one of the inputs `repeated` whose value differs per run, the
    entrant's given in parts among them, directly or through a step it
    reads, of `classes` the steps before it, as the planner classes each.
    """
    found = _references_in(given.values())
    read = list(dict.fromkeys(each.name for each in found if each.kind == "steps"))
    inputs = {each.name for each in found if each.kind == "inputs"}
    reads_entrant = bool(inputs & set(entrant)) or any(_READS[classes[s]][0] for s in read)
    reads_repeated = bool(inputs & set(repeated)) or any(_READS[classes[s]][1] for s in read)
    kind = next(name for name, flags in _READS.items() if flags == (reads_entrant, reads_repeated))
    return kind, read


def once_steps(steps: Iterable[RunStep], repeated: Collection[str]) -> set[str]:
    """The ids of the workflow's `steps`, in their order, done once for
    every run rather than for each, as the planner classes them for a task
    whose inputs `repeated` differ per run (`step_class`)."""
    classes: dict[str, StepClass] = {}
    for step in steps:
        classes[step.id] = step_class(step.with_, (), repeated, classes)[0]
    return {name for name, kind in classes.items() if kind in ONCE}


def _leaves(values: Iterable[object]) -> Iterable[dict[str, Any]]:
    """Every plain value among `values`: inside named files, programs and
    templates too.
    """
    for value in values:
        if not isinstance(value, dict):
            continue
        if "files" in value:
            yield from _leaves(value["files"].values())
        elif "programs" in value:
            for inputs in value["programs"].values():
                yield from _leaves(inputs.values())
        elif "template" in value:
            yield from _leaves(value["parts"])
        else:
            yield value


def _pages(size: int) -> int:
    return PAGE_KB * -(-size // (PAGE_KB * 1024))


def _number(value: object) -> bool:
    return is_number(value) and not isinstance(value, bool)


def _words(value: Mapping[str, object]) -> str:
    if "workflow" in value:
        return f"the workflow's `{value['workflow']}`"
    return f"`{value['task']}`"
