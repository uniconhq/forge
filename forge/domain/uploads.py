"""The rules a contestant's upload follows, from the slot it asks for to the
submit that uses it. Files go from the browser straight to the object store
and never through the platform: a file of at most `SINGLE_REQUEST_MAX` bytes
in one form post whose signed policy caps its size at what was declared, and
a larger one in parts of `part_size` bytes, each through a URL signed with
that part's exact length, so no request can carry more than it was signed
for. Once the browser says the bytes are there, the platform measures what
arrived and keeps its size and digest; an upload whose size is not the one
declared is rejected and can never be submitted.

One person holds at most `OPEN_MAX` uploads for a task that no submit has
used, and at most `OPEN_SUBMISSIONS` whole submissions' worth of bytes
declared by them together, so nobody fills the store with slots they never
submit. A rejected upload counts too until the sweep removes it: its object
stays until then, and its form still takes bytes until it expires.

An upload no submit uses is removed, object and row, `LIFETIME` after it was
asked for. The object of one a submit used goes then too, since the
submission's commit holds its bytes, and its row stays as the record of what
was submitted.

A file name is one plain path segment, the name the file is committed under
in `files/<input>/`. `accept` lists what a file input takes: an entry
starting with a dot is an ending of the name, compared ignoring case, and an
entry with a slash a content type, where `image/*` takes every image.
"""

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from forge.domain.content import UNSAFE_CHARACTERS
from forge.domain.workflow_definition import InputType

SINGLE_REQUEST_MAX = 16 * 1024 * 1024
PART_SIZE = 8 * 1024 * 1024
MAX_PARTS = 1000
FORM_TTL = timedelta(minutes=15)
PARTS_TTL = timedelta(hours=2)
LIFETIME = timedelta(days=2)
FILENAME_MAX = 255
CONTENT_TYPE_MAX = 255
OBJECT_PREFIX = "uploads/"
OPEN_MAX = 200
"""The most uploads one person holds for a task before a submit uses them:
several file[] inputs of dozens of files each, sent twice over."""
OPEN_SUBMISSIONS = 2
"""How many whole submissions' worth of bytes one person's open uploads for
a task may declare together: one submission's files and one more try."""

FILE_INPUTS = frozenset({InputType.CODE, InputType.FILE, InputType.FILES})
"""The contestant inputs a file is uploaded for: a code input's source is
uploaded as one file, like a file input's."""


class UploadStatus(StrEnum):
    """Where an upload stands: `presigned` while its bytes are on the way,
    `verified` once they arrived as declared, `rejected` when they did not,
    `consumed` once a submit used it, and `expired` once a submit used it and
    its object is gone, the bytes being in the submission's commit.
    """

    PRESIGNED = "presigned"
    VERIFIED = "verified"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"


OPEN = (UploadStatus.PRESIGNED, UploadStatus.VERIFIED, UploadStatus.REJECTED)
"""The statuses of an upload that counts against its owner's allowance: its
object may be in the store, or its URLs may still put one there, and no
submission holds its bytes."""


def open_bytes(submission_limit: int) -> int:
    """The most bytes one person's open uploads for a task may declare
    together, when a whole submission of it is at most `submission_limit`.
    """
    return OPEN_SUBMISSIONS * submission_limit


def object_key(upload: object) -> str:
    """Where an upload's bytes are kept in the uploads store."""
    return f"{OBJECT_PREFIX}{upload}"


def filename_problem(name: str) -> str | None:
    """What is wrong with `name` as the name of an uploaded file, if anything."""
    if not name or len(name) > FILENAME_MAX:
        return f"A file name is 1 to {FILENAME_MAX} characters."
    if name in (".", "..") or "/" in name:
        return "A file name is one name, with no folder in it."
    if any(character in UNSAFE_CHARACTERS or ord(character) < 32 for character in name):
        return 'A file name has none of \\ ? # % : * " < > | and no control characters.'
    if name != name.strip():
        return "A file name does not start or end with a space."
    return None


def accepts(accept: tuple[str, ...] | None, name: str, content_type: str | None) -> bool:
    """Whether an input whose `accept` is this takes a file of that name and
    content type. An input with no `accept` takes any file.
    """
    if accept is None:
        return True
    lowered = name.lower()
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    for entry in accept:
        wanted = entry.strip().lower()
        if wanted.startswith("."):
            if lowered.endswith(wanted):
                return True
        elif "/" in wanted:
            if wanted.endswith("/*") and kind.startswith(wanted[:-1]):
                return True
            if kind == wanted:
                return True
        elif lowered.endswith(f".{wanted}"):
            return True
    return False


def part_size(size: int) -> int:
    """The size of every part but the last of a file of `size` bytes sent in
    parts: `PART_SIZE`, or more for a file that would otherwise take more
    than `MAX_PARTS` parts, in whole megabytes.
    """
    megabyte = 1024 * 1024
    needed = math.ceil(size / MAX_PARTS)
    return max(PART_SIZE, math.ceil(needed / megabyte) * megabyte)


@dataclass(frozen=True, slots=True)
class Part:
    """One part of a file sent in parts: its number, from 1, and its exact
    length.
    """

    number: int
    length: int


def parts_of(size: int) -> tuple[Part, ...]:
    """The parts a file of `size` bytes is sent in, in order."""
    each = part_size(size)
    count = max(1, math.ceil(size / each))
    return tuple(
        Part(number=index + 1, length=min(each, size - index * each)) for index in range(count)
    )


def in_one_request(size: int) -> bool:
    return size <= SINGLE_REQUEST_MAX
