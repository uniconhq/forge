"""A new task's entry in its contest's `tasks` list, written into the text of
`contest.yaml` so the rest of the file stays as the organisers wrote it,
comments and layout included.

The entry is `{id: <task>}`: the task's label is its place in the list
(TASK-FORMAT.md section 1.1). In a contest already published, the entry also
sets `release_at` and `closes` at the contest's `end`, so work in progress
shows nothing until the organisers move them. It goes at the end of the list: a `tasks: []`
becomes a list of one, a block list gets one more item at the indent its
items already use, and a file with no `tasks` key gets one at the end. A
list written in flow style with items in it, `tasks: [{id: a, ...}]`, is
not edited, since no line of it can be added to on its own. Whatever is
written is read back and must say exactly what the file said before plus
the one entry; when it does not, nothing is written.

The same holds for taking a task out (#450): `without_entry` takes its
entry's lines out of the list, and `with_withdrawn` adds `withdrawn: true`
as the entry's last line, or inside its braces when it is written on one
line, or takes that key out again to put the task back. Each edit is read
back and must say exactly what the file said with that one change.
"""

import json
import re
from collections.abc import Sequence

from forge.domain.definitions import ContestTask, State, parse_contest, stamp
from forge.domain.yaml_models import YAMLError, load_mapping, load_yaml

_TASKS_KEY = re.compile(r"tasks:[ \t]*(?P<rest>.*)")
_EMPTY_FLOW = re.compile(r"\[[ \t]*\](?P<after>[ \t]*(#.*)?)")
_ITEM = re.compile(r"(?P<indent>[ \t]*)-[ \t]")


class Unlisted(Exception):
    """The file's `tasks` cannot be added to by an edit of its text, or the
    edit would not read back as the file plus the one entry.
    """


class NoEntry(Unlisted):
    """The file's `tasks` has no entry for the task to edit."""


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
        entry["release_at"] = entry["closes"] = stamp(contest.end)
        lines += [f"  release_at: {stamp(contest.end)}", f"  closes: {stamp(contest.end)}"]
    text = content.decode()
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = _inserted(text, lines, newline)
    before = load_mapping("contest.yaml", text)
    expected = {**before, "tasks": [*(before.get("tasks") or []), entry]}
    if load_mapping("contest.yaml", updated) != expected:
        raise Unlisted("The edit would change more than the tasks list.")
    parse_contest(updated.encode())
    return updated.encode()


def _scalar(value: str) -> str:
    """`value` as YAML reads it back as the same string: plain when it is,
    in double quotes otherwise, which keeps a name like `no` or `123` a
    string.
    """
    try:
        plain = load_yaml(value) == value
    except YAMLError:
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


def without_entry(content: bytes, task: str) -> bytes | None:
    """`contest.yaml` with the entry for `task` taken out of its `tasks`, its
    own lines and nothing else, so the entries after it move up; none when
    it has no entry. A list left empty is written `tasks: []`.
    `InvalidDefinition` when the file is not a valid `contest.yaml`, or
    would not be without the entry; `Unlisted` when the entry's lines
    cannot be told apart. A leaderboard reading the task is refused by the
    caller, which checks the leaderboards (`forge.services.taking_out`).
    """
    contest = parse_contest(content)
    index = _index(contest.tasks, task)
    if index is None:
        return None
    text = content.decode()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    key, after, spans = _items(lines, len(contest.tasks))
    first, last = spans[index]
    if len(spans) == 1:
        comment = after.strip()
        head = "tasks: []" + (f" {comment}" if comment else "")
        kept = [*lines[:key], head, *lines[key + 1 : first], *lines[last + 1 :]]
    else:
        kept = [*lines[:first], *lines[last + 1 :]]
    updated = newline.join(kept) + newline
    before = load_mapping("contest.yaml", text)
    listed = [each for at, each in enumerate(before["tasks"]) if at != index]
    if load_mapping("contest.yaml", updated) != {**before, "tasks": listed}:
        raise Unlisted("The edit would change more than the one entry.")
    parse_contest(updated.encode())
    return updated.encode()


def with_withdrawn(content: bytes, task: str, withdrawn: bool) -> bytes | None:
    """`contest.yaml` with the entry for `task` marked `withdrawn: true`, or,
    not `withdrawn`, with that key taken out of it, its other lines as they
    were; none when the entry says so already. `InvalidDefinition` when the
    file is not a valid `contest.yaml`; `Unlisted` when it has no entry for
    the task, or the entry cannot be edited line by line.
    """
    contest = parse_contest(content)
    index = _index(contest.tasks, task)
    if index is None:
        raise NoEntry("The contest has no entry for the task.")
    if contest.tasks[index].withdrawn == withdrawn:
        return None
    text = content.decode()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    _, _, spans = _items(lines, len(contest.tasks))
    first, last = spans[index]
    head = lines[first]
    item = _ITEM.match(head)
    assert item is not None
    rest = head[item.end() :]
    if rest.lstrip().startswith("{"):
        if first != last:
            raise Unlisted("The entry is a mapping written over more than one line.")
        edited = [_flow_withdrawn(head, withdrawn)]
    else:
        if _WITHDRAWN.match(rest.lstrip()):
            raise Unlisted("The entry starts with its withdrawn key.")
        indent = " " * item.end()
        edited = [head] + [
            line
            for line in lines[first + 1 : last + 1]
            if not (line.startswith(indent) and _WITHDRAWN.match(line[len(indent) :]))
        ]
        if withdrawn:
            edited.append(f"{indent}withdrawn: true")
    updated = newline.join([*lines[:first], *edited, *lines[last + 1 :]]) + newline
    before = load_mapping("contest.yaml", text)
    entries = [dict(each) for each in before["tasks"]]
    entries[index].pop("withdrawn", None)
    if withdrawn:
        entries[index]["withdrawn"] = True
    if load_mapping("contest.yaml", updated) != {**before, "tasks": entries}:
        raise Unlisted("The edit would change more than the one entry.")
    parse_contest(updated.encode())
    return updated.encode()


_WITHDRAWN = re.compile(r"withdrawn[ \t]*:")
_FLOW_ITEM = re.compile(r"(?P<head>[^{]*\{)(?P<body>[^{}]*)\}(?P<tail>[ \t]*(#.*)?)")


def _index(entries: Sequence[ContestTask], task: str) -> int | None:
    return next((at for at, entry in enumerate(entries) if entry.id == task), None)


def _items(lines: list[str], count: int) -> tuple[int, str, list[tuple[int, int]]]:
    """The line of the `tasks` key, what follows it on that line, and each
    item of its block list as its first and last line, a comment or a blank
    line between two items belonging to neither. `Unlisted` unless the list
    is written one item per line and has `count` items.
    """
    keys = [
        (index, found["rest"])
        for index, line in enumerate(lines)
        if (found := _TASKS_KEY.fullmatch(line)) is not None
    ]
    if len(keys) != 1:
        raise Unlisted("The file does not have exactly one tasks key.")
    ((key, after),) = keys
    if after.strip() and not after.lstrip().startswith("#"):
        raise Unlisted("The tasks list is not written one item per line.")
    spans: list[list[int]] = []
    indent = None
    for index in range(key + 1, len(lines)):
        line = lines[index]
        if line.strip() == "":
            continue
        if not (line[0] in " \t" or line.startswith("-")):
            break
        item = _ITEM.match(line)
        if indent is None and item is not None:
            indent = item["indent"]
        if item is not None and item["indent"] == indent:
            spans.append([index, index])
        elif spans and not line.lstrip().startswith("#"):
            spans[-1][1] = index
    if len(spans) != count:
        raise Unlisted("The tasks list's items cannot be told apart line by line.")
    return key, after, [(first, last) for first, last in spans]


def _flow_withdrawn(line: str, withdrawn: bool) -> str:
    """An entry written `- {id: a, ...}` on one line, with `withdrawn: true`
    added at its end, or taken out.
    """
    found = _FLOW_ITEM.fullmatch(line)
    if found is None:
        raise Unlisted("The entry is not a mapping written on one line.")
    parts = [part.strip() for part in found["body"].split(",") if part.strip()]
    parts = [part for part in parts if not _WITHDRAWN.match(part)]
    if withdrawn:
        parts.append("withdrawn: true")
    return f"{found['head']}{', '.join(parts)}}}{found['tail']}"
