"""What a tree of blocks reads, walked the same way by every caller.

A block is a mapping that holds `block`. Its inputs are every other value it
holds. A mapping without `block` is a leaf, and a walk yields it rather than
stepping into it.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any


def leaves(
    tree: object, *, into: Callable[[Mapping[Any, Any]], bool] | None = None
) -> Iterator[Mapping[Any, Any]]:
    """Each mapping in `tree` that is not a block.

    Lists are walked, and so is every input of a block. When `into` is given
    and `into(block)` is false, that block is yielded and not walked.
    """
    waiting: list[object] = [tree]
    while waiting:
        node = waiting.pop()
        if isinstance(node, list):
            waiting.extend(node)
            continue
        if not isinstance(node, Mapping):
            continue
        if "block" in node:
            if into is not None and not into(node):
                yield node
                continue
            waiting.extend(given for key, given in node.items() if key != "block")
            continue
        yield node


def reached(start: Iterable[str], edges: Mapping[str, Iterable[str]]) -> set[str]:
    """Every name reachable from `start` through `edges`, `start` included.

    `edges` maps a name to the names it leads to. A name it does not hold
    leads nowhere, and a name reached twice is walked once.
    """
    found: set[str] = set()
    waiting = list(start)
    while waiting:
        current = waiting.pop()
        if current in found:
            continue
        found.add(current)
        waiting.extend(edges.get(current, ()))
    return found
