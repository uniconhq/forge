"""Resolve checked requirements outside a request, retaining files until stored."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forge.domain.environment import Environment, EnvironmentLock


@dataclass(frozen=True, slots=True)
class PreparedEnvironment:
    lock: EnvironmentLock
    files: dict[str, Path]


class PackageResolver(Protocol):
    def prepare(
        self,
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AbstractAsyncContextManager[PreparedEnvironment]:
        """Resolve and download `asked`, awaiting `proceed` before each
        language and each piece downloaded; it raises to stop the work.
        """
        ...
