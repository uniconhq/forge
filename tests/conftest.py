"""The package's own test fixtures come from `forge.testing`, the plugin every
dependant loads too. `sibling` finds a file of another Unicon repo checked out
beside this one, for the tests that compare the package's copies with the
originals.
"""

import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

pytest_plugins = ["forge.testing"]

SIBLINGS = Path(__file__).resolve().parents[2]


def sibling(repo: str, *path: str) -> Path:
    """`path` in the repo `repo` beside this one. On a machine without that
    checkout the test is skipped; in CI, which checks every one of them out,
    a missing file fails the test, so a comparison never passes by not
    running.
    """
    found = SIBLINGS.joinpath(repo, *path)
    if not found.exists():
        if os.environ.get("CI"):
            pytest.fail(f"{found} is missing; CI checks {repo} out beside this repo")
        pytest.skip(f"the {repo} repo is not checked out beside this one")
    return found


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """psycopg cannot run asynchronously on Windows' default proactor loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}
