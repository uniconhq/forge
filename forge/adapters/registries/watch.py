"""A resolver held to the cap while it runs: its process is stopped once the
folder it downloads into holds more than the cap."""

import asyncio
import os
from contextlib import suppress
from pathlib import Path

from forge.domain.errors import TooLarge

WATCHED = 0.2
"""How often, in seconds, the folder's size is checked."""


async def watched(
    process: asyncio.subprocess.Process, folder: Path, cap: int, too_large: str
) -> tuple[bytes, bytes]:
    """What the process wrote to its standard output and error once it ends;
    `TooLarge` with `too_large` once `folder` holds more than `cap`. The
    process is killed when it is stopped early."""
    finished = asyncio.ensure_future(process.communicate())
    try:
        while not finished.done():
            await asyncio.wait({finished}, timeout=WATCHED)
            if not finished.done() and await asyncio.to_thread(held, folder) > cap:
                raise TooLarge(too_large)
        return finished.result()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await asyncio.wait({finished})
        raise


def held(folder: Path) -> int:
    """The bytes in the files under a folder, as they are being written."""
    total = 0
    for parent, _, names in os.walk(folder):
        for name in names:
            with suppress(OSError):
                total += os.lstat(os.path.join(parent, name)).st_size
    return total
