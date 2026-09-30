"""How a definition file written in YAML becomes a model, and how what is wrong
with it is reported. A file is read with a loader that refuses a key given
twice, then validated by a pydantic model that refuses any key it does not
know, so a typo is an error rather than a setting silently left out. Every
problem is reported as a path into the YAML and a sentence a person reads:
`leaderboards[0].order[1].direction`, `Must be one of asc or desc.` A form
shows the sentence beside the field the path names.
"""

import math
import re
from collections.abc import Hashable, Iterable, Sequence
from datetime import date, datetime
from typing import Annotated, Any, NoReturn, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, PlainValidator, ValidationError
from pydantic_core import ErrorDetails, PydanticCustomError

from forge.domain.errors import InvalidName, ServiceError
from forge.domain.names import validate_name

Path = tuple[str | int, ...]


class Problem(TypedDict):
    """One thing wrong with a definition file: where, and what."""

    path: str
    message: str


class InvalidDefinition(ServiceError):
    """A definition file that does not validate. `extra["errors"]` lists every
    problem found as a `Problem`, each with its YAML path; a problem with the
    file as a whole has the path "".
    """

    code = "invalid_definition"

    def __init__(self, what: str, errors: Sequence[Problem]) -> None:
        listed = list(errors)
        first = listed[0] if listed else Problem(path="", message="It is not valid.")
        where = f" at {first['path']}" if first["path"] else ""
        if len(listed) > 1:
            detail = f"{what} has {len(listed)} problems; the first is{where}: {first['message']}"
        else:
            detail = f"{what} is not valid{where}: {first['message']}"
        super().__init__(detail, errors=listed)

    @property
    def errors(self) -> list[Problem]:
        errors: list[Problem] = self.extra["errors"]
        return errors


def path_text(path: Iterable[str | int]) -> str:
    """A path as it is shown: keys joined by dots, list indices in brackets."""
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text


class _UniqueKeyLoader(yaml.SafeLoader):
    """The safe loader, refusing a mapping that gives one key twice."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Hashable, Any]:
    loader.flatten_mapping(node)
    seen: set[Hashable] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if isinstance(key, Hashable) and key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"the key {key!r} is given twice", key_node.start_mark
            )
        if isinstance(key, Hashable):
            seen.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml(text: bytes | str) -> object:
    """The document in `text`. Raises `yaml.YAMLError` when it does not parse."""
    return yaml.load(text, Loader=_UniqueKeyLoader)


def load_mapping(what: str, text: bytes | str) -> dict[str, Any]:
    """The document in `text`, which must be a mapping at the top."""
    try:
        document = load_yaml(text)
    except yaml.YAMLError as error:
        raise InvalidDefinition(what, [Problem(path="", message=_yaml_message(error))]) from None
    if not isinstance(document, dict):
        message = "The file must be a mapping of keys to values, such as `name: ...`."
        raise InvalidDefinition(what, [Problem(path="", message=message)])
    return document


def mapping_or_empty(text: bytes | str | None) -> dict[str, Any]:
    """The document in `text` if it is a mapping, and an empty one otherwise."""
    if text is None:
        return {}
    try:
        document = load_yaml(text)
    except yaml.YAMLError:
        return {}
    return document if isinstance(document, dict) else {}


def _yaml_message(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    problem = getattr(error, "problem", None) or "it is not valid YAML"
    if mark is None:
        return f"The file does not parse as YAML: {problem}."
    return f"The file does not parse as YAML at line {mark.line + 1}: {problem}."


class Model(BaseModel):
    """A part of a definition file: immutable, and refusing unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def validate[M: Model](model: type[M], what: str, document: dict[str, Any]) -> M:
    """`document` as a `model`, or `InvalidDefinition` listing every problem."""
    try:
        return model.model_validate(document)
    except ValidationError as error:
        raise InvalidDefinition(what, problems_of(error)) from None


def problems_of(error: ValidationError) -> list[Problem]:
    """Every problem pydantic found, each at its YAML path. A list that is
    too short only because some of its entries failed is not reported
    again, and a validator's own problems below the model are reported at
    their own paths.
    """
    details = error.errors()
    failed_entries = {tuple(detail["loc"]) for detail in details}
    problems: list[Problem] = []
    for detail in details:
        loc = tuple(detail["loc"])
        if detail["type"] == "too_short" and any(
            place[: len(loc)] == loc and len(place) > len(loc) for place in failed_entries
        ):
            continue
        if detail["type"] == "at_paths":
            for sub, message in detail.get("ctx", {})["problems"]:
                problems.append(Problem(path=path_text(loc + tuple(sub)), message=message))
        else:
            problems.append(Problem(path=path_text(loc), message=_message(detail)))
    return problems


def _listed(expected: str) -> str:
    return expected.replace("'", "")


def _message(detail: ErrorDetails) -> str:
    kind = detail["type"]
    ctx: dict[str, Any] = dict(detail.get("ctx") or {})
    match kind:
        case "missing":
            return "This key is required."
        case "extra_forbidden":
            return "This key is not part of the format; check its spelling."
        case "string_type":
            return "Must be text."
        case "int_type" | "int_from_float" | "int_parsing":
            return "Must be a whole number."
        case "bool_type" | "bool_parsing":
            return "Must be true or false."
        case "list_type" | "tuple_type":
            return "Must be a list."
        case "model_type" | "model_attributes_type" | "dict_type":
            return "Must be a mapping of keys to values."
        case "enum" | "literal_error":
            return f"Must be one of {_listed(str(ctx.get('expected', '')))}."
        case "greater_than_equal":
            return f"Must be at least {ctx.get('ge')}."
        case "greater_than":
            return f"Must be more than {ctx.get('gt')}."
        case "string_too_short":
            return "Must not be empty."
        case "too_short":
            count = ctx.get("min_length")
            return (
                "Must have at least one entry."
                if count == 1
                else f"Must have at least {count} entries."
            )
        case "value_error":
            return str(ctx.get("error", detail["msg"]))
    return detail["msg"]


def fail(problems: Sequence[tuple[Path, str]]) -> NoReturn:
    """Refuse from inside a model validator, naming paths below the model."""
    raise PydanticCustomError(
        "at_paths", "{count} problems", {"count": len(problems), "problems": list(problems)}
    )


class Problems:
    """The problems a model validator finds, raised together at the end."""

    def __init__(self) -> None:
        self.found: list[tuple[Path, str]] = []

    def add(self, path: Path, message: str) -> None:
        self.found.append((path, message))

    def raise_any(self) -> None:
        if self.found:
            fail(self.found)

    def duplicates(self, ids: Sequence[str], at: Path, key: str = "id") -> None:
        """Adds a problem for every entry whose `key` an earlier entry gave."""
        seen: set[str] = set()
        for index, value in enumerate(ids):
            if value in seen:
                self.add((*at, index, key), f"{value!r} is given more than once.")
            seen.add(value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be text.")
    if not value.strip():
        raise ValueError("Must not be empty.")
    return value


def _handle(value: object) -> str:
    text = _text(value)
    try:
        return validate_name(text)
    except InvalidName:
        raise ValueError(
            f"{text!r} must be lower case letters, digits, hyphens and underscores, "
            "starting with a letter or a digit, at most 40 characters."
        ) from None


def _number(value: object) -> int | float:
    if not is_number(value):
        raise ValueError("Must be a number.")
    assert isinstance(value, int | float)
    return value


def is_number(value: object) -> bool:
    """Whether `value` is a number: an int or a float, not a true-or-false,
    and not an infinity or NaN, which YAML and JSON can spell but no limit,
    input or plan holds.
    """
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


_TIME_EXAMPLE = "such as 2026-06-01T09:00:00Z"


def _aware_time(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"Must be a date and time, {_TIME_EXAMPLE}.") from None
    if not isinstance(value, datetime):
        if isinstance(value, date):
            raise ValueError(
                f"Must carry a time and a timezone as well as a date, {_TIME_EXAMPLE}."
            )
        raise ValueError(f"Must be a date and time, {_TIME_EXAMPLE}.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Must carry a timezone, {_TIME_EXAMPLE}.")
    return value


Text = Annotated[str, PlainValidator(_text)]
"""Text that is not empty or only spaces."""

Handle = Annotated[str, PlainValidator(_handle)]
"""An id following the name rules in `forge.domain.names`."""

Number = Annotated[int | float, PlainValidator(_number)]
"""A whole or decimal number, and never true or false."""

AwareTime = Annotated[datetime, PlainValidator(_aware_time)]
"""A date and time with a timezone, written as YAML writes one or as ISO 8601 text."""

_SIZE = re.compile(r"^\s*(\d+)\s*(B|KB|MB|GB)\s*$", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}


def parse_size(value: object) -> int:
    """A size such as `10MB` in bytes. A kilobyte is 1024 bytes."""
    match = _SIZE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Must be a size such as 512KB, 10MB or 1GB.")
    size = int(match.group(1)) * _SIZE_UNITS[match.group(2).lower()]
    if size < 1:
        raise ValueError("Must be more than nothing.")
    return size


def format_size(size: int) -> str:
    """A size in bytes written the way `parse_size` reads it, in the largest
    unit that divides it.
    """
    for unit in ("GB", "MB", "KB"):
        factor = _SIZE_UNITS[unit.lower()]
        if size % factor == 0:
            return f"{size // factor}{unit}"
    return f"{size}B"


Size = Annotated[int, PlainValidator(parse_size)]
"""A size written as `10MB`, held in bytes."""
