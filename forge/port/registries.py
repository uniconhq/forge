"""Resolve a language to immutable public package pieces for both machines."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from forge.domain.environment import CondaPiece


class RegistryPort(Protocol):
    async def solve(
        self, language: str, specs: Sequence[str], newest_before: datetime | None
    ) -> dict[str, list[CondaPiece]]: ...
