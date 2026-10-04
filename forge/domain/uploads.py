"""The rules a file follows from the slot it is asked for to the commit that
holds it. Files never pass through the platform: the browser works out the
file's SHA-256, asks for a slot, and sends the bytes through the upload door
to the forge's large-file store, which hashes them as it writes and refuses
anything that is not what the slot declared. The platform then asks the forge
whether the place holds that object *at that length*, measured and not taken
on trust (`port.uploads`), and the commit carries a pointer to it
(`pointer_text`).

One person holds at most `OPEN_MAX` uploads for a task that no submit has
used, and at most `OPEN_SUBMISSIONS` whole submissions' worth of bytes
declared by them together, so nobody fills the store with slots they never
submit.

An upload no submit uses loses its row once `LIFETIME` has passed since it
was asked for, the next time its owner asks for a slot; that frees the
person's allowance. Its bytes are the forge's to collect, which it does once
no commit names them (`[cron.gc_lfs]`), about a week later. An upload a
submit used keeps its row as the record of what was submitted, and its bytes
belong to the commit from then on.

A file name is one plain path segment, the name the file is committed under
in `files/<input>/`. `accept` lists what a file input takes: an entry
starting with a dot is an ending of the name, compared ignoring case, and an
entry with a slash a content type, where `image/*` takes every image.

Content the platform writes from someone's typed text is never a pointer,
and no place it writes carries its own rules about which of its files are
large ones (`refuse_pointer`). A pointer names bytes by their hash alone,
and a grading machine's checkout serves one from the org's shared store
without asking the forge, so text that happens to parse as a pointer would
read another run's file. Every pointer in every place is one the platform
wrote itself, for an object the forge confirmed that place holds.
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from forge.domain.content import UNSAFE_CHARACTERS
from forge.domain.errors import InvalidInputs
from forge.domain.workflow_definition import InputType

LIFETIME = timedelta(days=2)
FILENAME_MAX = 255
CONTENT_TYPE_MAX = 255
OPEN_MAX = 200
"""The most uploads one person holds for a task before a submit uses them:
several file[] inputs of dozens of files each, sent twice over."""
OPEN_SUBMISSIONS = 2
"""How many whole submissions' worth of bytes one person's open uploads for
a task may declare together: one submission's files and one more try."""

DIGEST_CHARACTERS = frozenset("0123456789abcdef")
DIGEST_LENGTH = 64

POINTER_V1 = "https://git-lfs.github.com/spec/v1"
POINTER_LEGACY = "https://hawser.github.com/spec/v1"
POINTER_GIT_MEDIA = "http://git-media.io/v/2"
POINTER_MARK = re.compile(rb"git-media|hawser|git-lfs")
"""What git-lfs wants to see in a blob's first bytes before it parses them."""
POINTER_MAX = 1024
"""How much of a blob git-lfs reads when it asks whether it is a pointer:
the first this many bytes, of a blob of any length."""

FILE_INPUTS = frozenset({InputType.CODE, InputType.FILE, InputType.FILES})
"""The contestant inputs a file is uploaded for: a code input's source is
uploaded as one file, like a file input's."""


class UploadStatus(StrEnum):
    """Where an upload stands: `waiting` until the forge holds its bytes,
    `verified` once the forge says it does, and `consumed` once a commit
    holds the pointer to it.
    """

    WAITING = "waiting"
    VERIFIED = "verified"
    CONSUMED = "consumed"


OPEN = (UploadStatus.WAITING, UploadStatus.VERIFIED)
"""The statuses of an upload that counts against its owner's allowance: its
bytes may be on the way or already at the forge, and no commit holds them."""


class UploadPurpose(StrEnum):
    """What an upload is for, which decides the place its bytes go into: a
    contestant's submission repository, or the task's own repository.
    """

    SUBMISSION = "submission"
    TASK_FILE = "task_file"


def open_bytes(submission_limit: int) -> int:
    """The most bytes one person's open uploads for a task may declare
    together, when a whole submission of it is at most `submission_limit`.
    """
    return OPEN_SUBMISSIONS * submission_limit


def pointer_text(digest: str, size: int) -> bytes:
    """The file a commit holds in place of an object's bytes: the git-lfs
    pointer naming its SHA-256 and its length. Three lines in this order
    with a trailing newline, which is the whole of the format.
    """
    return f"version {POINTER_V1}\noid sha256:{digest}\nsize {size}\n".encode()


def is_pointer(content: bytes) -> bool:
    """Whether git-lfs could read this content as a pointer rather than as
    the file itself. git-lfs reads the first `POINTER_MAX` bytes of a blob
    of any length, wants one of its marks in them, trims the space around
    them and parses what is left; this does the same up to the first line
    and stops there. Whatever follows a line that opens a pointer counts,
    because what matters is what a checkout would do with it and not
    whether it is well formed.
    """
    head = content[:POINTER_MAX]
    if not POINTER_MARK.search(head):
        return False
    first = head.strip().split(b"\n", 1)[0].strip()
    specs = (POINTER_V1, POINTER_LEGACY, POINTER_GIT_MEDIA)
    return any(first == f"version {spec}".encode() for spec in specs)


def read_pointer(content: bytes) -> tuple[str, int] | None:
    """The digest and size a pointer names, or none when this is not one the
    platform wrote. Only the exact three lines `pointer_text` writes are
    read back; `is_pointer` is the wider test, for refusing content that a
    checkout would act on.
    """
    if not is_pointer(content):
        return None
    lines = content.decode("utf-8", "replace").splitlines()
    if len(lines) != 3 or not lines[1].startswith("oid sha256:"):
        return None
    digest = lines[1].removeprefix("oid sha256:")
    size = lines[2].removeprefix("size ")
    if digest_problem(digest) is not None or not size.isdigit():
        return None
    return digest, int(size)


ATTRIBUTES_FILE = ".gitattributes"
"""The file git reads a place's own rules about its files from. Nothing the
platform writes may carry one: the forge's file API applies a place's
attributes to everything written through it, so a pointer committed under an
`lfs` rule is stored as a second object and the commit points at the pointer,
which reads back as 130 bytes of text instead of the file. What tells a
checkout that a pointer is one lives in the image that does the checking out
(`findings/upload-door-test.md` section 7)."""


def refuse_pointer(path: str, content: bytes) -> None:
    """Refuse content someone typed that a checkout would not read as itself:
    a large-file pointer, or a file of the rules that decide what is one.
    `InvalidInputs`, naming the path, or nothing at all.
    """
    if path == ATTRIBUTES_FILE or path.endswith(f"/{ATTRIBUTES_FILE}"):
        problem = (
            "A place the platform writes cannot carry its own file rules; "
            "upload a file and it is kept as one."
        )
    elif is_pointer(content):
        problem = "A file written here cannot be a large-file pointer; upload it instead."
    else:
        return
    raise InvalidInputs(problem, errors=[{"input": path, "message": problem}])


def digest_problem(digest: str) -> str | None:
    """What is wrong with `digest` as a file's SHA-256, if anything."""
    if not isinstance(digest, str) or len(digest) != DIGEST_LENGTH:
        return f"A digest is {DIGEST_LENGTH} characters of lowercase hex."
    if any(character not in DIGEST_CHARACTERS for character in digest):
        return "A digest is lowercase hex, 0 to 9 and a to f."
    return None


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
    if name == ATTRIBUTES_FILE:
        return f"A file cannot be named {ATTRIBUTES_FILE}: it would decide how its folder is read."
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


@dataclass(frozen=True, slots=True)
class Door:
    """Where one upload's bytes go at the forge, and what to present there.
    The proxy puts the browser's body through `path` with `authorization`
    in place of whatever the browser sent, so neither reaches the browser.
    """

    path: str
    authorization: str
