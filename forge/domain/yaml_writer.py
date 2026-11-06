"""A definition file's settings written from plain values, as a form saves
them, into the file's own text, keeping its comments and its key order.

The file is read with ruamel's round trip, every number keeping the digits it
is written with, and the values are laid over it part by part: a mapping key
by key, new keys after the old; a list item by item, by `id` when every item
has one, so a reordered list moves its items with their comments; anything
else is replaced where it stands, the comment on its line kept. Taking a key
out drops the comment on its line and keeps the comments after it. An
`Ordered` mapping follows the values' own order, and a key that stands where
one the values no longer have stood is renamed in place.

The document is written with ruamel's own formatting, so spacing and list
style may tidy up on the first save. What is written is read back and must
hold exactly the values and every comment kept; when it does not,
`Unwritable` is raised and nothing is written.
"""

import io
import json
import math
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.compat import ordereddict
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.emitter import RoundTripEmitter
from ruamel.yaml.error import CommentMark
from ruamel.yaml.error import YAMLError as RuamelError
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString
from ruamel.yaml.tokens import CommentToken

from forge.domain.yaml_models import TAG, CoreResolver, YAMLError, load_yaml


class Unwritable(Exception):
    """The values cannot be written into the file's text so that it reads
    back as them with every comment kept."""


class Ordered(dict[str, Any]):
    """A mapping whose keys the values put in order: the file's follows it."""


class _Digits(Decimal):
    """A number as the file writes it, which it is written back as."""

    text: str


def _digits(text: str) -> _Digits:
    bare = text.lstrip("+-").lower()
    if bare == ".inf":
        number = _Digits("-Infinity" if text.startswith("-") else "Infinity")
    elif bare == ".nan":
        number = _Digits("NaN")
    else:
        number = _Digits(text)
    number.text = text
    return number


class _Constructor(RoundTripConstructor):
    def construct_digits(self, node: ScalarNode) -> _Digits:
        return _digits(str(node.value))


_Constructor.add_constructor(TAG + "float", _Constructor.construct_digits)


class _Representer(RoundTripRepresenter):
    def represent_digits(self, data: _Digits) -> ScalarNode:
        node: ScalarNode = self.represent_scalar(TAG + "float", data.text)
        return node

    def represent_decimal(self, data: Decimal) -> ScalarNode:
        text = str(data)
        whole = text.lstrip("-").isdigit()
        node: ScalarNode = self.represent_scalar(TAG + ("int" if whole else "float"), text)
        return node


_Representer.add_representer(_Digits, _Representer.represent_digits)
_Representer.add_representer(Decimal, _Representer.represent_decimal)


class _Emitter(RoundTripEmitter):
    """The library's emitter, which after a list's key with a comment on its
    line runs a flow item's line into the next item's; this lets the line
    end."""

    def expect_flow_mapping(self, *args: Any, **kwargs: Any) -> None:
        self.no_newline = False
        super().expect_flow_mapping(*args, **kwargs)

    def expect_flow_sequence(self, *args: Any, **kwargs: Any) -> None:
        self.no_newline = False
        super().expect_flow_sequence(*args, **kwargs)


# How a block list is indented: under its key, or two spaces in.
_LAYOUTS = ((2, 2, 0), (2, 4, 2))


def _yaml(layout: tuple[int, int, int] = _LAYOUTS[0]) -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.Resolver = CoreResolver
    yaml.Constructor = _Constructor
    yaml.Representer = _Representer
    yaml.Emitter = _Emitter
    yaml.width = 1 << 30
    yaml.preserve_quotes = True
    mapping, sequence, offset = layout
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    return yaml


def _load(text: str) -> Any:
    try:
        return _yaml().load(text)
    except RuamelError as error:
        raise Unwritable(str(error)) from None


def _dump(document: Any, layout: tuple[int, int, int]) -> str:
    written = io.StringIO()
    _yaml(layout).dump(document, written)
    return written.getvalue()


def write_values(text: str, values: Mapping[str, Any]) -> str:
    """`text` with `values` as its settings, its comments and key order
    kept. `Unwritable` when the file is not a mapping of settings YAML reads,
    or what is written would not read back as the values or would lose a
    comment.
    """
    if "\r\n" in text:
        return write_values(text.replace("\r\n", "\n"), values).replace("\n", "\r\n")
    try:
        load_yaml(text)
    except YAMLError as error:
        raise Unwritable(str(error)) from None
    document = _load(text) if text.strip() else CommentedMap()
    if document is None:
        document = CommentedMap()
    if not isinstance(document, CommentedMap):
        raise Unwritable("The file does not hold named settings.")
    layout = _layout(text)
    edit = _Edit()
    edit.dropped += _lines(edit.mapping(document, values))
    changed = _dump(document, layout)
    kept = _comments(text) - edit.dropped
    if _holds(changed, values) and not kept - _comments(changed):
        return changed
    raise Unwritable("The values cannot be written without losing part of the file.")


def _layout(text: str) -> tuple[int, int, int]:
    """The list indent the file's own lines are most like."""

    def alike(layout: tuple[int, int, int]) -> int:
        try:
            written = _dump(_load(text), layout).splitlines()
        except Unwritable:
            return 0
        lines = text.splitlines()
        matcher = SequenceMatcher(None, lines, written, autojunk=False)
        return sum(size for _, _, size in matcher.get_matching_blocks())

    return max(_LAYOUTS, key=alike)


def _holds(text: str, values: Mapping[str, Any]) -> bool:
    try:
        read = load_yaml(text)
    except YAMLError:
        return False
    return same(read if read is not None else {}, values)


def _comments(text: str) -> Counter[str]:
    """Every comment line in the file, as its text."""
    found: Counter[str] = Counter()
    if not text.strip():
        return found
    for token in _tokens(_load(text)):
        for line in str(token.value).split("\n"):
            if line.strip().startswith("#"):
                found[line.strip()] += 1
    return found


def _tokens(node: object) -> Iterator[CommentToken]:
    if isinstance(node, CommentToken):
        yield node
    elif isinstance(node, list | tuple) and not isinstance(node, CommentedSeq):
        for item in node:
            yield from _tokens(item)
    if isinstance(node, CommentedMap | CommentedSeq):
        yield from _tokens(node.ca.comment)
        yield from _tokens(list(node.ca.items.values()))
        yield from _tokens(node.ca.end)
        children = node.values() if isinstance(node, CommentedMap) else node
        for child in children:
            yield from _tokens(child)


def same(a: object, b: object) -> bool:
    """Whether two values mean the same: a number by its value, so `1.0` is
    `1` and a number past what a float holds is the float nearest it, and a
    mapping by its keys, in any order.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if _is_number(a) and _is_number(b):
        assert isinstance(a, int | float | Decimal) and isinstance(b, int | float | Decimal)
        return _exact(a) == _exact(b) or _near(a) == _near(b)
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        mine = {_key(key): value for key, value in a.items()}
        theirs = {_key(key): value for key, value in b.items()}
        return mine.keys() == theirs.keys() and all(
            same(value, theirs[key]) for key, value in mine.items()
        )
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is None and b is None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _exact(value: int | float | Decimal) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _near(value: int | float | Decimal) -> float:
    try:
        return float(value)
    except OverflowError:
        return math.inf


def _key(key: object) -> tuple[str, str]:
    """A key as the values name it, its kind kept: the number 1 is not "1"."""
    if isinstance(key, str):
        return ("text", key)
    if isinstance(key, bool):
        return ("bool", str(key).lower())
    if _is_number(key):
        assert isinstance(key, int | float | Decimal)
        return ("number", str(_exact(key).normalize()))
    return ("other", repr(key))


def _fresh(value: object) -> Any:
    """A new part of the file for a value: a mapping or list of plain values
    on one line, anything else as a block.
    """
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, Mapping):
        made = CommentedMap()
        for key, item in value.items():
            made[key] = _fresh(item)
        if all(_flat(item) for item in value.values()):
            made.fa.set_flow_style()
        else:
            made.fa.set_block_style()
        return made
    if isinstance(value, list | tuple):
        seq = CommentedSeq(_fresh(item) for item in value)
        if all(_flat(item) for item in value):
            seq.fa.set_flow_style()
        else:
            seq.fa.set_block_style()
        return seq
    return value


def _flat(value: object) -> bool:
    if isinstance(value, Mapping):
        return False
    if isinstance(value, list | tuple):
        return all(not isinstance(item, Mapping | list | tuple) for item in value)
    return True


Slot = tuple[CommentedMap | CommentedSeq, Any]


class _Edit:
    """One laying of values over a document, with the comment lines it took
    out with the parts it took out."""

    def __init__(self) -> None:
        self.dropped: Counter[str] = Counter()

    def mapping(self, node: CommentedMap, values: Mapping[str, Any]) -> str:
        """Lays `values` over `node`; the lines after its end stay at its
        end, or are given back when it ends on no line of its own."""
        tail = _take_tail(node)
        self._mapping(node, values)
        return _put_tail(node, tail)

    def sequence(self, node: CommentedSeq, values: Sequence[Any]) -> str:
        tail = _take_tail(node)
        self._sequence(node, values)
        return _put_tail(node, tail)

    def _mapping(self, node: CommentedMap, values: Mapping[str, Any]) -> None:
        if isinstance(values, Ordered):
            self._rename(node, values)
        named = {_key(key): key for key in node}
        wanted = {_key(key) for key in values}
        for key in list(node):
            if _key(key) not in wanted:
                self._take_out(node, key)
        if len(node) == 0 and values and node.fa.flow_style():
            node.fa.set_block_style()
        for key, value in values.items():
            old = named.get(_key(key))
            if old is not None and old in node:
                self._put(node, old, value)
            else:
                node[key] = _fresh(value)
        if isinstance(values, Ordered):
            order = [named.get(_key(key), key) for key in values]
            for key in order:
                if key in node:
                    node.move_to_end(key)

    def _rename(self, node: CommentedMap, values: Ordered) -> None:
        """A key the values no longer have, renamed in place to a new key
        that holds the same value, or, when one key alone went and one came,
        to that one, keeping its place and comments."""
        wanted = {_key(key) for key in values}
        present = {_key(key) for key in node}
        gone = [key for key in node if _key(key) not in wanted]
        added = [key for key in values if _key(key) not in present]
        pairs: list[tuple[Any, Any]] = []
        for key in added:
            match = next((old for old in gone if same(node[old], values[key])), None)
            if match is not None:
                gone.remove(match)
                pairs.append((match, key))
        rest = [key for key in added if key not in {new for _, new in pairs}]
        if len(gone) == 1 and len(rest) == 1:
            pairs.append((gone[0], rest[0]))
        for old, key in pairs:
            at = list(node).index(old)
            value = node[old]
            comment = node.ca.items.pop(old, None)
            del node[old]
            node.insert(at, key, value)
            if comment is not None:
                node.ca.items[key] = _shifted(comment, len(str(key)) - len(str(old)))

    def _sequence(self, node: CommentedSeq, values: Sequence[Any]) -> None:
        if len(node) == 0 and values and node.fa.flow_style():
            node.fa.set_block_style()
        ids = _ids(node), _ids(values)
        if ids[0] is not None and ids[1] is not None and ids[0] != ids[1]:
            self._by_id(node, values, ids[0], ids[1])
            return
        # Items are matched by what they hold, so taking one out or putting
        # one in leaves the others with their own comments.
        matcher = SequenceMatcher(
            None,
            [_canonical(item) for item in node],
            [_canonical(v) for v in values],
            autojunk=False,
        )
        for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()):
            if tag == "equal":
                for at in range(i2 - i1):
                    self._put(node, i1 + at, values[j1 + at])
                continue
            paired = min(i2 - i1, j2 - j1)
            for at in range(i2 - 1, i1 + paired - 1, -1):
                self._take_out(node, at)
            for at in range(j2 - 1, j1 + paired - 1, -1):
                node.insert(i1 + paired, _fresh(values[at]))
            for at in range(paired):
                self._put(node, i1 + at, values[j1 + at])

    def _by_id(
        self, node: CommentedSeq, values: Sequence[Any], old: list[str], new: list[str]
    ) -> None:
        for at in range(len(old) - 1, -1, -1):
            if old[at] not in new:
                self._take_out(node, at)
                del old[at]
        items = [(node[at], node.ca.items.get(at)) for at in range(len(node))]
        held = dict(zip(old, items, strict=True))
        list.clear(node)
        node.ca.items.clear()
        for at, (key, value) in enumerate(zip(new, values, strict=True)):
            if key in held:
                item, comment = held[key]
                list.append(node, item)
                if comment is not None:
                    node.ca.items[at] = comment
                self._put(node, at, value)
            else:
                list.append(node, _fresh(value))

    def _put(self, parent: CommentedMap | CommentedSeq, key: Any, value: object) -> None:
        old = parent[key]
        if same(old, value) and not _reordered(old, value):
            return
        if isinstance(old, CommentedMap) and isinstance(value, Mapping):
            _append((parent, key), self.mapping(old, value))
        elif isinstance(old, CommentedSeq) and isinstance(value, list | tuple):
            _append((parent, key), self.sequence(old, value))
        else:
            if isinstance(old, CommentedMap | CommentedSeq):
                self.dropped += _inside(old)
            if _block(old) and isinstance(value, str) and "\n" in value:
                parent[key] = type(old)(value)
                return
            if _block(old):
                # Set past the library's own setter, which keeps a block's style.
                if isinstance(parent, CommentedSeq):
                    list.__setitem__(parent, key, _fresh(value))
                else:
                    ordereddict.__setitem__(parent, key, _fresh(value))
                _after_block(parent, key)
            else:
                parent[key] = _fresh(value)

    def _take_out(self, parent: CommentedMap | CommentedSeq, key: Any) -> None:
        """Takes the part at `key` out with the comments in it and on its
        line, the comment lines after it kept after what stood before it.
        """
        value = parent[key]
        after = _split_after(_last_slot(parent, key))
        self.dropped += _slot_comments(parent, key) + _inside(value)
        keys = list(parent) if isinstance(parent, CommentedMap) else list(range(len(parent)))
        at = keys.index(key)
        if isinstance(parent, CommentedMap):
            parent.ca.items.pop(key, None)
            del parent[key]
        else:
            del parent[key]
        if not after:
            return
        if at > 0:
            _append(_last_slot(parent, keys[at - 1]), after)
        elif len(parent) > 0:
            first_key = next(iter(parent)) if isinstance(parent, CommentedMap) else 0
            _prepend(parent, first_key, after)
        else:
            self.dropped += _lines(after)


def _shifted(comment: list[Any], shift: int) -> list[Any]:
    """A key's comments with the one on its line moved by `shift` columns,
    so a renamed key keeps the gap before it."""
    token = comment[2] if len(comment) > 2 else None
    if not isinstance(token, CommentToken) or str(token.value).startswith("\n") or not shift:
        return comment
    column = max(0, token.start_mark.column + shift)
    return [*comment[:2], CommentToken(str(token.value), CommentMark(column)), *comment[3:]]


def _block(value: object) -> bool:
    return isinstance(value, LiteralScalarString | FoldedScalarString)


def _after_block(parent: CommentedMap | CommentedSeq, key: Any) -> None:
    """The comment lines after a block scalar, kept after what replaces it:
    a block's slot holds them with no line of its own before them."""
    token = _slot_token((parent, key))
    if token is not None:
        _set_slot_token((parent, key), CommentToken("\n" + str(token.value), token.start_mark))


def _canonical(value: object) -> str:
    """A value as one text, the same for values that are `same`."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return json.dumps(value)
    if _is_number(value):
        assert isinstance(value, int | float | Decimal)
        return "n:" + repr(_near(value))
    if isinstance(value, Mapping):
        items = sorted((repr(_key(key)), _canonical(item)) for key, item in value.items())
        return "{" + ",".join(f"{key}:{item}" for key, item in items) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return repr(value)


def _ids(items: Sequence[Any]) -> list[str] | None:
    """Each item's `id`, when every item is a mapping with a text one, all
    different."""
    found: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            return None
        found.append(item["id"])
    return found if len(set(found)) == len(found) else None


def _reordered(old: object, value: object) -> bool:
    """Whether an `Ordered` mapping, here or inside, puts its keys in
    another order than the file."""
    if isinstance(old, CommentedMap) and isinstance(value, Mapping):
        if isinstance(value, Ordered) and [_key(k) for k in old] != [_key(k) for k in value]:
            return True
        named = {_key(key): key for key in old}
        return any(
            _reordered(old[named[_key(key)]], item)
            for key, item in value.items()
            if _key(key) in named
        )
    if isinstance(old, CommentedSeq) and isinstance(value, list | tuple):
        return any(_reordered(a, b) for a, b in zip(old, value, strict=False))
    return False


def _last_slot(parent: CommentedMap | CommentedSeq, key: Any) -> Slot:
    """Where the comments after the part at `key` are held: the slot of its
    last line."""
    value = parent[key]
    if isinstance(value, CommentedMap) and len(value) and not value.fa.flow_style():
        return _last_slot(value, list(value)[-1])
    if isinstance(value, CommentedSeq) and len(value) and not value.fa.flow_style():
        return _last_slot(value, len(value) - 1)
    return parent, key


def _slot_index(parent: CommentedMap | CommentedSeq) -> int:
    return 2 if isinstance(parent, CommentedMap) else 0


def _slot_token(slot: Slot) -> CommentToken | None:
    parent, key = slot
    entry = parent.ca.items.get(key)
    if entry is None:
        return None
    token = entry[_slot_index(parent)]
    return token if isinstance(token, CommentToken) else None


def _set_slot_token(slot: Slot, token: CommentToken | None) -> None:
    parent, key = slot
    entry = parent.ca.items.setdefault(key, [None, None, None, None])
    entry[_slot_index(parent)] = token


def _append(slot: Slot, after: str) -> None:
    if not after:
        return
    token = _slot_token(slot)
    if token is not None:
        _set_slot_token(slot, CommentToken(str(token.value) + after, token.start_mark))
        return
    # A block scalar's slot holds the lines after it with no line end before them.
    start = "" if _block(slot[0][slot[1]]) else "\n"
    _set_slot_token(slot, CommentToken(start + after, CommentMark(0)))


def _split_after(slot: Slot) -> str:
    """Takes the lines after the slot's own line out of it and gives them."""
    token = _slot_token(slot)
    if token is None:
        return ""
    if _block(slot[0][slot[1]]):
        _set_slot_token(slot, None)
        return str(token.value)
    first, _, after = str(token.value).partition("\n")
    _set_slot_token(slot, CommentToken(first + "\n", token.start_mark) if first else None)
    return after


def _end_slot(node: CommentedMap | CommentedSeq) -> Slot | None:
    """The slot of a block mapping's or list's last line."""
    if not len(node) or node.fa.flow_style():
        return None
    return _last_slot(node, list(node)[-1] if isinstance(node, CommentedMap) else len(node) - 1)


def _take_tail(node: CommentedMap | CommentedSeq) -> str:
    slot = _end_slot(node)
    return _split_after(slot) if slot is not None else ""


def _put_tail(node: CommentedMap | CommentedSeq, tail: str) -> str:
    """The lines after a part put back after its new last line; given back
    when it has none of its own."""
    if not len(node):
        node.fa.set_flow_style()
    slot = _end_slot(node)
    if slot is None:
        return tail
    _append(slot, tail)
    return ""


def _prepend(parent: CommentedMap | CommentedSeq, key: Any, after: str) -> None:
    """Comment lines put before the first part of `parent`."""
    entry = parent.ca.items.setdefault(key, [None, None, None, None])
    lines = [CommentToken(line + "\n", CommentMark(0)) for line in after.split("\n") if line]
    entry[1] = [*lines, *(entry[1] or [])]


def _lines(text: str) -> Counter[str]:
    return Counter(line.strip() for line in text.split("\n") if line.strip().startswith("#"))


def _slot_comments(parent: CommentedMap | CommentedSeq, key: Any) -> Counter[str]:
    found: Counter[str] = Counter()
    for token in _tokens(parent.ca.items.get(key)):
        found += _lines(str(token.value))
    return found


def _inside(value: object) -> Counter[str]:
    found: Counter[str] = Counter()
    if isinstance(value, CommentedMap | CommentedSeq):
        for token in _tokens(value):
            found += _lines(str(token.value))
    return found
