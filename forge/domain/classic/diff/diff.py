"""The diff script of unicon/classic@v4: does a run's output match the answer?

exec runs it with the run's `output` and the test's `answer` placed in its
working directory, and `program.yaml` beside this file as its manifest. It
reports through the folder exec names in `UNICON_REPORT`: `verdict` 0 and
`score` 1 when the two match, and `verdict` 1 when they do not, so a wrong
answer's score counts as 0, the worst its bounds allow.

Two files match when they hold the same words in the same order, a word
being a run of bytes between spaces, tabs, carriage returns and line feeds,
as testlib's `wcmp` and most judges compare. So an answer is not wrong for
its line breaks or its spacing: leading, trailing and repeated whitespace
and blank lines anywhere do not count, and an answer split across lines
matches one on a single line. A vertical tab or form feed is part of a
word, as in testlib. A task that wants exact lines brings a checker of its
own.

The files are compared as bytes, in blocks, so no text encoding is assumed
and the time is linear in their size: each is read as its words one space
apart, and the two are compared block by block, so the memory is a few
blocks however long a word is: a 32 MB output of one-digit lines against
a 48 MB answer took 0.3 s and 34 MB on a laptop, the interpreter included,
and two 32 MB files of one word each 16 MB. The step needs a memory limit
of 128 MB or more, which leaves room.

A file that is missing or cannot be read ends the script with an error, as a
program that cannot judge does: exec makes that a fault for staff, never a
score.
"""

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

BLOCK = 1 << 18
SPACE = frozenset(b" \t\r\n")
"""The bytes words are split on."""
NOT_SPACE = (b"\x0b", b"\x0c")
"""The bytes `bytes.split` splits on that are part of a word here."""
WORD = re.compile(rb"[^ \t\r\n]+")


def main() -> int:
    """Compare `output` with `answer` and write the report."""
    report = Path(os.environ["UNICON_REPORT"])
    if same(Path("output"), Path("answer")):
        (report / "score").write_text("1\n")
        (report / "verdict").write_text("0\n")
    else:
        (report / "verdict").write_text("1\n")
    return 0


def same(actual: Path, expected: Path) -> bool:
    """Whether two files hold the same words in the same order.

    Each file is read as its words one space apart (see `spaced`), and the
    two streams are compared as far as the shorter block goes, the rest kept
    for the next. Once one stream runs out, the other must have run out too.
    """
    with actual.open("rb") as left, expected.open("rb") as right:
        ours, theirs = spaced(left), spaced(right)
        a = b = b""
        while True:
            a = a or next(ours, b"")
            b = b or next(theirs, b"")
            if not a or not b:
                return not a and not b
            length = min(len(a), len(b))
            if a[:length] != b[:length]:
                return False
            a, b = a[length:], b[length:]


def spaced(stream: BinaryIO) -> Iterator[bytes]:
    """The stream's words, each pair one space apart, with nothing before
    the first or after the last, in non-empty blocks.

    Each block read gives its words one space apart, and one more space
    before them when whitespace parts them from the word before, at the end
    of the last block with a word or the start of this one. A word that runs
    over the end of a block goes on in the next block's first, with nothing
    between.
    """
    started = parted = False
    for block in iter(lambda: stream.read(BLOCK), b""):
        found = split(block)
        if not found:
            parted = True
            continue
        joined = b" ".join(found)
        if started and (parted or block[0] in SPACE):
            joined = b" " + joined
        yield joined
        started = True
        parted = block[-1] in SPACE


def split(block: bytes) -> list[bytes]:
    """The words of one block, at the speed of `bytes.split` unless the block
    holds a byte that splits there but not here."""
    if any(byte in block for byte in NOT_SPACE):
        return WORD.findall(block)
    return block.split()


if __name__ == "__main__":
    sys.exit(main())
