"""Workflows and primitives as the port describes them. A workflow is an
arrangement of grading steps owned by an org or a user; a primitive is one
grading step, defined by the platform alone.
"""

from dataclasses import dataclass
from enum import StrEnum

from forge.domain.ids import PrimitiveId, WorkflowId


class Visibility(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"


@dataclass(frozen=True, slots=True)
class Workflow:
    id: WorkflowId
    owner: str
    name: str
    visibility: Visibility
    stars: int
    versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Primitive:
    id: PrimitiveId
    name: str
    versions: tuple[str, ...]
