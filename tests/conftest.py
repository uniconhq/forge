"""The package's own test fixtures come from `forge.testing`, the plugin every
dependant loads too."""

import asyncio
import sys
from collections.abc import Callable, Mapping

import pytest

pytest_plugins = ["forge.testing"]


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """psycopg cannot run asynchronously on Windows' default proactor loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}
