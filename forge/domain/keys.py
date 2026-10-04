"""The keys orgs, contests and tasks are filed under. A key is made once,
when the thing is asked for, and never changes or names anything else: a
rename changes the name, never the key, and a name freed and taken again
gets a new one. Everything the platform stores, and every name it gives a
thing at the forge, is built from keys; the names people give things live
in the `names` table and are read only to find a key and to show one.

A key is a UUID v7 in lower-case base32, 26 characters of `a-z` and `2-7`,
which is short enough to sit in the forge's repository and team names and
uses only characters those allow. `key_from_name` is for tests, which file
a thing under its name so their ids read as the names they were made with.
"""

import base64
import uuid
from collections.abc import Callable

KeyMaker = Callable[[str], str]
"""What makes the key for a thing being named: given the name, a new key."""


def random_key(name: str) -> str:
    """A new key, whatever the name: the one the platform runs with."""
    return base64.b32encode(uuid.uuid7().bytes).decode().rstrip("=").lower()


def key_from_name(name: str) -> str:
    """The name itself, for tests."""
    return name
