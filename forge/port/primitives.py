"""Primitives: the grading steps the platform defines, held at the host so
the compiler can read their declarations.
"""

from typing import Protocol

from forge.domain.identity import Identity
from forge.domain.ids import PrimitiveId
from forge.domain.workflows import Primitive


class PrimitivePort(Protocol):
    async def list_primitives(self) -> tuple[Primitive, ...]:
        """Every primitive the platform holds, with its versions."""
        ...

    async def read_declaration(self, as_: Identity, primitive: PrimitiveId, version: str) -> bytes:
        """The declaration of the primitive at a version, read as `as_`, the
        organiser whose save compiles it: its inputs, outputs and the image it
        runs from. `NotFound` when there is no such primitive or version;
        `Forbidden` when the identity may not read it.
        """
        ...
