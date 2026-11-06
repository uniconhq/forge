"""A settings file's data, or a workflow draft's, as a page reads and sends
it: plain JSON values.

Each mapping a form edits as a list, leaderboards, their columns and a task's
groups and values, is a list of named entries, so two entries with one name
reach the forge and are refused at that name. A number is a JSON number and
a key is text, so a fold's `weights` keys are given back the type of the
repeated input values they name. A workflow's inputs and outputs follow the
values' order, so a renamed one keeps its place and comments.
"""

import copy
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from forge.domain.errors import Rejected
from forge.domain.yaml_models import InvalidDefinition, Path, Problem, already_listed
from forge.domain.yaml_writer import Ordered, Unwritable, same, write_values

Kind = Literal["contest", "task", "workflow"]

FILES: dict[Kind, str] = {
    "contest": "contest.yaml",
    "task": "task.yaml",
    "workflow": "workflow.yaml",
}


MAX_FILE = 256 * 1024
"""The most a `workflow.yaml` may hold, and the most values written into a
file may take as JSON."""


class NotValues(Exception):
    """Data a form cannot be given as values."""


# Each keyed part: where it is, what an entry's name is called, and what its
# value is called when it is not spread into the entry.
_Part = tuple[Path, str, str | None]
_PARTS: dict[Kind, tuple[_Part, ...]] = {
    "contest": ((("leaderboards",), "key", None),),
    "task": ((("scoring", "groups"), "name", "picks"), (("scoring", "values"), "name", "formula")),
    "workflow": (),
}
_COLUMNS: _Part = (("columns",), "name", "formula")


def values_of(kind: Kind, data: object) -> dict[str, Any]:
    """The file's data as values; `NotValues` when it is not named settings
    or holds a number JSON cannot carry."""
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise NotValues("The file does not hold named settings.")
    values: dict[str, Any] = _plain(data)
    for path, name, value in _PARTS[kind]:
        _listed(values, path, name, value)
    if kind == "contest" and isinstance(values.get("leaderboards"), list):
        for board in values["leaderboards"]:
            _listed(board, *_COLUMNS)
    return values


def settings_of(kind: Kind, values: Mapping[str, Any]) -> dict[str, Any]:
    """The settings `values` give, each list of named entries a mapping
    again; `InvalidDefinition` naming each entry whose name an earlier one
    has, or that has none as text."""
    settings: dict[str, Any] = copy.deepcopy(dict(values))
    problems: list[Problem] = []
    for path, name, value in _PARTS[kind]:
        problems += _keyed(settings, path, name, value)
    if kind == "contest":
        boards = settings.get("leaderboards")
        if isinstance(boards, Mapping):
            for key in boards:
                if isinstance(boards[key], Mapping):
                    found = _keyed(boards[key], *_COLUMNS)
                    problems += [
                        Problem(path=("leaderboards", key, *p["path"]), message=p["message"])
                        for p in found
                    ]
    if problems:
        raise InvalidDefinition(FILES[kind], problems)
    if kind == "task":
        _retype_weights(settings)
    if kind == "workflow":
        for key in ("inputs", "outputs"):
            if isinstance(settings.get(key), Mapping):
                settings[key] = Ordered(settings[key])
    return settings


def _plain(value: object) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float | Decimal):
        number = float(value)
        if not math.isfinite(number):
            raise NotValues("The file holds a number a form cannot show.")
        return number
    if isinstance(value, Mapping):
        return {_text(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return str(value)


def _text(key: object) -> str:
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float | Decimal):
        number = float(key)
        return str(int(number)) if number.is_integer() else repr(number)
    return str(key)


def _at(values: Any, path: Path) -> tuple[Any, Any] | None:
    """The mapping holding the part at `path`, and its key."""
    for step in path[:-1]:
        values = values.get(step) if isinstance(values, Mapping) else None
    return (values, path[-1]) if isinstance(values, Mapping) else None


def _listed(values: Any, path: Path, name: str, value: str | None) -> None:
    found = _at(values, path)
    if found is None:
        return
    holder, key = found
    part = holder.get(key)
    if not isinstance(part, Mapping):
        return
    if value is None and not all(
        isinstance(item, Mapping) and name not in item for item in part.values()
    ):
        return
    holder[key] = [
        {name: entry, **item} if value is None else {name: entry, value: item}
        for entry, item in part.items()
    ]


def _keyed(values: Any, path: Path, name: str, value: str | None) -> list[Problem]:
    found = _at(values, path)
    if found is None:
        return []
    holder, key = found
    part = holder.get(key)
    if not isinstance(part, list):
        return []
    mapping = Ordered()
    problems: list[Problem] = []
    for entry in part:
        given = entry.get(name) if isinstance(entry, Mapping) else None
        if not isinstance(given, str):
            problems.append(Problem(path=path, message="Give each one a name."))
            continue
        if given in mapping:
            message = already_listed(given) if given else "Give each one a name."
            problems.append(Problem(path=(*path, given), message=message))
            continue
        if value is None:
            mapping[given] = {k: v for k, v in entry.items() if k != name}
            continue
        extra = sorted(set(entry) - {name, value})
        if extra:
            problems.append(
                Problem(path=(*path, given), message=f"{extra[0]} is not part of this entry.")
            )
        mapping[given] = entry.get(value)
    holder[key] = mapping
    return problems


def _retype_weights(settings: dict[str, Any]) -> None:
    """Each `weights` key a fold names a repeated input's value by, as that
    value's own type: the number 1 for a number input's 1, not the text."""
    repeated = _repeated(settings.get("repeat"))
    scoring = settings.get("scoring")
    formulas = scoring.get("values") if isinstance(scoring, Mapping) else None
    if not isinstance(formulas, Mapping):
        return
    for node in _nodes(formulas):
        weights = node.get("weights")
        if not isinstance(weights, Mapping):
            continue
        node["weights"] = {
            name: (
                {_typed(key, repeated.get(name, [])): w for key, w in table.items()}
                if isinstance(table, Mapping)
                else table
            )
            for name, table in weights.items()
        }


def _nodes(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


def _repeated(axes: object) -> dict[str, list[object]]:
    found: dict[str, list[object]] = {}
    if not isinstance(axes, list):
        return found
    for axis in axes:
        if not isinstance(axis, Mapping):
            continue
        inputs, rows = axis.get("inputs"), axis.get("tuples")
        if not isinstance(inputs, list) or not isinstance(rows, list):
            continue
        for row in rows:
            for column, name in enumerate(inputs):
                if isinstance(row, Mapping):
                    cell = row.get(name)
                elif isinstance(row, list):
                    cell = row[column] if column < len(row) else None
                else:
                    cell = row if len(inputs) == 1 else None
                if isinstance(name, str):
                    found.setdefault(name, []).append(cell)
    return found


def _typed(key: object, given: Sequence[object]) -> object:
    if not isinstance(key, str):
        return key
    for value in given:
        if isinstance(value, bool):
            if key == ("true" if value else "false"):
                return value
        elif isinstance(value, int | float) and same(_number(key), value):
            return value
    return key


def _number(text: str) -> Decimal | None:
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


UNWRITABLE = (
    "These settings cannot be saved from the form without losing part of the file. "
    "Change them in the stored settings."
)
UNWRITABLE_DRAFT = "These changes cannot be saved into workflow.yaml without losing part of it."


def written_values(kind: Kind, content: bytes, values: Mapping[str, Any]) -> bytes:
    """The file `content` with `values` as its settings; `Rejected` for values
    over `MAX_FILE` as JSON, `InvalidDefinition` when an entry's name repeats,
    or the values cannot be written into it."""
    _sized(kind, values)
    settings = settings_of(kind, values)
    try:
        return write_values(content.decode(), settings).encode()
    except Unwritable, UnicodeDecodeError:
        message = UNWRITABLE_DRAFT if kind == "workflow" else UNWRITABLE
        raise InvalidDefinition(FILES[kind], [Problem(path=(), message=message)]) from None


def _sized(kind: Kind, values: Mapping[str, Any]) -> None:
    size = 0
    for chunk in json.JSONEncoder(default=str).iterencode(values):
        size += len(chunk.encode())
        if size > MAX_FILE:
            most = MAX_FILE // 1024
            if kind == "workflow":
                raise Rejected(f"A workflow can be at most {most} KB. Make it smaller.")
            raise Rejected(f"Settings can be at most {most} KB. Make them smaller.")
