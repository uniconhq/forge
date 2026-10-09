"""Combining several workflows into one (Task 10.3.2), and writing a
definition back out as `workflow.yaml`.

Each source is inlined whole, so the result needs none of them to be read
again. An input or a test field that two sources declare alike is one input
or field of the result, which is how two ways of grading one submission
share it; one declared differently under a taken id is renamed, and so is
every step and every reported name that is taken, by the first free
`<id>-2`, `<id>-3` (`<name>_2` for a reported name, which takes no hyphen).
Each reference in a renamed source follows its rename. The once steps of
every source come first, in the sources' order, then their per-test steps,
since a once step comes before every per-test step and no source reads
another's.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import yaml

from forge.domain.workflow_definition import (
    Meaning,
    Reference,
    TestField,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowStep,
    references,
    whole_reference,
)

_MAX_ID = 40


def combine_workflows(sources: Sequence[WorkflowDefinition]) -> str:
    """One `workflow.yaml` holding every source's inputs, test fields, steps
    and report, renamed where they would clash.
    """
    inputs: dict[str, WorkflowInput] = {}
    test: dict[str, TestField] = {}
    once: list[dict[str, Any]] = []
    per_test: list[dict[str, Any]] = []
    report: dict[str, str | dict[str, Any]] = {}
    step_ids: set[str] = set()
    for source in sources:
        renamed = {
            "inputs": _merge(source.inputs, inputs, "-"),
            "test": _merge(source.test, test, "-"),
            "steps": {},
        }
        renamed["steps"] = _renames([step.id for step in source.steps], step_ids, "-")
        step_ids.update(renamed["steps"].values())
        rename = _renamer(renamed)
        for step in source.steps:
            (per_test if step.per_test else once).append(
                _step(renamed["steps"][step.id], step, rename)
            )
        names = _renames(list(source.report), set(report), "_")
        for name, entry in source.report.items():
            report[names[name]] = _entry(entry, rename)
    return write_workflow(inputs, test, [*once, *per_test], report)


def _renames(keys: list[str], earlier: set[str], separator: str) -> dict[str, str]:
    """Each of a source's keys as the result holds it: itself when no
    earlier source took it, otherwise the first free one, free of every key
    taken so far and of the source's own others.
    """
    taken = earlier | set(keys)
    renames: dict[str, str] = {}
    for key in keys:
        if key not in earlier:
            renames[key] = key
            continue
        new_key = _free(key, taken, separator)
        taken.add(new_key)
        renames[key] = new_key
    return renames


def _merge[D: (WorkflowInput, TestField)](
    declared: Mapping[str, D], into: dict[str, D], separator: str
) -> dict[str, str]:
    """Add a source's declarations to the result's: one an earlier source
    declared alike under the same id is that one, and one an earlier source
    took otherwise gets a free id. The renames, old id to new.
    """
    earlier = dict(into)
    shared = {key for key, declaration in declared.items() if earlier.get(key) == declaration}
    rest = [key for key in declared if key not in shared]
    renames = _renames(rest, set(earlier), separator)
    for key in rest:
        into[renames[key]] = declared[key]
    return {**{key: key for key in shared}, **renames}


def _free(key: str, taken: set[str], separator: str) -> str:
    """`key` when it is free, otherwise the first free `<key><sep><n>` from
    2 up, the key cut so the whole stays within an id's length.
    """
    if key not in taken:
        return key
    number = 2
    while True:
        tail = f"{separator}{number}"
        candidate = key[: _MAX_ID - len(tail)] + tail
        if candidate not in taken:
            return candidate
        number += 1


def _renamer(renamed: Mapping[str, Mapping[str, str]]) -> Callable[[str], str]:
    """A function rewriting every reference in a text by the renames."""

    def rename(text: str) -> str:
        found = references(text)
        if not found:
            return text
        parts: list[str] = []
        at = 0
        for match, reference in found:
            new_name = renamed[reference.kind].get(reference.name, reference.name)
            parts.append(text[at : match.start()])
            parts.append(str(Reference(reference.kind, new_name, reference.output)))
            at = match.end()
        parts.append(text[at:])
        return "".join(parts)

    return rename


def _step(new_id: str, step: WorkflowStep, rename: Callable[[str], str]) -> dict[str, Any]:
    written: dict[str, Any] = {"id": new_id, "use": str(step.use)}
    if step.per_test:
        written["per_test"] = True
    written["with"] = {
        port: rename(value) if isinstance(value, str) else value
        for port, value in step.with_.items()
    }
    return written


def _entry(entry: str | Meaning, rename: Callable[[str], str]) -> str | dict[str, Any]:
    if isinstance(entry, str):
        return rename(entry)
    written: dict[str, Any] = {"from": rename(entry.from_)}
    if entry.fold is not None:
        written["fold"] = entry.fold.value
    if entry.better is not None:
        written["better"] = rename(entry.better)
    if entry.at_least is not None:
        written["at_least"] = entry.at_least
    if entry.at_most is not None:
        written["at_most"] = entry.at_most
    return written


# Writing a definition


def write_workflow(
    inputs: Mapping[str, WorkflowInput],
    test: Mapping[str, TestField],
    steps: Sequence[Mapping[str, Any]],
    report: Mapping[str, str | Mapping[str, Any]],
) -> str:
    """The `workflow.yaml` of these parts, in the order the format lists its
    keys: each declaration and report entry on one line, a declaration of
    its type alone in the short form, and each step's `with` a port to a
    line.
    """
    lines: list[str] = []
    if inputs:
        lines.append("inputs:")
        lines.extend(f"  {_one_line(key)}: {_declaration(value)}" for key, value in inputs.items())
    lines.append("test:")
    lines.extend(f"  {_one_line(key)}: {_declaration(value)}" for key, value in test.items())
    lines.append("steps:")
    for step in steps:
        lines.append(f"  - id: {_one_line(step['id'])}")
        lines.append(f"    use: {_one_line(str(step['use']))}")
        if step.get("per_test"):
            lines.append("    per_test: true")
        given = step.get("with") or {}
        if not given:
            lines.append("    with: {}")
            continue
        lines.append("    with:")
        lines.extend(f"      {_one_line(port)}: {_scalar(value)}" for port, value in given.items())
    if report:
        lines.append("report:")
        for name, entry in report.items():
            shown = _flow(dict(entry)) if isinstance(entry, Mapping) else _scalar(entry)
            lines.append(f"  {_one_line(name)}: {shown}")
    return "\n".join(lines) + "\n"


def _declaration(declared: WorkflowInput | TestField) -> str:
    """A declaration as one line: its type alone when nothing else is set."""
    fields: dict[str, Any] = {"type": declared.type.value}
    for key, value in declared.model_dump(exclude={"type"}, exclude_defaults=True).items():
        fields[key] = list(value) if isinstance(value, tuple) else value
    return str(fields["type"]) if len(fields) == 1 else _flow(fields)


def _flow(value: Mapping[str, Any]) -> str:
    return _one_line(dict(value), sort_keys=False)


_PLAIN_REFERENCE = re.compile(r"^\$\{\{ [a-z][a-z0-9_.-]* \}\}$")


def _scalar(value: object) -> str:
    """A value as a block mapping holds it. A whole reference is written
    bare, as the format's own examples are; anything else as YAML would.
    """
    if isinstance(value, str) and _PLAIN_REFERENCE.match(value) and whole_reference(value):
        return value
    return _one_line(value)


def _one_line(value: object, **options: Any) -> str:
    """`value` as YAML on one line. Text over several lines would be written
    across lines under the key, which only a lax reader takes, so it is
    written double-quoted with its line breaks as escapes.
    """
    for style in (None, '"'):
        dumped = str(
            yaml.safe_dump(
                value, default_flow_style=True, default_style=style, width=1 << 30, **options
            )
        )
        dumped = dumped.removesuffix("\n").removesuffix("\n...").strip()
        if not any(brk in dumped for brk in _LINE_BREAKS):
            return dumped
    raise AssertionError("a double-quoted scalar holds no line break")


_LINE_BREAKS = ("\n", "\r", "\x85", "\u2028", "\u2029")


__all__ = ["combine_workflows", "write_workflow"]
