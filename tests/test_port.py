"""The port speaks the platform's words and both implementations satisfy it."""

import inspect
import re
from pathlib import Path

from forge import port
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge
from forge.port import Forge

HOST_WORDS = re.compile(
    r"\b(repo|repos|repository|repositories|tag|tags|team|teams|pipeline|pipelines)\b", re.I
)


def test_the_port_names_nothing_of_the_host() -> None:
    source = Path(inspect.getfile(port)).read_text(encoding="utf-8")
    assert HOST_WORDS.search(source) is None


def test_every_operation_is_implemented_by_the_fake() -> None:
    fake = FakeForge()
    missing = [name for name in _operations() if not callable(getattr(fake, name, None))]
    assert missing == []
    assert isinstance(fake, Forge)


def test_the_cache_passes_every_operation_through() -> None:
    cached = CachedForge(FakeForge(), enabled=True)
    missing = [name for name in _operations() if not callable(getattr(cached, name, None))]
    assert missing == []


def _operations() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(Forge)
        if not name.startswith("_") and inspect.isfunction(member)
    ]
