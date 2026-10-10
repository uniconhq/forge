"""A submission: what a contestant gives for each of a task's contestant
inputs, checked against those inputs, and the one commit it becomes in their
place to submit the task (TASK-FORMAT.md section 2, the runner's
`submission.schema.json`, version 5):

- `files/<input id>/<relative path>` for every file of a `file` or `folder`
  input, and nothing else; a `per_test` input's files are at
  `files/<input id>/<group>/<test>` or `files/<input id>/<group>/<test>.<ending>`,
  the ending not empty, one per test of the plan at most;
- `submission.json` at the top, naming each file or folder input's files,
  or giving a text, number, true-or-false or enum input its value.

    {"schema_version": 5, "inputs": {
      "submission": {"files": ["files/submission/main.py"]},
      "language": {"value": "python"}}}

A contestant input is what the plan's `contestant` declares, with the form
details the task gives it (`Field`). A file input is one file; a folder
input one or more, each under its own path; the files under an input
together within its `max_size`. A text, number, true-or-false or enum input
the contestant leaves out takes the task's `default`, and one without a
default is required; an enum's value is one of the task's `options`, the
workflow's when the task narrows none, and a number lies within `min` and
`max`.

The submission is named as a protected version whose note carries the
submit's idempotency key, so a submit tried again after its answer was lost
finds the submission it made instead of making a second.
"""

import json
import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import yaml

from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE, Form
from forge.domain.errors import InvalidInputs
from forge.domain.ids import SubmissionId, VersionId
from forge.domain.types import FILES, Type
from forge.domain.uploads import filename_problem
from forge.domain.yaml_models import is_number

SCHEMA_VERSION = 5
SUBMISSION_FILE = "submission.json"
FILES_FOLDER = "files"
TEXT_MAX = 64 * 1024
KEY_MIN, KEY_MAX = 8, 128
KEY_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
TEST_FILE = re.compile(r"^(?P<group>[A-Za-z0-9_-]+)/(?P<test>[A-Za-z0-9_-]+)(\.[^/]+)?$")
"""A per-test input's file name: `<group>/<test>`, or `<group>/<test>.<ending>`
with an ending that is not empty, the harness's own rule."""


@dataclass(frozen=True, slots=True)
class Field:
    """One input the contestant gives: what the plan declares of it, its
    type, an enum's options and whether it is one file per test, and the
    task's form details, its label, default, bounds, options and size.
    """

    id: str
    type: Type
    label: str
    options: tuple[str, ...] | None = None
    per_test: bool = False
    default: Any = None
    min: int | Decimal | None = None
    max: int | Decimal | None = None
    max_size: int = DEFAULT_MAX_SIZE

    @property
    def files(self) -> bool:
        return self.type in FILES


def fields_of(contestant: Mapping[str, Any], inputs: Mapping[str, Any]) -> tuple[Field, ...]:
    """The contestant's inputs, each from the plan's `contestant` declaration
    of it and the task's form details in `inputs`: in the order `task.yaml`
    lists them, then the rest by name.
    """
    order = [name for name in inputs if name in contestant]
    order += sorted(name for name in contestant if name not in order)
    found = []
    for name in order:
        declared = contestant[name]
        details = inputs.get(name)
        form = Form.model_validate(details) if isinstance(details, dict) else Form()
        options = tuple(form.options or declared.options or ()) or None
        found.append(
            Field(
                id=name,
                type=Type(declared.type),
                label=form.label or name,
                options=options,
                per_test=bool(declared.per_test),
                default=form.default,
                min=form.min,
                max=form.max,
                max_size=form.max_size if form.max_size is not None else DEFAULT_MAX_SIZE,
            )
        )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Submitted:
    """A submission as the port hands it back: its id, its number among the
    workspace's submissions of the task, the version its files went in with,
    the idempotency key its note carries, if any, and when it was made.
    """

    id: SubmissionId
    number: int
    version: VersionId
    key: str | None
    at: datetime


def write_note(key: str) -> str:
    """The note a submission's protected version is made with."""
    return yaml.safe_dump({"idempotency_key": key}, sort_keys=False)


def read_note(text: str | None) -> str | None:
    """The idempotency key a submission's note carries, or none when the note
    is missing or does not read.
    """
    try:
        document = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return None
    if not isinstance(document, dict):
        return None
    key = document.get("idempotency_key")
    return key if isinstance(key, str) else None


def key_is_valid(key: str) -> bool:
    """Whether `key` is an idempotency key: 8 to 128 letters, digits, hyphens
    and underscores, which a UUID is.
    """
    return KEY_MIN <= len(key) <= KEY_MAX and set(key) <= KEY_CHARACTERS


def path_problem(entry: Field, name: str, tests: Collection[str]) -> str | None:
    """What is wrong with `name` as where an uploaded file goes under its
    input, if anything: one plain name for a file input, a path of plain
    names for a folder input, and `<group>/<test>` with or without an
    ending, for a test of the plan, for a per-test input.
    """
    parts = name.split("/")
    for part in parts:
        problem = filename_problem(part)
        if problem is not None:
            return problem
    if entry.per_test:
        matched = TEST_FILE.match(name)
        if matched is None or f"{matched['group']}/{matched['test']}" not in tests:
            return "A file of this input is named for a test, <group>/<test>, such as main/1.txt."
        return None
    if entry.type is Type.FILE and len(parts) > 1:
        return "A file name is one name, with no folder in it."
    return None


@dataclass(frozen=True, slots=True)
class SubmittedInput:
    """What a contestant gives for one input: the uploads of its files; or,
    for a text, number, true-or-false or enum input, its value.
    """

    uploads: tuple[uuid.UUID, ...] = ()
    value: str | int | float | bool | None = None


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """A checked upload as a submission lays it out: the input it was asked
    for, the path it is committed under in that input and its size.
    """

    upload: uuid.UUID
    input: str
    filename: str
    size: int


@dataclass(frozen=True, slots=True)
class Layout:
    """Where each upload goes in the submission's commit, by path, and the
    `submission.json` that names them.
    """

    files: Mapping[str, uuid.UUID]
    document: bytes
    inputs: Mapping[str, Any] = field(default_factory=dict)


def lay_out(
    declared: tuple[Field, ...],
    tests: Collection[str],
    given: Mapping[str, SubmittedInput],
    uploads: Mapping[uuid.UUID, UploadedFile],
) -> Layout:
    """The submission `given` makes for inputs `declared` of a plan whose
    tests are `tests`, each upload of which is in `uploads`. `InvalidInputs`
    naming every problem when it does not fit, or when its `submission.json`
    would break the runner's contract.
    """
    problems: list[dict[str, str]] = []
    files: dict[str, uuid.UUID] = {}
    inputs: dict[str, Any] = {}
    known = {entry.id for entry in declared}
    for name in sorted(set(given) - known):
        problems.append({"input": name, "message": "The task has no such input."})
    for entry in declared:
        found = _one(entry, tests, given.get(entry.id), uploads, files)
        if isinstance(found, str):
            problems.append({"input": entry.id, "message": found})
        elif found is not None:
            inputs[entry.id] = found
    document = {"schema_version": SCHEMA_VERSION, "inputs": inputs}
    if not problems:
        problems.extend(_contract_problems(document))
    if problems:
        first = problems[0]
        raise InvalidInputs(f"{first['input']}: {first['message']}", errors=problems)
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return Layout(files=files, document=text.encode(), inputs=inputs)


def _contract_problems(document: Mapping[str, Any]) -> list[dict[str, str]]:
    """How `document` breaks the runner's submission contract, at the input
    it is about when it is about one, so a submission the harness would
    refuse is never made.
    """
    broken = violation(document, "submission")
    if broken is None:
        return []
    location = broken.removeprefix("at ").split(":", 1)[0].split("/")
    about = location[1] if location[0] == "inputs" and len(location) > 1 else ""
    return [{"input": about, "message": f"The submission breaks the runner's contract {broken}."}]


def _one(
    entry: Field,
    tests: Collection[str],
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
) -> dict[str, Any] | str | None:
    """The input's entry in `submission.json`, or a sentence saying what is
    wrong.
    """
    if entry.files:
        return _files(entry, tests, given, uploads, files)
    if given is None or given.value is None:
        if given is not None and given.uploads:
            return "Give this input a value, not files."
        if entry.default is None:
            return "This input is required."
        return {"value": entry.default}
    if given.uploads:
        return "Give this input a value, not files."
    problem = value_problem(entry, given.value)
    return problem if problem is not None else {"value": given.value}


def _files(
    entry: Field,
    tests: Collection[str],
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
) -> dict[str, Any] | str:
    if given is None or not given.uploads:
        return (
            "This input needs a file."
            if entry.type is Type.FILE
            else "This input needs at least one file."
        )
    if given.value is not None:
        return "Give this input files, not a value."
    if entry.type is Type.FILE and not entry.per_test and len(given.uploads) != 1:
        return "This input takes exactly one file."
    if len(set(given.uploads)) != len(given.uploads):
        return "An upload is given more than once."
    chosen = [uploads[upload] for upload in given.uploads]
    if any(upload.input != entry.id for upload in chosen):
        return "An upload was asked for another input."
    names = [upload.filename for upload in chosen]
    if len(set(names)) != len(names):
        return "Two files have the same path."
    for name in names:
        problem = path_problem(entry, name, tests)
        if problem is not None:
            return f"{name}: {problem}"
    if entry.per_test:
        answered = [
            f"{matched['group']}/{matched['test']}"
            for name in names
            if (matched := TEST_FILE.match(name)) is not None
        ]
        if len(set(answered)) != len(answered):
            return "Two files answer the same test."
    total = sum(upload.size for upload in chosen)
    if total > entry.max_size:
        return f"The files of this input total more than the {entry.max_size} bytes allowed."
    paths = []
    for upload in sorted(chosen, key=lambda upload: upload.filename):
        path = f"{FILES_FOLDER}/{entry.id}/{upload.filename}"
        files[path] = upload.upload
        paths.append(path)
    return {"files": paths}


def value_problem(entry: Field, value: object) -> str | None:
    """What is wrong with `value` for a text, number, true-or-false or enum
    input, if anything.
    """
    match entry.type:
        case Type.TEXT:
            if not isinstance(value, str):
                return "Must be text."
            if len(value.encode()) > TEXT_MAX:
                return f"Must be at most {TEXT_MAX} bytes."
        case Type.NUMBER:
            if not is_number(value):
                return "Must be a number."
            assert isinstance(value, int | Decimal | float)
            if entry.min is not None and value < entry.min:
                return f"Must be at least {entry.min}."
            if entry.max is not None and value > entry.max:
                return f"Must be at most {entry.max}."
        case Type.BOOLEAN:
            if not isinstance(value, bool):
                return "Must be true or false."
        case Type.ENUM:
            if value not in (entry.options or ()):
                return f"Choose one of {', '.join(entry.options or ())}."
        case _:
            return "This input does not take a value."
    return None
