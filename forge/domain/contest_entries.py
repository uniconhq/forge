"""A new task's entry in its contest's `tasks` list, written into the text of
`contest.yaml` so the rest of the file stays as the organisers wrote it,
comments and layout included.

The entry takes the first label of A, B, ..., Z, AA, AB, ... that no other
entry holds, and 100 points. It goes at the end of the list: a `tasks: []`
becomes a list of one, a block list gets one more item at the indent its
items already use, and a file with no `tasks` key gets one at the end. A
list written in flow style with items in it, `tasks: [{id: a, ...}]`, is
not edited, since no line of it can be added to on its own. Whatever is
written is read back and must say exactly what the file said before plus
the one entry; when it does not, nothing is written.
"""

import json
import re

import yaml

from forge.domain.definitions import parse_contest
from forge.domain.yaml_models import load_mapping, load_yaml

POINTS = 100
"""What a new entry is worth, the value the format's own examples use."""

_TASKS_KEY = re.compile(r"tasks:[ \t]*(?P<rest>.*)")
_EMPTY_FLOW = re.compile(r"\[[ \t]*\](?P<after>[ \t]*(#.*)?)")
_ITEM = re.compile(r"(?P<indent>[ \t]*)-[ \t]")


class Unlisted(Exception):
    """The file's `tasks` cannot be added to by an edit of its text, or the
    edit would not read back as the file plus the one entry.
    """


def label_for(taken: set[str]) -> str:
    """The first of A, B, ..., Z, AA, AB, ... not in `taken`."""
    count = 0
    while True:
        label = _letters(count)
        if label not in taken:
            return label
        count += 1


def with_entry(content: bytes, task: str) -> bytes | None:
    """`contest.yaml` with an entry for `task`, or none when it has one
    already. `InvalidDefinition` when the file is not a valid `contest.yaml`;
    `Unlisted` when its `tasks` cannot be added to.
    """
    contest = parse_contest(content)
    if any(entry.id == task for entry in contest.tasks):
        return None
    label = label_for({entry.label for entry in contest.tasks})
    text = content.decode()
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = _inserted(text, _entry_lines(task, label), newline)
    before = load_mapping("contest.yaml", text)
    expected = {
        **before,
        "tasks": [*(before.get("tasks") or []), {"id": task, "label": label, "points": POINTS}],
    }
    if load_mapping("contest.yaml", updated) != expected:
        raise Unlisted("The edit would change more than the tasks list.")
    parse_contest(updated.encode())
    return updated.encode()


def _letters(count: int) -> str:
    label = ""
    count += 1
    while count:
        count, rest = divmod(count - 1, 26)
        label = chr(ord("A") + rest) + label
    return label


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


def _entry_lines(task: str, label: str) -> list[str]:
    """The entry's lines, with the item marker at no indent."""
    return [f"- id: {_scalar(task)}", f"  label: {_scalar(label)}", f"  points: {POINTS}"]


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
