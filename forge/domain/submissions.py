"""A submission: what an entrant gives for each of a task's entrant
inputs, checked against those inputs, and the one commit it becomes in their
place to submit the task (SIMPLER-CORE.md 8.3, the runner's
`submission.schema.json`, version 6, `lay_out_runs`):

- `files/<input id>/<relative path>` for every file of a `file` or `folder`
  input given once, and nothing else;
- `submission.json` at the top, naming each file or folder input's files,
  or giving a text, number, true-or-false or enum input its value.

An entrant input is what the run plan's `entrant` declares, with the form
details the task gives it (`Field`). A file input is one file; a folder
input one or more, each under its own path; the files under an input
together within its `max_size`. A text, number, true-or-false or enum input
the entrant leaves out takes the task's `default`, and one without a
default is required; an enum's value is one of the task's `options`, the
workflow's when the task narrows none, and a number lies within `min` and
`max`.

An entrant input inside the task's `repeat` is given part by part, each
part its tuple's position from 1, its files at
`files/<input id>/<part>/<relative path>` and its value in a map from part
to value, so a label never shows a hidden file's name. A part not given is
left out, and every run made from its tuple is skipped (`skipped`).

    {"schema_version": 6, "inputs": {
      "best": {"value": 1200},
      "route": {"parts": {"1": {"files": ["files/route/1/route.txt"]}}}}}

The submission is named as a protected version whose note carries the
submit's idempotency key, so a submit tried again after its answer was lost
finds the submission it made instead of making a second.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import yaml

from forge.domain import exact_json
from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE, ENTRANT, Form
from forge.domain.errors import InvalidInputs
from forge.domain.ids import SubmissionId, VersionId
from forge.domain.run_task import Part, Run
from forge.domain.types import FILES, Type
from forge.domain.uploads import filename_problem

RUN_SCHEMA_VERSION = 6
SUBMISSION_FILE = "submission.json"
FILES_FOLDER = "files"
TEXT_MAX = 64 * 1024
KEY_MIN, KEY_MAX = 8, 128
KEY_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


@dataclass(frozen=True, slots=True)
class FieldPart:
    """One part of an entrant input: the tuple's title, and the most that
    part may hold, in bytes.
    """

    title: str | None
    max_size: int


@dataclass(frozen=True, slots=True)
class Field:
    """One input the entrant gives: what the plan declares of it, its type
    and an enum's options, the task's form details, its label, default,
    bounds, options and size, and its parts when it is inside `repeat`.
    """

    id: str
    type: Type
    label: str
    options: tuple[str, ...] | None = None
    default: Any = None
    min: int | Decimal | None = None
    max: int | Decimal | None = None
    max_size: int = DEFAULT_MAX_SIZE
    parts: tuple[FieldPart, ...] | None = None

    @property
    def files(self) -> bool:
        return self.type in FILES


def run_fields_of(entrant: Mapping[str, Any], inputs: Mapping[str, Any]) -> tuple[Field, ...]:
    """A task's entrant inputs, each from its run plan's `entrant` entry
    and the form details `task.yaml` gives an input made the entrant's,
    `{entrant: {...}}`: in the order `inputs` lists them, then the rest by
    name. A file's most size is the plan's.
    """
    order = [name for name in inputs if name in entrant]
    order += sorted(name for name in entrant if name not in order)
    found = []
    for name in order:
        declared = entrant[name]
        given = inputs.get(name)
        details = given.get(ENTRANT) if isinstance(given, dict) else None
        form = Form.model_validate(details) if isinstance(details, dict) else Form()
        max_size = declared.max_size or form.max_size or DEFAULT_MAX_SIZE
        found.append(
            Field(
                id=name,
                type=Type(declared.type),
                label=form.label or name,
                options=tuple(form.options or declared.options or ()) or None,
                default=form.default,
                min=form.min,
                max=form.max,
                max_size=max_size,
                parts=_field_parts(declared, max_size),
            )
        )
    return tuple(found)


def _field_parts(declared: Any, max_size: int) -> tuple[FieldPart, ...] | None:
    """A plan entry's parts, one each, or none when it is given once. A
    plan names a part's title and size only when some part has its own;
    otherwise each part is untitled and holds `max_size`, the input's.
    """
    raw = getattr(declared, "part_details", None)
    if raw:
        return tuple(FieldPart(title=part.title, max_size=part.max_size) for part in raw)
    count = getattr(declared, "parts", None)
    if not count:
        return None
    return tuple(FieldPart(title=None, max_size=max_size) for _ in range(count))


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


def path_problem(entry: Field, name: str) -> str | None:
    """What is wrong with `name` as where an uploaded file goes under its
    input, if anything: one plain name for a file input, and a path of
    plain names for a folder input.
    """
    parts = name.split("/")
    for part in parts:
        problem = filename_problem(part)
        if problem is not None:
            return problem
    if entry.type is Type.FILE and len(parts) > 1:
        return "A file name cannot include a folder."
    return None


PLAIN_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
"""A number as a contestant gives it: plain decimal digits, as the API serves
every number, with no exponent, no sign but a leading minus and no space."""


@dataclass(frozen=True, slots=True)
class SubmittedInput:
    """What a contestant gives for one input: the uploads of its files; or,
    for a text, number, true-or-false or enum input, its value. A number is
    given as the text of its plain decimal digits, `2.5`, the form every
    number is served in, so it is checked and written exactly, never read
    into a float on the way.
    """

    uploads: tuple[uuid.UUID, ...] = ()
    value: str | bool | None = None
    parts: Mapping[int, SubmittedInput] = field(default_factory=dict)


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


def lay_out_runs(
    declared: tuple[Field, ...],
    parts: Mapping[str, int],
    given: Mapping[str, SubmittedInput],
    uploads: Mapping[uuid.UUID, UploadedFile],
) -> Layout:
    """A submission, version 6: `given` for inputs `declared`, `parts`
    giving how many parts each input inside the task's `repeat` has, each
    upload of which is in `uploads`. An input given once has its files
    under `files/<input id>/`; one in parts needs at least one part, each
    part's files under `files/<input id>/<part>/`. `InvalidInputs` naming
    every problem when it does not fit, or when its `submission.json` would
    break the runner's contract.
    """
    problems: list[dict[str, str]] = []
    files: dict[str, uuid.UUID] = {}
    inputs: dict[str, Any] = {}
    known = {entry.id for entry in declared}
    for name in sorted(set(given) - known):
        problems.append({"input": name, "message": "The task has no such input."})
    for entry in declared:
        found: dict[str, Any] | str | None
        submitted = given.get(entry.id)
        count = parts.get(entry.id)
        if count is None:
            if submitted is not None and submitted.parts:
                found = "This input is given once, not in parts."
            else:
                found = _one(entry, submitted, uploads, files, entry.max_size)
        else:
            found = _parts(entry, count, submitted, uploads, files)
        if isinstance(found, str):
            problems.append({"input": entry.id, "message": found})
        elif found is not None:
            inputs[entry.id] = found
    document = {"schema_version": RUN_SCHEMA_VERSION, "inputs": inputs}
    if not problems:
        problems.extend(_contract_problems(document))
    if problems:
        first = problems[0]
        raise InvalidInputs(
            f"There is a problem with the input {first['input']}: {first['message']}",
            errors=problems,
        )
    text = exact_json.dumps(document, sort_keys=True, indent=2) + "\n"
    return Layout(files=files, document=text.encode(), inputs=inputs)


def _parts(
    entry: Field,
    count: int,
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
) -> dict[str, Any] | str:
    """An input inside the task's `repeat`: each part given, by its number,
    or a sentence saying what is wrong.
    """
    if given is None or not given.parts:
        if given is not None and (given.uploads or given.value is not None):
            return "Give this input part by part."
        return "Give at least one part of this input."
    if given.uploads or given.value is not None:
        return "Give this input part by part."
    found: dict[str, Any] = {}
    for number, part in sorted(given.parts.items()):
        if not 1 <= number <= count:
            return f"There is no part {number}: this input has parts 1 to {count}."
        if part.parts:
            return f"Part {number} is one part, with no parts of its own."
        laid: dict[str, uuid.UUID] = {}
        each = _one(entry, part, uploads, laid, _part_size(entry, number))
        if isinstance(each, str):
            return f"Part {number}: {each}"
        assert each is not None
        if "files" in each:
            each = {"files": [_in_part(path, entry.id, number) for path in each["files"]]}
            files.update(
                {_in_part(path, entry.id, number): upload for path, upload in laid.items()}
            )
        found[str(number)] = each
    return {"parts": found}


def _part_size(entry: Field, number: int) -> int:
    """The most part `number` of `entry` may hold: its own size, as the plan
    budgets its box, or the input's when the plan gives parts none.
    """
    if entry.parts is not None and 1 <= number <= len(entry.parts):
        return entry.parts[number - 1].max_size
    return entry.max_size


def _in_part(path: str, input_id: str, number: int) -> str:
    """A file's path in the commit moved into its part's folder."""
    top = f"{FILES_FOLDER}/{input_id}/"
    return f"{top}{number}/{path.removeprefix(top)}"


def skipped(runs: Sequence[Run], inputs: Mapping[str, Any]) -> frozenset[str]:
    """The runs a submission's `inputs` skips: each made from a
    tuple whose part of an entrant input was not given.
    """
    return frozenset(
        run.id
        for run in runs
        if any(
            isinstance(value, Part)
            and str(value.number) not in inputs.get(name, {}).get("parts", {})
            for name, value in run.key.items()
        )
    )


def _contract_problems(document: Mapping[str, Any]) -> list[dict[str, str]]:
    """How `document` breaks the runner's submission contract, at the input
    it is about when it is about one, so a submission the runner would
    refuse is never made.
    """
    broken = violation(document, "submission")
    if broken is None:
        return []
    location = broken.removeprefix("at ").split(":", 1)[0].split("/")
    about = location[1] if location[0] == "inputs" and len(location) > 1 else ""
    return [
        {
            "input": about,
            "message": f"Unicon cannot grade this submission as it is ({broken}). Check it "
            "with the organisers.",
        }
    ]


def _one(
    entry: Field,
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
    max_size: int,
) -> dict[str, Any] | str | None:
    """The input's entry in `submission.json`, or a sentence saying what is
    wrong. Its files come to at most `max_size` bytes.
    """
    if entry.files:
        return _files(entry, given, uploads, files, max_size)
    if given is None or given.value is None:
        if given is not None and given.uploads:
            return "Give this input a value, not files."
        if entry.default is None:
            return "This input is required."
        return {"value": entry.default}
    if given.uploads:
        return "Give this input a value, not files."
    problem = value_problem(entry, given.value)
    if problem is not None:
        return problem
    if entry.type is Type.NUMBER:
        assert isinstance(given.value, str)
        return {"value": Decimal(given.value)}
    return {"value": given.value}


def _files(
    entry: Field,
    given: SubmittedInput | None,
    uploads: Mapping[uuid.UUID, UploadedFile],
    files: dict[str, uuid.UUID],
    max_size: int,
) -> dict[str, Any] | str:
    if given is None or not given.uploads:
        return (
            "This input needs a file."
            if entry.type is Type.FILE
            else "This input needs at least one file."
        )
    if given.value is not None:
        return "Give this input files, not a value."
    if entry.type is Type.FILE and len(given.uploads) != 1:
        return "This input takes exactly one file."
    if len(set(given.uploads)) != len(given.uploads):
        return "The same file is given more than once. Give it once."
    chosen = [uploads[upload] for upload in given.uploads]
    if any(upload.input != entry.id for upload in chosen):
        return "A file here was uploaded for another input. Upload it again for this one."
    names = [upload.filename for upload in chosen]
    if len(set(names)) != len(names):
        return "Two files have the same name. Rename one of them."
    folders = {
        "/".join(name.split("/")[:end]) for name in names for end in range(1, name.count("/") + 1)
    }
    both = sorted(folders & set(names))
    if both:
        return f"{both[0]} is both a file and a folder. Rename one of them."
    for name in names:
        problem = path_problem(entry, name)
        if problem is not None:
            return f"There is a problem with the file {name}: {problem}"
    total = sum(upload.size for upload in chosen)
    if total > max_size:
        return (
            f"The files of this input come to more than the {max_size} bytes allowed. "
            "Make them smaller."
        )
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
                return "This must be text."
            if len(value.encode()) > TEXT_MAX:
                return f"This must be at most {TEXT_MAX} bytes."
        case Type.NUMBER:
            if not isinstance(value, str) or PLAIN_DECIMAL.match(value) is None:
                return "Give a number in plain digits, such as 2.5."
            number = Decimal(value)
            if entry.min is not None and number < entry.min:
                return f"This must be at least {entry.min}."
            if entry.max is not None and number > entry.max:
                return f"This must be at most {entry.max}."
        case Type.BOOLEAN:
            if not isinstance(value, bool):
                return "This must be true or false."
        case Type.ENUM:
            if value not in (entry.options or ()):
                return f"Choose one of {', '.join(entry.options or ())}."
        case _:
            return "This input does not take a value."
    return None
