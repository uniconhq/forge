"""The port speaks the platform's words, every implementation satisfies every
area, and every error code is unique.
"""

import inspect
import re
from pathlib import Path

import pytest

from forge import port
from forge.domain import errors
from forge.domain.errors import UniconError
from forge.forges.cached import CachedForge
from forge.forges.fake import FakeForge
from forge.port import Forge

HOST_WORDS = re.compile(
    r"\b(repo|repos|repository|repositories|tag|tags|team|teams|pipeline|pipelines"
    r"|issue|issues|collaborator|collaborators|branch|branches|webhook|topic)\b",
    re.I,
)

AREAS = {
    "identity": port.IdentityPort,
    "orgs": port.OrgPort,
    "content": port.ContentPort,
    "workspaces": port.WorkspacePort,
    "threads": port.ThreadPort,
    "workflows": port.WorkflowPort,
    "primitives": port.PrimitivePort,
    "grading": port.GradingPort,
    "computes": port.ComputePort,
}


def test_the_port_names_nothing_of_the_host() -> None:
    for module in Path(inspect.getfile(port)).parent.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        found = HOST_WORDS.search(source)
        assert found is None, f"{module.name}: {found.group(0) if found else ''}"


@pytest.mark.parametrize("forge", [FakeForge(), CachedForge(FakeForge(), enabled=True)])
def test_every_area_operation_is_implemented(forge: object) -> None:
    assert isinstance(forge, Forge)
    for area, protocol in AREAS.items():
        implementation = getattr(forge, area)
        missing = [
            name
            for name in _operations(protocol)
            if not callable(getattr(implementation, name, None))
        ]
        assert missing == [], f"{type(forge).__name__}.{area} lacks {missing}"


def test_every_error_code_is_unique() -> None:
    codes = [
        cls.code
        for cls in vars(errors).values()
        if inspect.isclass(cls)
        and issubclass(cls, UniconError)
        and cls not in (UniconError, errors.PortError, errors.ServiceError)
    ]
    assert len(codes) == len(set(codes))


def _operations(protocol: type) -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(protocol)
        if not name.startswith("_") and inspect.isfunction(member)
    ]
