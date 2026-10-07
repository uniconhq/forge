"""The types a value has wherever it appears, a workflow input, a test field,
a primitive's port or a form field (TASK-FORMAT.md section 0): `text`,
`number`, `boolean`, `enum`, `file` and `folder`. `outcome` is the one more
type a primitive's output port may have, and nothing else.

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


VALUE_TYPES = (Type.TEXT, Type.NUMBER, Type.BOOLEAN, Type.ENUM, Type.FILE, Type.FOLDER)
SCALARS = frozenset({Type.TEXT, Type.NUMBER, Type.BOOLEAN, Type.ENUM})
FILES = frozenset({Type.FILE, Type.FOLDER})
RETIRED_TYPES = {
    "code": "`code` is gone: a program is a `file` input, and its language an `enum` input.",
    "file[]": "`file[]` is now `folder`.",
    "dataset": "`dataset` is now `file` or `folder`.",
    "jupyter": "`jupyter` is now `file`.",
    "tests": "Tests are folders under `tests/`, declared by the workflow's `test` block.",
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
    return RETIRED_TYPES.get(str(found), "This type is not part of the format.")
