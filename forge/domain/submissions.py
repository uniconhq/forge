"""A submission: what a contestant gives for each of a task's contestant
inputs, checked against those inputs, and the one commit it becomes in their
place to submit the task (the runner's `submission.schema.json`, version 4):

- `files/<input id>/<file name>` for every file of a `code`, `file` or
  `file[]` input, and nothing else;
- `submission.json` at the top, naming each input's files and, for a code
  input, the language chosen, or giving a text, number or true-or-false
  input its value.

    {"schema_version": 4, "inputs": {
      "submission": {"files": ["files/submission/main.py"], "language": "python"},
      "alpha": {"value": 0.5}}}

A code input is one file and, when the input lists languages, one of them; a
file input one file; a file[] input one or more, each under its own name. A
text, number or true-or-false input the contestant leaves out takes the
input's default, and one without a default is required. A jupyter input is
not submitted from here.

The submission is named as a protected version whose note carries the
submit's idempotency key, so a submit tried again after its answer was lost
finds the submission it made instead of making a second.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

from forge.domain.definitions import ContestantInput
from forge.domain.errors import InvalidInputs
from forge.domain.ids import SubmissionId, VersionId
from forge.domain.uploads import FILE_INPUTS
from forge.domain.workflow_definition import InputType
from forge.domain.yaml_models import is_number

SCHEMA_VERSION = 4
SUBMISSION_FILE = "submission.json"
FILES_FOLDER = "files"
TEXT_MAX = 64 * 1024
KEY_MIN, KEY_MAX = 8, 128
KEY_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


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


@dataclass(frozen=True, slots=True)
class SubmittedInput:
    """What a contestant gives for one input: the uploads of its files, and
    for a code input the language chosen; or, for a text, number or
    true-or-false input, its value.
    """

    uploads: tuple[uuid.UUID, ...] = ()
    language: str | None = None
    value: str | int | float | bool | None = None


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """A checked upload as a submission lays it out: the input it was asked
    for, the name it is committed under and its size.
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
    declared: tuple[ContestantInput, ...],
    given: Mapping[str, SubmittedInput],
    uploads: Mapping[uuid.UUID, UploadedFile],
) -> Layout:
    """The submission `given` makes for inputs `declared`, each upload of which
    is in `uploads`. `InvalidInputs` naming every problem when it does not
    fit.
    """
    problems: list[dict[str, str]] = []
    files: dict[str, uuid.UUID] = {}
    inputs: dict[str, Any] = {}
    known = {entry.id for entry in declared}
    for name in sorted(set(given) - known):
        problems.append({"input": name, "message": "The task has no such input."})
    for entry in declared:
        found = _one(entry, given.get(entry.id), uploads, files)
        if isinstance(found, str):
            problems.append({"input": entry.id, "message": found})
        elif found is not None:
            inputs[entry.id] = found
    if problems:
        first = problems[0]
        raise InvalidInputs(f"{first['input']}: {first['message']}", errors=problems)
    document = {"schema_version": SCHEMA_VERSION, "inputs": inputs}
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return Layout(files=files, document=text.encode(), inputs=inputs)


def _one(
    entry: ContestantInput,
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
) -> dict[str, Any] | str | None:
    """The input's entry in `submission.json`, a sentence saying what is
    wrong, or none for an input that is left out and needs nothing.
    """
    if entry.type is InputType.JUPYTER:
        return "A notebook input is not submitted from here." if given is not None else None
    if entry.type in FILE_INPUTS:
        return _files(entry, given, uploads, files)
    if given is None or given.value is None:
        if given is not None and (given.uploads or given.language is not None):
            return "Give this input a value, not files."
        if entry.default is None:
            return "This input is required."
        return {"value": entry.default}
    if given.uploads or given.language is not None:
        return "Give this input a value, not files."
    problem = _value_problem(entry, given.value)
    return problem if problem is not None else {"value": given.value}


def _files(
    entry: ContestantInput,
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
) -> dict[str, Any] | str:
    if given is None or not given.uploads:
        return (
            "This input needs a file."
            if entry.type is not InputType.FILES
            else ("This input needs at least one file.")
        )
    if given.value is not None:
        return "Give this input files, not a value."
    if entry.type is not InputType.FILES and len(given.uploads) != 1:
        return "This input takes exactly one file."
    if len(set(given.uploads)) != len(given.uploads):
        return "An upload is given more than once."
    chosen = [uploads[upload] for upload in given.uploads]
    if any(upload.input != entry.id for upload in chosen):
        return "An upload was asked for another input."
    names = [upload.filename for upload in chosen]
    if len(set(names)) != len(names):
        return "Two files have the same name."
    result: dict[str, Any] = {}
    if entry.type is InputType.CODE:
        languages = entry.language
        if languages is None and given.language is not None:
            return "This input takes no language."
        if languages is not None and given.language not in languages:
            return f"Choose one of the languages {', '.join(languages)}."
        if given.language is not None:
            result["language"] = given.language
    elif given.language is not None:
        return "Only a code input takes a language."
    paths = []
    for upload in sorted(chosen, key=lambda upload: upload.filename):
        path = f"{FILES_FOLDER}/{entry.id}/{upload.filename}"
        files[path] = upload.upload
        paths.append(path)
    result["files"] = paths
    return result


def _value_problem(entry: ContestantInput, value: object) -> str | None:
    match entry.type:
        case InputType.TEXT:
            if not isinstance(value, str):
                return "Must be text."
            if len(value.encode()) > TEXT_MAX:
                return f"Must be at most {TEXT_MAX} bytes."
        case InputType.NUMBER:
            if not is_number(value):
                return "Must be a number."
            assert isinstance(value, int | float)
            if entry.min is not None and value < entry.min:
                return f"Must be at least {entry.min}."
            if entry.max is not None and value > entry.max:
                return f"Must be at most {entry.max}."
        case InputType.BOOLEAN:
            if not isinstance(value, bool):
                return "Must be true or false."
        case _:
            return "This input does not take a value."
    return None
