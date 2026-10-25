from collections.abc import Collection, Mapping
from typing import Any

from forge.domain.content import has_path, path_problem
from forge.domain.definitions import ENTRANT
from forge.domain.port_values import scalar_to_text
from forge.domain.program_manifest import ProgramManifest, parse_program
from forge.domain.run_task.records import RUN_ID, Part
from forge.domain.run_workflow import Program
from forge.domain.types import Type
from forge.domain.workflow_definition import RunInput
from forge.domain.yaml_models import InvalidDefinition, Path, is_number


def _entrant_form(given: object) -> object:
    """The form details of an input made the entrant's, `{}` for none, or
    none for a setting.
    """
    if given == ENTRANT:
        return {}
    if isinstance(given, dict) and set(given) == {ENTRANT}:
        return {} if given[ENTRANT] is None else given[ENTRANT]
    return None


def _value_problem(wanted: RunInput, given: object, paths: Collection[str]) -> str | None:
    match wanted.type:
        case Type.TEXT:
            return None if isinstance(given, str) else "This must be text."
        case Type.NUMBER:
            return None if is_number(given) else "This must be a number."
        case Type.BOOLEAN:
            return None if isinstance(given, bool) else "This must be true or false."
        case Type.ENUM:
            options = wanted.options or ()
            return None if given in options else f"This must be one of {', '.join(options)}."
        case Type.FILE | Type.FOLDER:
            folder = wanted.type is Type.FOLDER
            problem = path_problem(given, folder=folder)
            if problem is not None:
                return problem
            assert isinstance(given, str)
            if not has_path(paths, given):
                return (
                    f"The task has no files in {given}. Add them, or give another folder."
                    if folder
                    else f"The task has no file {given}. Add it, or give another file."
                )
            return None
    return "This is not a value this input takes."


def _read_manifest(
    path: str, manifests: Mapping[str, bytes], program: Program
) -> ProgramManifest | None:
    text = manifests.get(path)
    if text is None or program.manifest is None:
        return None
    try:
        return parse_program(text, path, own_names=program.manifest.own_names)
    except InvalidDefinition:
        return None


def _cell(index: int, row: int, column: int, name: str, given: object) -> Path:
    """Where one value of a tuple is written."""
    at: Path = ("repeat", index, "tuples", row)
    if isinstance(given, list):
        return (*at, column)
    if isinstance(given, dict):
        return (*at, name)
    return at


def _described(kind: Type) -> str:
    return {
        Type.TEXT: "some text",
        Type.NUMBER: "a number",
        Type.BOOLEAN: "true or false",
        Type.ENUM: "one of its options",
        Type.FILE: "a file of the task",
        Type.FOLDER: "a folder of the task",
    }.get(kind, "a value")


def _value_words(value: object) -> str:
    return str(value) if isinstance(value, Part) else scalar_to_text(value)


def key_json(key: Mapping[str, object]) -> dict[str, object]:
    return {
        name: {"part": value.number} if isinstance(value, Part) else value
        for name, value in key.items()
    }


def _run_id(value: object) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ValueError(f"{value!r} is no run id")
    return value


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{value!r} is not true or false")
    return value


def _parts(value: object) -> int | None:
    if value is None or (isinstance(value, int) and not isinstance(value, bool) and value > 0):
        return value
    raise ValueError(f"{value!r} is no number of parts")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not text")
    return value


def _key_read(document: Mapping[str, Any]) -> dict[str, object]:
    return {
        str(name): Part(int(value["part"])) if isinstance(value, dict) else value
        for name, value in document.items()
    }
