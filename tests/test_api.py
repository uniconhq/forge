"""The front door. Every module under `forge.api` lists its names in
`__all__`, each of them written elsewhere in the package, and none of them is
a building block: a function whose first parameter is a `Context` and which is
not marked `@action`. So a building block cannot reach the backend's list by
mistake. Every type a listed function returns, when the package writes it, is
listed too, so the backend can name what it is handed.
"""

import importlib
import inspect
import pkgutil
import typing
from collections.abc import Callable, Iterator
from types import ModuleType, UnionType

import pytest

import forge.api
import forge.api.sign_in
from forge.api import files, publications, types, uploads
from forge.domain.content import Uploaded
from forge.domain.sessions import Session
from forge.runtime.context import Context
from forge.services import files as file_service
from forge.services import sessions
from forge.services import uploads as upload_service


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


def _package_classes(annotation: object) -> set[type]:
    """The classes the package writes that an annotation names, looking
    inside unions and generics.
    """
    found: set[type] = set()
    if isinstance(annotation, type) and annotation.__module__.startswith("forge."):
        found.add(annotation)
    if isinstance(annotation, UnionType) or typing.get_origin(annotation) is not None:
        for argument in typing.get_args(annotation):
            found |= _package_classes(argument)
    return found


def _returned(function: Callable[..., object]) -> set[type]:
    hints = typing.get_type_hints(inspect.unwrap(function))
    return _package_classes(hints.get("return"))


LISTED = {id(getattr(module, name)) for module in MODULES for name in module.__all__}


def test_the_check_finds_the_classes_a_return_names() -> None:
    assert _returned(forge.api.sign_in.complete) == {Session}


@pytest.mark.parametrize("module", MODULES, ids=lambda module: module.__name__)
def test_every_type_a_listed_function_returns_is_listed(module: ModuleType) -> None:
    unlisted = {
        f"{name} returns {returned.__name__}"
        for name in module.__all__
        if callable(value := getattr(module, name)) and not isinstance(value, type)
        for returned in _returned(value)
        if id(returned) not in LISTED
    }
    assert unlisted == set(), f"{module.__name__}: {sorted(unlisted)}"


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


def test_an_organisers_upload_into_a_task_reaches_the_backend() -> None:
    """The slot, the save that takes it and the types they take and return
    are on the list, so the backend can serve feature 8's upload.
    """
    assert files.write_upload is file_service.write_upload
    assert uploads.task_file_slot is upload_service.task_file_slot
    assert uploads.Slot is upload_service.Slot
    assert types.Uploaded is Uploaded
    assert {"Edit", "ConflictToken"} <= set(types.__all__)
    assert {"Published", "Draft"} <= set(publications.__all__)
