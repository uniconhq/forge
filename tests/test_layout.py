"""The package imports cleanly with every layer present, and both implementations
satisfy the port.
"""

import importlib

import forge
from forge.forges.fake import FakeForge
from forge.port import Forge

LAYERS = [
    "forge.domain",
    "forge.services",
    "forge.db",
    "forge.port",
    "forge.forges",
    "forge.forges.forgejo",
    "forge.forges.fake",
    "forge.log",
    "forge.api",
    "forge.cookies",
    "forge.cli",
]


def test_every_layer_imports() -> None:
    for name in LAYERS:
        assert importlib.import_module(name).__name__ == name


def test_the_version_is_a_release_number() -> None:
    major, minor, patch = forge.__version__.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


def test_the_fake_is_a_forge() -> None:
    assert isinstance(FakeForge(), Forge)
    assert FakeForge().name == "fake"
