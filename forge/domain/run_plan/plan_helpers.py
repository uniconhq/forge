from collections.abc import Iterable, Mapping
from typing import Any

from forge.domain.primitives import PrimitiveDeclaration
from forge.domain.run_plan.records import PAGE_KB
from forge.domain.run_task import Part
from forge.domain.workflow_definition import BadReference, Reference, references
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


def _key_json(key: Mapping[str, object]) -> dict[str, object]:
    return {
        name: {"part": value.number} if isinstance(value, Part) else value
        for name, value in key.items()
    }
