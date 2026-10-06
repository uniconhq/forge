"""A new task's entry in its contest's `tasks` list, written into the text of
`contest.yaml` so the rest of the file stays as the organisers wrote it,
comments and layout included.

The entry is `{id: <task>}`: the task's label is its place in the list, and
its worth the default 100 once it gives points (TASK-FORMAT.md section 1.1).
In a contest already published, the entry also sets `release_at` and
`closes` at the contest's `end`, so work in progress shows nothing until the
organisers move them. It goes at the end of the list: a `tasks: []`
becomes a list of one, a block list gets one more item at the indent its
items already use, and a file with no `tasks` key gets one at the end. A
list written in flow style with items in it, `tasks: [{id: a, ...}]`, is
not edited, since no line of it can be added to on its own. Whatever is
written is read back and must say exactly what the file said before plus
the one entry; when it does not, nothing is written.
"""

import json
import re
from datetime import datetime

import yaml

from forge.domain.definitions import State, parse_contest, stamp
from forge.domain.yaml_models import load_mapping, load_yaml

_TASKS_KEY = re.compile(r"tasks:[ \t]*(?P<rest>.*)")
_EMPTY_FLOW = re.compile(r"\[[ \t]*\](?P<after>[ \t]*(#.*)?)")
_ITEM = re.compile(r"(?P<indent>[ \t]*)-[ \t]")


class Unlisted(Exception):
    """The file's `tasks` cannot be added to by an edit of its text, or the
    edit would not read back as the file plus the one entry.
    """


def with_entry(content: bytes, task: str) -> bytes | None:
    """`contest.yaml` with an entry for `task`, or none when it has one
    already. `InvalidDefinition` when the file is not a valid `contest.yaml`;
    `Unlisted` when its `tasks` cannot be added to.
    """
    contest = parse_contest(content)
    if contest.entry(task) is not None:
        return None
    entry: dict[str, object] = {"id": task}
    lines = [f"- id: {_scalar(task)}"]
    if contest.state is State.PUBLISHED:
        entry["release_at"] = entry["closes"] = contest.end
        lines += [f"  release_at: {stamp(contest.end)}", f"  closes: {stamp(contest.end)}"]
    text = content.decode()
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = _inserted(text, lines, newline)
    before = load_mapping("contest.yaml", text)
    expected = {**before, "tasks": [*(before.get("tasks") or []), entry]}
    if _comparable(load_mapping("contest.yaml", updated)) != _comparable(expected):
        raise Unlisted("The edit would change more than the tasks list.")
    parse_contest(updated.encode())
    return updated.encode()


def _comparable(value: object) -> object:
    """`value` with every time in UTC, as YAML may read one in another zone."""
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.timestamp()
    return value


def _scalar(value: str) -> str:
    """`value` as YAML reads it back as the same string: plain when it is,
    in double quotes otherwise, which keeps a name like `no` or `123` a
    string.
    """
    try:
        plain = load_yaml(value) == value
    except yaml.YAMLError:
        plain = False
    return value if plain else json.dumps(value)


def _inserted(text: str, entry: list[str], newline: str) -> str:
    lines = text.splitlines()
    keys = [
        (index, found["rest"])
        for index, line in enumerate(lines)
        if (found := _TASKS_KEY.fullmatch(line)) is not None
    ]
    if not keys:
        return newline.join([*lines, "tasks:", *(f"  {line}" for line in entry)]) + newline
    if len(keys) > 1:
        raise Unlisted("The file has more than one tasks key.")
    ((key, after),) = keys
    empty = _EMPTY_FLOW.fullmatch(after)
    if empty is not None:
        head = f"tasks:{empty['after']}"
        added = [f"  {line}" for line in entry]
        return newline.join([*lines[:key], head, *added, *lines[key + 1 :]]) + newline
    if after.strip() and not after.lstrip().startswith("#"):
        raise Unlisted("The tasks list is not written one item per line.")
    end = key + 1
    last = key
    indent = None
    while end < len(lines):
        line = lines[end]
        if line.strip() == "":
            end += 1
            continue
        if not (line[0] in " \t" or line.startswith("-")):
            break
        item = _ITEM.match(line)
        if indent is None and item is not None:
            indent = item["indent"]
        last = end
        end += 1
    added = [f"{indent if indent is not None else '  '}{line}" for line in entry]
    return newline.join([*lines[: last + 1], *added, *lines[last + 1 :]]) + newline
