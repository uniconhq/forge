"""The front door. Every module under `forge.api` lists its names in
`__all__`, each of them written elsewhere in the package, and none of them is
a building block: a function whose first parameter is a `Context` and which is
not marked `@action`. So a building block cannot reach the backend's list by
mistake.
"""

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from types import ModuleType

import pytest

import forge.api
from forge.context import Context
from forge.services import sessions


def _modules() -> Iterator[ModuleType]:
    yield forge.api
    for found in pkgutil.iter_modules(forge.api.__path__):
        yield importlib.import_module(f"forge.api.{found.name}")


MODULES = list(_modules())


def _is_building_block(value: object) -> bool:
    if not inspect.isfunction(value):
        return False
    parameters = list(inspect.signature(value, follow_wrapped=False).parameters.values())
    return bool(parameters) and parameters[0].annotation is Context


def test_the_check_tells_a_building_block_from_an_action() -> None:
    assert _is_building_block(sessions.create)
    assert not _is_building_block(sessions.revoke)


@pytest.mark.parametrize("module", MODULES, ids=lambda module: module.__name__)
def test_no_building_block_is_on_the_list(module: ModuleType) -> None:
    listed = [name for name in module.__all__ if _is_building_block(getattr(module, name))]
    assert listed == [], f"{module.__name__} lists building blocks: {listed}"


@pytest.mark.parametrize("module", MODULES, ids=lambda module: module.__name__)
def test_every_public_name_is_listed_and_written_elsewhere(module: ModuleType) -> None:
    public = {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and not inspect.ismodule(value)
    }
    assert public == set(module.__all__)
    for name in module.__all__:
        home = getattr(getattr(module, name), "__module__", "")
        assert not home.startswith("forge.api"), f"{module.__name__}.{name} is written here"
