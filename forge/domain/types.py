"""The types a value has wherever it appears, a workflow input, a test field,
a primitive's port or a form field (TASK-FORMAT.md section 0): `text`,
`number`, `boolean`, `enum`, `file` and `folder`. `outcome` is the one more
type a primitive's output port may have, and nothing else. A contract 6
primitive's input port may also be `named_files`, `programs` or `links`
(`PORT_TYPES`), which no workflow input, test field or form field takes:
they read only `VALUE_TYPES`.

A declaration of a type is a mapping with `type`, or just the type's name
when nothing else is said: `time_limit: number` is `time_limit: {type:
number}`. `declared` turns the short form into the long one before a model
reads it.
"""

from enum import StrEnum


class Type(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    FILE = "file"
    FOLDER = "folder"
    OUTCOME = "outcome"
    NAMED_FILES = "named_files"
    PROGRAMS = "programs"
    LINKS = "links"


VALUE_TYPES = (Type.TEXT, Type.NUMBER, Type.BOOLEAN, Type.ENUM, Type.FILE, Type.FOLDER)
PORT_TYPES = (Type.NAMED_FILES, Type.PROGRAMS, Type.LINKS)
"""The types only a primitive's input port may have, from contract 6: files
by the names a program sees them under, the programs a step runs side by
side, and the pipes between them."""
SCALARS = frozenset({Type.TEXT, Type.NUMBER, Type.BOOLEAN, Type.ENUM})
FILES = frozenset({Type.FILE, Type.FOLDER})
RETIRED_TYPES = {
    "code": "This type is no longer used: give a program as a file input, and its language "
    "as an enum input.",
    "file[]": "This type is now called folder.",
    "dataset": "This type is no longer used: use file or folder instead.",
    "jupyter": "This type is no longer used: use file instead.",
    "tests": "Tests are no longer an input: the task repeats the workflow over its tests with "
    "repeat.",
}


def declared(value: object) -> object:
    """A declaration in its long form: `number` becomes `{type: number}`;
    anything else is left as it is for the model to read.
    """
    return {"type": value} if isinstance(value, str) else value


def retired_type(value: object) -> bool:
    """Whether a declaration names a type the format no longer has."""
    found = value.get("type") if isinstance(value, dict) else value
    return isinstance(found, str) and found in RETIRED_TYPES


def retired_type_message(value: object) -> str:
    found = value.get("type") if isinstance(value, dict) else value
    return RETIRED_TYPES.get(str(found), "Unicon does not know this type.")
