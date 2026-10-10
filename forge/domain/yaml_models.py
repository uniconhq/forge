"""How a definition file written in YAML becomes a model, and how what is wrong
with it is reported. A file is read as YAML 1.2 with its core schema, every
file alike whatever `%YAML` line it starts with: only `true` and `false` are
true and false, so a group named `no` or the country code `NO` is text, and
`1_000`, `1:30` and a date are text too. Every number with a point or an
exponent is read as the exact `Decimal` it is written as, never a float, so
no digit of a bound is lost on its way to a score. The loader refuses a key
given twice, and the document is then validated by a pydantic model that refuses any key it does not
know, so a typo is an error rather than a setting silently left out. Every
problem is reported as a path into the YAML and a sentence a person reads:
`leaderboards[0].order[1].direction`, `Must be one of asc or desc.` A form
shows the sentence beside the field the path names.
"""

import io
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, NoReturn, TypedDict

from pydantic import BaseModel, ConfigDict, PlainValidator, ValidationError
from pydantic_core import ErrorDetails, PydanticCustomError
from ruamel.yaml import YAML
from ruamel.yaml.constructor import ConstructorError, SafeConstructor
from ruamel.yaml.error import YAMLError as YAMLError
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import SafeRepresenter
from ruamel.yaml.resolver import BaseResolver

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


_TAG = "tag:yaml.org,2002:"
_DIGITS = list("0123456789")
_CORE: tuple[tuple[str, str, list[str]], ...] = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", ["-", "+", *_DIGITS]),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        ["-", "+", ".", *_DIGITS],
    ),
)


class _CoreResolver(BaseResolver):
    """YAML 1.2's core schema and nothing else, whatever version a file
    names: no timestamps, no merge key, no `yes`, no `1_000`. Every
    resolver is filed under the characters it can start with, since the
    library adds the ones filed under none to a list it keeps.
    """

    yaml_implicit_resolvers: dict[Any, list[tuple[str, re.Pattern[str]]]] = {}  # noqa: RUF012

    def __init__(self, version: Any = None, loader: Any = None) -> None:
        super().__init__(loader)

    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 2)


for _name, _pattern, _first in _CORE:
    for _char in _first:
        _CoreResolver.yaml_implicit_resolvers.setdefault(_char, []).append(
            (_TAG + _name, re.compile(_pattern))
        )


class _ExactConstructor(SafeConstructor):
    """The safe constructor with every number that is not whole an exact
    `Decimal`, every whole one read as 1.2 spells it, and a mapping that
    gives one key twice refused.
    """

    def check_mapping_key(
        self, node: Any, key_node: Any, mapping: Any, key: Any, value: Any
    ) -> bool:
        if key in mapping:
            raise ConstructorError(
                None, None, f"the key {key!r} is given twice", key_node.start_mark
            )
        return True

    def construct_exact_float(self, node: ScalarNode) -> Decimal:
        text = str(self.construct_scalar(node))
        bare = text.lstrip("+-").lower()
        if bare == ".inf":
            return Decimal("-Infinity" if text.startswith("-") else "Infinity")
        if bare == ".nan":
            return Decimal("NaN")
        return Decimal(text)

    def construct_not_core(self, node: Any) -> Any:
        raise ConstructorError(
            None,
            None,
            f"the tag {node.tag} is not one of YAML 1.2's core schema",
            node.start_mark,
        )

    def construct_core_int(self, node: ScalarNode) -> int:
        text = str(self.construct_scalar(node))
        if text.startswith(("0o", "0x")):
            return int(text[2:], 8 if text[1] == "o" else 16)
        return int(text, 10)


_ExactConstructor.add_constructor(_TAG + "float", _ExactConstructor.construct_exact_float)
_ExactConstructor.add_constructor(_TAG + "int", _ExactConstructor.construct_core_int)
for _other in ("timestamp", "binary", "set", "omap", "pairs", "merge", "value"):
    _ExactConstructor.add_constructor(_TAG + _other, _ExactConstructor.construct_not_core)


def _reader() -> YAML:
    reader = YAML(typ="safe", pure=True)
    reader.Resolver = _CoreResolver
    reader.Constructor = _ExactConstructor
    return reader


def load_yaml(text: bytes | str) -> object:
    """The document in `text`, read as YAML 1.2's core schema with every
    number that is not whole a `Decimal`. Raises `YAMLError` when it does
    not parse.
    """
    return _reader().load(text)


class _ExactRepresenter(SafeRepresenter):
    """The safe representer, writing a `Decimal` as the digits it holds."""

    def represent_decimal(self, data: Decimal) -> ScalarNode:
        """Its digits, under the int tag when they are a whole number with
        no point, so they are written bare rather than tagged.
        """
        text = str(data)
        whole = re.fullmatch(r"-?[0-9]+", text) is not None
        node: ScalarNode = self.represent_scalar(_TAG + ("int" if whole else "float"), text)
        return node


_ExactRepresenter.add_representer(Decimal, _ExactRepresenter.represent_decimal)


def one_line(value: object, *, double_quoted: bool = False) -> str:
    """`value` as YAML 1.2 on one line, read back by `load_yaml` as the same
    value: text that would read as something else is quoted, a `Decimal` is
    written as its digits, and a mapping keeps its order, in flow style.
    """
    writer = YAML(typ="safe", pure=True)
    writer.Resolver = _CoreResolver
    writer.Representer = _ExactRepresenter
    writer.default_flow_style = True
    writer.default_style = '"' if double_quoted else None  # type: ignore[assignment]
    writer.width = 1 << 30
    writer.sort_base_mapping_type_on_output = False  # type: ignore[assignment]
    written = io.StringIO()
    writer.dump(value, written)
    return written.getvalue().removesuffix("\n").removesuffix("\n...").strip()


def load_mapping(what: str, text: bytes | str) -> dict[str, Any]:
    """The document in `text`, which must be a mapping at the top."""
    try:
        document = load_yaml(text)
    except YAMLError as error:
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
    except YAMLError:
        return {}
    return document if isinstance(document, dict) else {}


def _yaml_message(error: YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    problem = getattr(error, "problem", None) or "it is not valid YAML"
    if mark is None:
        return f"The file does not parse as YAML: {problem}."
    return f"The file does not parse as YAML at line {mark.line + 1}: {problem}."


class Model(BaseModel):
    """A part of a definition file: immutable, and refusing unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


ANY = object()
"""A part of a `Retired` path that matches any key or index."""


@dataclass(frozen=True, slots=True)
class Retired:
    """A key, or a value at a key, the format no longer has, and the sentence
    that says what replaced it, or a function of the value giving it. `path`
    may hold `ANY` for a list index or a mapping key; `when`, given, says
    which values at the path are retired, and without it any value is.
    """

    path: tuple[object, ...]
    message: str | Callable[[object], str]
    when: Callable[[object], bool] | None = None


def retired_problems(document: object, rules: Sequence[Retired]) -> list[tuple[Path, str]]:
    """Every place in `document` a rule of `rules` names, with its sentence."""
    found: list[tuple[Path, str]] = []
    for rule in rules:
        for path, value in _at(document, rule.path, ()):
            if rule.when is None or rule.when(value):
                message = rule.message if isinstance(rule.message, str) else rule.message(value)
                found.append((path, message))
    return found


def _at(node: object, pattern: tuple[object, ...], path: Path) -> list[tuple[Path, object]]:
    if not pattern:
        return [(path, node)]
    head, rest = pattern[0], pattern[1:]
    if isinstance(node, dict):
        keys = [key for key in node if isinstance(key, str | int)] if head is ANY else [head]
        return [
            found
            for key in keys
            if key in node
            for found in _at(node[key], rest, (*path, key))  # type: ignore[arg-type]
        ]
    if isinstance(node, list) and (head is ANY or isinstance(head, int)):
        indices = range(len(node)) if head is ANY else [head]
        return [
            found
            for index in indices
            if isinstance(index, int) and 0 <= index < len(node)
            for found in _at(node[index], rest, (*path, index))
        ]
    return []


def validate[M: Model](
    model: type[M], what: str, document: dict[str, Any], retired: Sequence[Retired] = ()
) -> M:
    """`document` as a `model`, or `InvalidDefinition` listing every problem.
    A key or value `retired` names is refused with its own sentence, and
    nothing else is said about it or anything under it.
    """
    old = [
        Problem(path=path_text(path), message=message)
        for path, message in retired_problems(document, retired)
    ]
    try:
        found = model.model_validate(document)
    except ValidationError as error:
        problems = [
            problem
            for problem in problems_of(error)
            if not any(_under(problem["path"], replaced["path"]) for replaced in old)
        ]
        raise InvalidDefinition(what, [*old, *problems]) from None
    if old:
        raise InvalidDefinition(what, old)
    return found


def _under(path: str, top: str) -> bool:
    return path == top or path.startswith((f"{top}.", f"{top}["))


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
        # A mapping's key that fails is reported at the key itself; pydantic
        # adds a `[key]` part no YAML path has.
        loc = tuple(part for part in detail["loc"] if part != "[key]")
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
        case "less_than_equal":
            return f"Must be at most {ctx.get('le')}."
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


def _number(value: object) -> int | Decimal:
    if not is_number(value):
        raise ValueError("Must be a number.")
    assert isinstance(value, int | Decimal | float)
    return exact_number(value)


def exact_number(value: int | Decimal | float) -> int | Decimal:
    """A number as the platform holds one: an int, or the exact `Decimal`
    of a decimal, a float being taken as the shortest decimal that reads
    back as it.
    """
    return Decimal(repr(value)) if isinstance(value, float) else value


def is_number(value: object) -> bool:
    """Whether `value` is a number: an int, a `Decimal` or a float, not a
    true-or-false, and not an infinity or NaN, which YAML and JSON can spell
    but no limit, input or plan holds.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        return value.is_finite()
    return isinstance(value, float) and math.isfinite(value)


_TIME_EXAMPLE = "such as 2026-06-01T09:00:00Z"


_DATE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d\d?)-(?P<day>\d\d?)$")
_TIME = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d\d?)-(?P<day>\d\d?)(?:[Tt]|[ \t]+)"
    r"(?P<hour>\d\d?):(?P<minute>\d\d):(?P<second>\d\d)(?:\.(?P<fraction>\d*))?"
    r"(?:[ \t]*(?P<zone>[Zz]|(?P<sign>[-+])(?P<zone_hour>\d\d?)(?::?(?P<zone_minute>\d\d))?))?$"
)


def read_time(text: str) -> date | None:
    """A date, or a date and time, in every spelling a definition file has
    been read in: ISO 8601 as `datetime.fromisoformat` reads it, such as
    `2026-06-01T09:00:00Z` or `2026-06-01T09:00+08:00`, and the spellings
    YAML 1.1 read as a time besides, with a space or a lower case `t`, one
    digit for a month, a day or an hour, and a zone such as ` -5`. None for
    any other text, or one naming no real moment.
    """
    text = text.strip()
    try:
        if (found := _DATE.match(text)) is not None:
            return date(int(found["year"]), int(found["month"]), int(found["day"]))
        if (found := _TIME.match(text)) is None:
            return _iso_time(text)
        zone = None
        if found["zone"] is not None:
            zone = UTC
            if found["sign"] is not None:
                offset = timedelta(
                    hours=int(found["zone_hour"]), minutes=int(found["zone_minute"] or 0)
                )
                zone = timezone(-offset if found["sign"] == "-" else offset)
        return datetime(
            int(found["year"]),
            int(found["month"]),
            int(found["day"]),
            int(found["hour"]),
            int(found["minute"]),
            int(found["second"]),
            int((found["fraction"] or "0")[:6].ljust(6, "0")),
            tzinfo=zone,
        )
    except ValueError:
        return None


def _iso_time(text: str) -> datetime | None:
    """`text` as ISO 8601 names a date and time, or None. A date alone is
    left to `_DATE`, which reads it as a date.
    """
    if not text[:1].isdigit():
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _aware_time(value: object) -> datetime:
    if isinstance(value, str):
        read = read_time(value)
        if read is None:
            raise ValueError(f"Must be a date and time, {_TIME_EXAMPLE}.")
        value = read
    if not isinstance(value, datetime):
        if isinstance(value, date):
            raise ValueError(
                f"Must carry a time and a timezone as well as a date, {_TIME_EXAMPLE}."
            )
        raise ValueError(f"Must be a date and time, {_TIME_EXAMPLE}.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Must carry a timezone, {_TIME_EXAMPLE}.")
    return value


def _line(value: object) -> str:
    text = _text(value)
    if "\n" in text or "\r" in text:
        raise ValueError("Must be one line.")
    return text


Text = Annotated[str, PlainValidator(_text)]
"""Text that is not empty or only spaces."""

Line = Annotated[str, PlainValidator(_line)]
"""One line of text: not empty, not only spaces, no line break."""

Handle = Annotated[str, PlainValidator(_handle)]
"""An id following the name rules in `forge.domain.names`."""

Number = Annotated[int | Decimal, PlainValidator(_number)]
"""A whole number as an int or a decimal one as its exact `Decimal`, and
never true or false."""

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
